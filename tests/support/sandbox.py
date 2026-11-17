"""Real MXC acceptance helpers. Availability never counts as successful isolation."""

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
    executable = os.environ.get("VIRGO_MXC_EXECUTABLE")
    if not executable:
        if required:
            pytest.fail("VIRGO_MXC_EXECUTABLE must name an explicitly installed MXC 1.1.0 executor")
        pytest.skip("MXC executor not configured; no native isolation claim")
    session = make_session(
        settings={"execution": {"provider": "sandbox"}, "sandbox": {"executable": executable}},
        refresh_models=False,
        extensions=False,
    )
    result = run(session, python_command(session, "print('sandbox-ready')"))
    assert not result.is_error, result.text()
    boundary = result.details["boundary"]
    assert boundary["provider"] == "sandbox" and boundary["state"] == "established", json.dumps(boundary)
    assert boundary["payload_started"] is True and "sandbox-ready" in result.text()
    return session
