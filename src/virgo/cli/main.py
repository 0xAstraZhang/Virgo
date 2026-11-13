"""Command-line entry point: fullscreen/plain UI, print mode and JSON events."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .. import __version__
from ..ai.types import AssistantMessage
from ..diagnostics import capture_error, emergency_write, error_summary, safe_text
from ..harness.diagnostics import DiagnosticLog
from ..harness.permissions import MODES, normalize_permission_mode
from ..harness.session import AgentSession
from ..harness.settings import Paths
from ..harness.store import SessionStore
from .console import ConsoleBroker, PrintUI, TerminalUI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="virgo", description="Virgo — a lightweight, extensible coding agent.")
    p.add_argument("prompt", nargs="*", help="initial prompt")
    p.add_argument(
        "-p", "--print", action="store_true", help="run the prompt non-interactively and print the answer"
    )
    p.add_argument(
        "--mode", choices=["text", "json"], default="text", help="print-mode output (json = event stream)"
    )
    p.add_argument(
        "--ui",
        choices=["auto", "fullscreen", "plain"],
        default="auto",
        help="interactive UI (auto = fullscreen on a supported terminal, otherwise plain)",
    )
    p.add_argument(
        "-m", "--model", help="model as provider/id, e.g. anthropic/claude-opus-5-5 or openai/gpt-5"
    )
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="reasoning effort")
    p.add_argument(
        "--permission-mode",
        type=normalize_permission_mode,
        choices=MODES,
        help="permission mode (auto-review: review external/network operations; accept-all: no approval; plan: read-only)",
    )
    p.add_argument(
        "-c", "--continue", dest="cont", action="store_true", help="continue the most recent session"
    )
    p.add_argument(
        "-r", "--resume", nargs="?", const="", metavar="FILE", help="resume a session (pick from a list)"
    )
    p.add_argument("--no-session", action="store_true", help="do not save this session")
    p.add_argument("--no-extensions", action="store_true", help="skip user/project extensions")
    p.add_argument("-C", "--cwd", default=".", help="working directory")
    p.add_argument("-V", "--version", action="version", version=f"virgo {__version__}")
    return p


def _pick_session(sessions_dir: Path) -> Path | None:
    items = SessionStore.list(sessions_dir) if sessions_dir.is_dir() else []
    if not items:
        print("No saved sessions for this directory.", file=sys.stderr)
        return None
    listed = items[:20]
    for i, it in enumerate(listed, 1):
        print(f"{i:>3}. {it['title']}  ({it['messages']} msgs)")
    try:
        choice = input("session number: ").strip()
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


def _startup_error(error: Exception, cwd: str) -> None:
    """Initialization can fail before an AgentSession owns a diagnostic sink."""
    details = getattr(error, "_virgo_error_details", None)
    if not isinstance(details, dict):
        details = capture_error(error, phase="cli.startup", context={"cwd": cwd})
    try:
        if not details.get("log_path") and not details.get("logging_error"):
            details = DiagnosticLog(Paths(Path(cwd)).user / "logs").record(details)
        print(f"virgo: {error_summary(details)}", file=sys.stderr)
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
    args = build_parser().parse_args(argv)
    fullscreen = False
    if not args.print and args.ui != "plain":
        fullscreen = _fullscreen_available()
        if args.ui == "fullscreen" and not fullscreen:
            print(
                "virgo: fullscreen needs an interactive stdin/stdout and a supported TERM; "
                "use --ui plain or -p.",
                file=sys.stderr,
            )
            return 2
    prompt = " ".join(args.prompt).strip()
    if args.print and not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            prompt = f"{prompt}\n\n{piped}" if prompt else piped
    if args.print and not prompt:
        print("virgo: -p needs a prompt (argument or stdin)", file=sys.stderr)
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
            picked = _pick_session(Paths(Path(args.cwd)).sessions)
            if picked is None:
                return 1
            session_arg = picked

    overrides = {"effort": args.effort} if args.effort else None
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
        _startup_error(error, args.cwd)
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
