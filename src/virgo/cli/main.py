"""Command-line entry point: fullscreen/plain UI, print mode and JSON events."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .. import __version__
from ..ai.types import AssistantMessage
from ..diagnostics import capture_error, emergency_write, error_summary, safe_text
from ..features.permission import MODES, normalize_permission_mode
from ..harness.diagnostics import DiagnosticLog
from ..harness.session import AgentSession
from ..harness.settings import Paths, Settings
from ..harness.store import SessionStore
from ..i18n import Translator, normalize_language
from .console import ConsoleBroker, PrintUI, TerminalUI
from .i18n import tr


class _LocalizedFormatter(argparse.HelpFormatter):
    def __init__(self, prog: str, *, translator: Translator):
        super().__init__(prog)
        self.translator = translator

    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix or self.translator("ui.cli.usage"))


class _LocalizedParser(argparse.ArgumentParser):
    def __init__(self, *, translator: Translator, **kwargs):
        self.translator = translator
        super().__init__(
            add_help=False,
            formatter_class=lambda prog: _LocalizedFormatter(prog, translator=translator),
            **kwargs,
        )
        self._positionals.title = translator("ui.cli.arguments")
        self._optionals.title = translator("ui.cli.options")
        self.add_argument("-h", "--help", action="help", help=translator("ui.cli.help"))

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, self.translator("ui.cli.error", prog=self.prog, message=message))


def build_parser(language: str = "en") -> argparse.ArgumentParser:
    t = Translator(language)
    p = _LocalizedParser(translator=t, prog="virgo", description=tr(t, "ui.cli.description"))
    p.add_argument("--language", type=normalize_language, metavar="LANGUAGE", help=t("ui.cli.language"))
    p.add_argument("prompt", nargs="*", help=tr(t, "ui.cli.prompt"))
    p.add_argument("-p", "--print", action="store_true", help=tr(t, "ui.cli.print"))
    p.add_argument("--mode", choices=["text", "json"], default="text", help=tr(t, "ui.cli.mode"))
    p.add_argument(
        "--ui",
        choices=["auto", "fullscreen", "plain"],
        default="auto",
        help=tr(t, "ui.cli.ui"),
    )
    p.add_argument("-m", "--model", help=tr(t, "ui.cli.model"))
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help=tr(t, "ui.cli.effort"))
    p.add_argument(
        "--permission-mode",
        type=normalize_permission_mode,
        choices=MODES,
        help=tr(t, "ui.cli.permissions"),
    )
    p.add_argument("-c", "--continue", dest="cont", action="store_true", help=tr(t, "ui.cli.continue"))
    p.add_argument("-r", "--resume", nargs="?", const="", metavar="FILE", help=tr(t, "ui.cli.resume"))
    p.add_argument("--no-session", action="store_true", help=tr(t, "ui.cli.no_session"))
    p.add_argument("--no-extensions", action="store_true", help=tr(t, "ui.cli.no_extensions"))
    p.add_argument("-C", "--cwd", default=".", help=tr(t, "ui.cli.cwd"))
    p.add_argument(
        "-V", "--version", action="version", version=f"virgo {__version__}", help=t("ui.cli.version")
    )
    return p


def _pick_session(sessions_dir: Path, t: Translator | None = None) -> Path | None:
    t = t or Translator()
    items = SessionStore.list(sessions_dir) if sessions_dir.is_dir() else []
    if not items:
        print(tr(t, "ui.cli.no_saved_sessions"), file=sys.stderr)
        return None
    listed = items[:20]
    for i, it in enumerate(listed, 1):
        print(tr(t, "ui.cli.session_row", index=i, title=it["title"], count=it["messages"]))
    try:
        choice = input(tr(t, "ui.cli.session_number")).strip()
    except EOFError:
        return None
    # Only listed numbers are valid: 0 or a negative value must not index from the end.
    if not choice.isdecimal() or not 1 <= int(choice) <= len(listed):
        return None
    return listed[int(choice) - 1]["path"]


def _fullscreen_available() -> bool:
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "").lower() not in {"", "dumb", "unknown"}
    )


def _startup_error(error: Exception, cwd: str, *, t: Translator | None = None) -> None:
    """Initialization can fail before an AgentSession owns a diagnostic sink."""
    details = getattr(error, "_virgo_error_details", None)
    if not isinstance(details, dict):
        details = capture_error(error, phase="cli.startup", context={"cwd": cwd})
    try:
        if not details.get("log_path") and not details.get("logging_error"):
            details = DiagnosticLog(Paths(Path(cwd)).user / "logs").record(details)
        print(f"virgo: {error_summary(details, t=t)}", file=sys.stderr)
    except Exception as reporting_error:
        emergency_write(details, reporting_error)


def _shutdown(session: AgentSession) -> bool:
    try:
        session.shutdown()
        return True
    except Exception as error:
        session.report_error(error, phase="session.shutdown")
        return False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--language")
    bootstrap.add_argument("-C", "--cwd", default=".")
    early, _ = bootstrap.parse_known_args(argv)
    try:
        language = early.language or Settings.load(Paths(Path(early.cwd))).get("language", "en")
        t = Translator(language)
    except (OSError, ValueError) as error:
        _startup_error(error, early.cwd)
        return 2
    args = build_parser(t.language).parse_args(argv)
    fullscreen = False
    if not args.print and args.ui != "plain":
        fullscreen = _fullscreen_available()
        if args.ui == "fullscreen" and not fullscreen:
            print(
                tr(t, "ui.cli.fullscreen_error"),
                file=sys.stderr,
            )
            return 2
    prompt = " ".join(args.prompt).strip()
    if args.print and not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            prompt = f"{prompt}\n\n{piped}" if prompt else piped
    if args.print and not prompt:
        print(tr(t, "ui.cli.print_prompt_error"), file=sys.stderr)
        return 2

    session_arg: str | Path | None = "new"
    pick_session = False
    if args.no_session:
        session_arg = None
    elif args.cont:
        session_arg = "continue"
    elif args.resume is not None:
        if args.resume:
            session_arg = Path(args.resume)
        elif fullscreen:
            pick_session = True
        else:
            picked = _pick_session(Paths(Path(args.cwd)).sessions, t)
            if picked is None:
                return 1
            session_arg = picked

    overrides = {"effort": args.effort} if args.effort else {}
    if args.language:
        overrides["language"] = args.language
    try:
        if args.print:
            ui = PrintUI(json_mode=args.mode == "json")
        elif fullscreen:
            from .tui.bridge import FullscreenUI

            ui = FullscreenUI()
        else:
            broker = ConsoleBroker()
            ui = TerminalUI(broker)
        session = AgentSession(
            args.cwd,
            ui=ui,
            model=args.model,
            session=session_arg,
            permission_mode=args.permission_mode,
            settings=overrides,
            extensions=not args.no_extensions,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        _startup_error(error, args.cwd, t=t)
        return 2 if isinstance(error, (ValueError, OSError)) else 1

    if not args.print:
        try:
            if fullscreen:
                from .tui import run_fullscreen

                return run_fullscreen(session, ui, initial=prompt or None, pick_session=pick_session)

            from .repl import run_repl

            run_repl(session, ui, broker, prompt or None)
            return 0
        except KeyboardInterrupt:
            session.abort()
            _shutdown(session)
            return 130
        except Exception as error:
            session.report_error(error, phase="cli.interactive")
            _shutdown(session)
            return 1

    exit_code = 0
    try:
        session.prompt(prompt)
        session.wait_idle()
    except KeyboardInterrupt:
        session.abort()
        exit_code = 130
    except Exception as error:
        session.report_error(error, phase="cli.run")
        exit_code = 1
    finally:
        if not _shutdown(session) and not exit_code:
            exit_code = 1
    if exit_code:
        return exit_code
    if session.has_save_failure:
        return 1
    if ui.command_output:
        return 0
    last = next((m for m in reversed(session.main.messages) if isinstance(m, AssistantMessage)), None)
    if last is None:
        return 1
    if last.stop_reason in ("error", "refusal", "aborted"):
        print(f"virgo: {safe_text(last.error_message or last.stop_reason)}", file=sys.stderr)
        return 1
    if args.mode == "text":
        print(last.text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
