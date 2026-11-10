"""Fullscreen terminal frontend; imported lazily by the CLI."""

from __future__ import annotations


def run_fullscreen(session, ui, initial: str | None = None, pick_session: bool = False) -> int:
    import sys

    from .app import VirgoApp

    app = VirgoApp(session, ui, initial=initial, pick_session=pick_session)
    try:
        result = app.run()
        return app.return_code or result or 0
    finally:
        # Textual restores the terminal even if composing/rendering raises.
        # The controller also releases approvals on exceptional exit.
        app.controller.request_close()
        if not app.controller.wait_closed(timeout=5):
            print("virgo: terminal restored; a shutdown hook is still waiting.", file=sys.stderr)
