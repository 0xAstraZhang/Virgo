"""Native process acceptance helpers. Availability never counts as successful isolation."""

from __future__ import annotations

import json
import os
import shlex
import sys

import pytest

from virgo.ai.providers import faux

from tests.support.harness import tool_results
from tests.support.shell import selected


def python_command(session, source):
    argv = [sys.executable, "-I", "-S", "-c", source]
    if selected(session).tool_name == "powershell":
        return "& " + " ".join("'" + v.replace("'", "''") + "'" for v in argv)
    return shlex.join(argv)


def run(session, command, **arguments):
    faux.script(
        "test",
        faux.reply(tools=[(selected(session).tool_name, {"command": command, **arguments})]),
        faux.reply("done"),
    )
    session.prompt("Execute this isolation acceptance operation")
    return tool_results(session)[-1]


def native_session(make_session, platforms):
    required = os.environ.get("VIRGO_REQUIRE_SANDBOX_TESTS") == "1"
    if sys.platform not in platforms:
        if required:
            pytest.fail("Native acceptance requested on the wrong platform")
        pytest.skip("Other-platform native acceptance")
    settings = {"execution": {"mode": "workspace-edit"}}
    if sys.platform == "win32":
        import shutil

        executable = shutil.which("pwsh")
        assert executable, "PowerShell 7 is required by native Windows acceptance"
        settings["shell"] = {"executable": executable}
    session = make_session(
        settings=settings,
        refresh_models=False,
        extensions=False,
    )
    result = run(session, python_command(session, "print('sandbox-ready')"))
    assert not result.is_error, result.text()
    boundary = result.details["boundary"]
    assert boundary["provider"] == "sandbox" and boundary["state"] == "established", json.dumps(boundary)
    assert boundary["payload_started"] is True and "sandbox-ready" in result.text()
    return session
