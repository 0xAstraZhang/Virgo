"""Example extension: protect secrets files and add a /stats command."""

from virgo.agent import Block

PROTECTED = (".env", ".env.local", "id_rsa")


def setup(api):
    def guard(ev, session):
        path = str(ev.args.get("path", ""))
        if ev.tool_name in ("write", "edit") and path.endswith(PROTECTED):
            return Block(f"{path} is protected by the protect_env extension.")
        return None

    api.on("tool_call", guard, priority=90)  # runs right after the permission check

    def stats(session, args):
        msgs = session.main.messages
        api.ui.print(f"{len(msgs)} messages, ~{session.context_tokens()} context tokens")

    api.register_command("stats", stats, "show conversation stats", concurrent=True)
