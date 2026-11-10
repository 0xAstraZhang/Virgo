"""The context report describes the active request, without sending a model call."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from virgo.agent import AgentTool
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, TextContent, Usage, UserMessage
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.harness.context import build_context_report, render_context_report

from .conftest import calls
from .test_tui_app import wait_for

CATEGORIES = (
    "System prompt",
    "System tools",
    "MCP tools",
    "MCP server instruction",
    "Memory files",
    "Skills",
    "Messages",
    "Free space",
    "Autocompact buffer",
)


def row_tokens(report):
    return {row.name: row.tokens for row in report.rows}


def set_usage(session, tokens):
    session.main.messages[:] = [
        UserMessage("hello"),
        AssistantMessage(
            [TextContent("hello back")],
            api="faux",
            provider="faux",
            model="test",
            usage=Usage(input=tokens - 1, output=1),
        ),
    ]


def assert_balanced(report):
    rows = row_tokens(report)
    assert tuple(rows) == CATEGORIES
    assert all(value >= 0 for value in rows.values())
    assert sum(rows[name] for name in CATEGORIES[:7]) == report.used
    assert rows["Free space"] == report.free
    assert rows["Autocompact buffer"] == report.buffer
    if report.used <= report.window:
        assert report.used + report.free + report.buffer == report.window


def test_fresh_context_counts_system_and_tools_without_calling_provider(make_session):
    session = make_session()
    report = build_context_report(session.main)
    rows = row_tokens(report)
    assert rows["System prompt"] > 0
    assert rows["System tools"] > 0
    assert rows["MCP tools"] == rows["MCP server instruction"] == rows["Messages"] == 0
    assert report.used == session.context_tokens() > 0
    assert_balanced(report)
    assert not calls()


def test_report_uses_provider_total_including_cached_input_once(make_session):
    session = make_session()
    session.main.messages[:] = [
        UserMessage("hi"),
        AssistantMessage(
            [TextContent("Hello")],
            usage=Usage(input=160, output=11, cache_read=3584, cache_write=100),
        ),
    ]
    report = build_context_report(session.main)
    assert report.used == 3855 == session.context_tokens()
    assert row_tokens(report)["Messages"] > 0
    assert_balanced(report)
    session.main.messages.append(UserMessage("A new request" * 40))
    assert build_context_report(session.main).used > report.used


def test_old_usage_is_not_reused_after_compaction(make_session):
    session = make_session()
    set_usage(session, 80_000)
    session.main.compacted_at = len(session.main.messages)
    report = build_context_report(session.main)
    assert 0 < report.used < 80_000
    assert row_tokens(report)["System prompt"] > 0
    assert row_tokens(report)["System tools"] > 0
    assert_balanced(report)


def test_rebuilt_system_invalidates_usage_and_counts_new_prompt_text(make_session):
    session = make_session()
    set_usage(session, 80_000)
    original = build_context_report(session.main)
    assert original.reported_usage and original.used == 80_000
    session.sections.append((70, "large-context", lambda role: "Additional instructions.\n" * 20_000))
    session.main.rebuild_system()
    report = build_context_report(session.main)
    assert not report.reported_usage
    assert report.used > original.used
    assert report.used == session.context_tokens()
    assert_balanced(report)


@pytest.mark.parametrize("change", ["plan", "model"])
def test_changed_request_invalidates_previous_provider_usage(make_session, change):
    session = make_session()
    set_usage(session, 80_000)
    assert build_context_report(session.main).reported_usage
    if change == "plan":
        session.permission_mode = "plan"
    else:
        session.set_model("faux/alternate")
    report = build_context_report(session.main)
    assert not report.reported_usage
    assert 0 < report.used < 80_000
    assert report.used == session.context_tokens()
    assert_balanced(report)
    assert not calls()


def test_successful_request_restores_reported_usage_after_mode_change(make_session):
    session = make_session(faux.reply("New answer"), settings={"compaction": {"enabled": False}})
    set_usage(session, 80_000)
    session.permission_mode = "plan"
    assert not build_context_report(session.main).reported_usage
    session.prompt("Continue")
    latest = session.main.messages[-1]
    assert isinstance(latest, AssistantMessage) and latest.text() == "New answer"
    report = build_context_report(session.main)
    assert report.reported_usage
    assert report.used == latest.usage.total == session.context_tokens()
    assert len(calls()) == 1
    assert_balanced(report)


def test_file_categories_describe_frozen_prompt_not_files_changed_on_disk(make_session, project):
    instructions = project / "AGENTS.md"
    instructions.write_text("Use the project conventions.\n" * 10)
    skill = project / ".virgo" / "skills" / "inspect" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: inspect\ndescription: Inspect the project\n---\nRead the source files.")
    session = make_session()
    before = build_context_report(session.main)
    rows = row_tokens(before)
    assert rows["Memory files"] > 0
    assert rows["Skills"] > 0
    instructions.write_text("Changed instructions that are not loaded yet.\n" * 500)
    skill.write_text("A changed skill that is not loaded yet.\n" * 500)
    assert row_tokens(build_context_report(session.main)) == rows
    session.main.rebuild_system()
    assert row_tokens(build_context_report(session.main))["Memory files"] > rows["Memory files"]


def test_mcp_tools_and_server_instructions_are_separate_categories(make_session):
    session = make_session()
    session.register_tool(
        AgentTool(
            "mcp__example__lookup",
            "[MCP example] Look up a record.",
            {"type": "object", "properties": {"query": {"type": "string"}}},
            lambda args, context: "record",
        )
    )
    session.sections.append((30, "mcp", lambda role: "<mcp>Use lookup to fetch records.</mcp>"))
    session.main.tools[:] = session.tools_for("main")
    session.main.rebuild_system()
    report = build_context_report(session.main)
    rows = row_tokens(report)
    assert rows["MCP tools"] > 0
    assert rows["MCP server instruction"] > 0
    assert rows["System tools"] > rows["MCP tools"]
    assert_balanced(report)


def test_plan_context_omits_tools_that_cannot_be_used(make_session):
    session = make_session(mode="accept-all")
    normal = build_context_report(session.main)
    session.permission_mode = "plan"
    planned = build_context_report(session.main)
    assert row_tokens(planned)["System tools"] < row_tokens(normal)["System tools"]
    assert row_tokens(planned)["System prompt"] > row_tokens(normal)["System prompt"]
    assert_balanced(planned)


@pytest.mark.parametrize(
    ("settings", "used", "trigger", "enabled", "free", "buffer", "due"),
    [
        ({}, 10_000, 83_616, True, 73_616, 16_384, False),
        ({"triggerTokens": 80_000}, 80_000, 80_000, True, 0, 20_000, False),
        ({"triggerTokens": 80_000}, 85_000, 80_000, True, 0, 15_000, True),
        ({"triggerTokens": 80_000}, 105_000, 80_000, True, 0, 0, True),
        ({"enabled": False}, 90_000, 83_616, False, 10_000, 0, False),
    ],
)
def test_report_and_compaction_share_threshold_and_space_accounting(
    make_session, settings, used, trigger, enabled, free, buffer, due
):
    session = make_session(settings={"compaction": settings})
    session.main.model.context_window = 100_000
    set_usage(session, used)
    report = build_context_report(session.main)
    assert report.window == 100_000
    assert report.used == used
    assert report.trigger == trigger
    assert report.enabled is enabled
    assert report.free == free
    assert report.buffer == buffer
    assert session.main._compaction_due() is due
    assert report.over_threshold is (enabled and used > trigger)
    assert_balanced(report)


def test_context_command_is_discoverable_and_does_not_change_context(make_session):
    session = make_session(persist=True)
    command = session.commands.get("context")
    assert command is not None and command.concurrent
    session.prompt("/help")
    assert "/context" in session.ui.text
    messages = deepcopy(session.main.messages)
    entries = deepcopy(session.store.branch())
    system = session.main.system
    session.prompt("/context")
    for category in CATEGORIES:
        assert category in session.ui.text
    assert "Auto-compact" in session.ui.text
    assert session.main.messages == messages
    assert session.store.branch() == entries
    assert session.main.system == system
    assert not calls()
    rendered = render_context_report(build_context_report(session.main))
    assert "estimate" in rendered.lower()


def test_context_completes_and_renders_in_fullscreen_without_model_call(make_session):
    async def scenario():
        ui = FullscreenUI()
        session = make_session(ui=ui)
        app = VirgoApp(session, ui)
        async with app.run_test(size=(80, 24)) as pilot:
            app.editor.load_text("/con")
            await wait_for(pilot, lambda: "/context" in app.completion_names)
            await pilot.press("tab")
            assert app.editor.text == "/context "
            await pilot.press("enter")
            await wait_for(pilot, lambda: bool(app.transcript.query(".notice")) and not app.controller.busy)
            rendered = "\n".join(str(widget.content) for widget in app.transcript.query(".notice"))
            for category in CATEGORIES:
                assert category in rendered
            assert "Auto-compact: on" in rendered and "Threshold:" in rendered
            assert app.editor.text == ""
            assert not calls() and not session.main.messages

    asyncio.run(scenario())
