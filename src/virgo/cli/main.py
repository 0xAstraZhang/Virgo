"""Command-line entry point: fullscreen/plain UI, print mode and JSON events."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .. import __version__
from ..ai.types import AssistantMessage
from ..harness.session import AgentSession
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
        choices=["default", "accept-edits", "plan", "yolo"],
        help="permission mode (default = ask before changes)",
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
    for i, it in enumerate(items[:20], 1):
        print(f"{i:>3}. {it['title']}  ({it['messages']} msgs)")
    try:
        choice = input("session number: ").strip()
        return items[int(choice) - 1]["path"]
    except (ValueError, IndexError, EOFError):
        return None


def _fullscreen_available() -> bool:
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "").lower() not in {"", "dumb", "unknown"}
    )


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
            from ..harness.settings import Paths

            picked = _pick_session(Paths(Path(args.cwd)).sessions)
            if picked is None:
                return 1
            session_arg = picked

    overrides = {"effort": args.effort} if args.effort else None
    if args.print:
        ui = PrintUI(json_mode=args.mode == "json")
    elif fullscreen:
        from .tui.bridge import FullscreenUI

        ui = FullscreenUI()
    else:
        broker = ConsoleBroker()
        ui = TerminalUI(broker)
    try:
        session = AgentSession(
            args.cwd,
            ui=ui,
            model=args.model,
            session=session_arg,
            permission_mode=args.permission_mode,
            settings=overrides,
            extensions=not args.no_extensions,
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"virgo: {e}", file=sys.stderr)
        return 2

    if not args.print:
        if fullscreen:
            from .tui import run_fullscreen

            return run_fullscreen(session, ui, initial=prompt or None, pick_session=pick_session)

        from .repl import run_repl

        run_repl(session, ui, broker, prompt or None)
        return 0

    if not prompt:
        print("virgo: -p needs a prompt (argument or stdin)", file=sys.stderr)
        return 2
    try:
        session.prompt(prompt)
        session.wait_idle()
    except KeyboardInterrupt:
        session.abort()
        return 130
    finally:
        session.shutdown()
    last = next((m for m in reversed(session.main.messages) if isinstance(m, AssistantMessage)), None)
    if last is None:
        return 1
    if last.stop_reason in ("error", "refusal", "aborted"):
        print(f"virgo: {last.error_message or last.stop_reason}", file=sys.stderr)
        return 1
    if args.mode == "text":
        print(last.text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
