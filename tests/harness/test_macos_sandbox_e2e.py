"""Native sandbox behavior specific to macOS."""

from __future__ import annotations

import tempfile
from pathlib import Path

from virgo.ai.providers import faux

from tests.support.harness import tool_results
from tests.support.sandbox import native_session, python_command
from tests.support.shell import selected


def test_macos_tmp_symlink_grant_and_real_path_dialog(make_session):
    """Approving /tmp/<dir> resolves to /private/tmp/<dir> and works for Read and Shell."""
    native = native_session(make_session, {"darwin"})
    with tempfile.TemporaryDirectory(prefix="virgo-tmp-", dir="/tmp") as raw_dir:
        raw_path = Path("/tmp") / Path(raw_dir).name
        real_path = raw_path.resolve()
        assert str(raw_path) != str(real_path)
        sample = real_path / "data.txt"
        sample.write_text("from-tmp")

        shell = selected(native).tool_name
        cmd = {"command": python_command(native, f"from pathlib import Path; print(Path({str(raw_path / 'data.txt')!r}).read_text())")}
        native.ui.answers.extend(["y", "y"])
        faux.script(
            "test",
            faux.reply(tools=[("read", {"path": str(raw_path / "data.txt")})]),
            faux.reply(tools=[("request_permissions", {
                "tool": shell, "arguments": cmd,
                "permissions": {"read_paths": [str(raw_path)]},
                "reason": "Read temporary fixture via /tmp path",
            })]),
            faux.reply(tools=[(shell, cmd)]),
            faux.reply("done"),
        )
        native.prompt("Read via /tmp symlink and run approved shell command")
        read_res, req_res, shell_res = tool_results(native)[-3:]
        assert not read_res.is_error and "from-tmp" in read_res.text(), read_res.text()
        assert not req_res.is_error, req_res.text()
        assert not shell_res.is_error and "from-tmp" in shell_res.text(), shell_res.text()
        # Both approval dialogs must display the resolved real path (/private/tmp/...).
        assert len(native.ui.questions) == 2
        assert all(str(real_path) in q for q in native.ui.questions)
