"""Read the pinned upstream license through Virgo's real Read-Only web tool."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from virgo.ai.providers import faux
from virgo.ai.types import ToolResultMessage
from virgo.harness.session import AgentSession
from virgo.harness.ui import UI

REVISION = "04e94ed45992d8e2cbf2178c8c6b562704092a83"
URL = f"https://raw.githubusercontent.com/0xAstraZhang/Virgo/{REVISION}/LICENSE"
EXPECTED_SHA256 = "a994ec17c4cede6b3bc5264c40ad566bd8d8175a3dab57f5a87ca4db8f1ac984"


class NoApprovalUI(UI):
    def __init__(self):
        self.questions = []

    def confirm(self, question, agent="main"):
        self.questions.append(question)
        raise AssertionError(f"Unexpected approval in Read-Only mode: {question}")

    def select(self, question, options, *, timeout=None):
        raise AssertionError(f"Unexpected interaction in Read-Only mode: {question}")


def main():
    with tempfile.TemporaryDirectory(prefix="virgo-readonly-web-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        ui = NoApprovalUI()
        session = AgentSession(workspace, home=root / "home", model="faux/web-smoke", session=None,
                               settings={"execution": {"mode": "read-only"}}, ui=ui,
                               extensions=False, refresh_models=False)
        try:
            router = session.services["execution"]
            before = router.mode_for(session.main).flags()
            assert router.selected_mode_id == "read-only" and before["network_allowed"] is False
            assert before["web_allowed"] is True
            faux.script("web-smoke", faux.reply(tools=[("web_fetch", {"url": URL})]), faux.reply("done"))
            session.prompt("Read the upstream license.")
            result, = [m for m in session.main.messages if isinstance(m, ToolResultMessage)]
            assert result.tool_name == "web_fetch" and not result.is_error, result.text()
            fetched = json.loads(result.text())
            assert fetched["backend"] == "http" and fetched["final_url"] == URL
            assert fetched["truncated"] is False
            digest = hashlib.sha256(fetched["content"].encode("utf-8")).hexdigest()
            assert digest == EXPECTED_SHA256, digest
            assert router.selected_mode_id == "read-only" and router.mode_for(session.main).flags() == before
            print(json.dumps({"url": URL, "sha256": digest, "mode": router.selected_mode_id,
                              "shell_network": before["network_allowed"],
                              "approval_count": len(ui.questions)}))
        finally:
            session.shutdown()


if __name__ == "__main__":
    main()
