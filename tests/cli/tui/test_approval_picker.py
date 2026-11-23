"""Approval details and controls as rendered in the fullscreen app."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from textual.widgets import OptionList

from virgo.ai.providers import faux
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import Confirmation, FullscreenUI
from virgo.cli.tui.dialogs import ApprovalPicker

from tests.support.shell import selected
from tests.support.tui import wait_for

RESOURCE_CHOICES = {
    "en": ["1. Allow for this session [y]", "› 2. Deny [n]", "3. Always allow for project [a]"],
    "zh-CN": ["1. 本次会话允许 [y]", "› 2. 拒绝 [n]", "3. 此项目始终允许 [a]"],
}


def rendered_rows(app, region):
    screen = app.screen._compositor.render_strips()
    return [strip.crop(region.x, region.right).text.strip() for strip in screen[region.y:region.bottom]]


@asynccontextmanager
async def approval(session, operation, size):
    faux.script("test", faux.reply(tools=[operation]), faux.reply("done"))
    app = VirgoApp(session, session.ui, initial="Perform the requested operation")
    async with app.run_test(size=size) as pilot:
        picker = app.query_one(ApprovalPicker)
        options = picker.query_one(OptionList)
        await wait_for(pilot, lambda: picker.display and options.region.height > 0)
        await pilot.pause()
        try:
            yield app, pilot, picker, options
        finally:
            if picker.display:
                await pilot.press("escape")
            await wait_for(pilot, lambda: not app.controller.busy)


@pytest.mark.parametrize("language,read_label", [("en", "Read access: "), ("zh-CN", "读取权限：")])
def test_initial_viewport_identifies_requested_parent(broker_session, tmp_path, language, read_label):
    root = tmp_path / "long-workspace-component-for-the-approval-dialog"
    workspace = root / "workspace_deepseek"
    workspace.mkdir(parents=True)
    directory = root / "outside_docs"
    directory.mkdir()
    session = broker_session(ui=FullscreenUI(), settings={"language": language}, cwd=workspace)
    request = ("request_permissions", {
        "tool": "read", "permissions": {"read_paths": ["../outside_docs"]},
        "reason": "Read outside_docs/note_deny.txt for the user.",
    })

    async def scenario():
        async with approval(session, request, (120, 36)) as (app, _, picker, options):
            question = picker.query_one("#approval-question")
            assert question.scroll_y == 0
            viewport = "".join(rendered_rows(app, question.scrollable_content_region))
            # Long canonical paths may fold across multiple terminal rows.
            grant = f"{read_label}{directory.resolve()}"
            assert "".join(viewport.split()).startswith("".join(grant.split()))
            assert "read {}" not in viewport and "Project:" not in viewport
            assert picker.confirmation.question.splitlines() == [
                grant, "Operation: read",
                "Reason: Read outside_docs/note_deny.txt for the user.", f"Working directory: {workspace.resolve()}",
            ]
            assert rendered_rows(app, options.region) == RESOURCE_CHOICES[language]

    asyncio.run(scenario())


@pytest.mark.parametrize("language", RESOURCE_CHOICES)
def test_paging_shows_full_details_and_keeps_choices_visible(broker_session, language):
    session = broker_session(ui=FullscreenUI(), preset="read-only", settings={"language": language})
    shell = selected(session).tool_name
    args = {"command": "echo " + "abcdefghij " * 10 + "终点", "timeout": 30}
    reason = "Review the network request and the complete command before deciding."
    request = ("request_permissions", {
        "tool": shell, "arguments": args, "permissions": {"network_allowed": True}, "reason": reason,
    })

    async def scenario():
        async with approval(session, request, (40, 16)) as (app, pilot, picker, options):
            question = picker.query_one("#approval-question")
            assert question.max_scroll_y > 0 and question.show_vertical_scrollbar
            assert question.styles.scrollbar_size_vertical >= 1
            assert app.transcript.styles.scrollbar_size_vertical == 0
            bar = question.vertical_scrollbar.region
            assert bar.width == 1 and bar.height == question.region.height
            strips = app.screen._compositor.render_strips()
            assert any(segment.style and segment.style.meta.get("@mouse.down") == "grab"
                       for strip in strips[bar.y:bar.bottom] for segment in strip.crop(bar.x, bar.right))

            # Reassemble the rows actually exposed by paging, including the overlapping final page.
            seen = {}
            while True:
                first = question.scroll_offset.y
                seen.update(enumerate(rendered_rows(app, question.scrollable_content_region), first))
                assert options.highlighted == 1
                assert rendered_rows(app, options.region) == RESOURCE_CHOICES[language]
                if first == question.max_scroll_y:
                    break
                await pilot.press("pagedown")
                await pilot.wait_for_scheduled_animations()
                assert question.scroll_offset.y > first
            visible = "".join("".join(seen[i] for i in range(len(seen))).split())
            details = (f"Operation: {shell} {json.dumps(args, ensure_ascii=False)}"
                       f"Reason: {reason}Working directory: {session.cwd.resolve()}")
            assert "".join(details.split()) in visible
            while question.scroll_offset.y:
                before = question.scroll_offset.y
                await pilot.press("pageup")
                await pilot.wait_for_scheduled_animations()
                assert question.scroll_offset.y < before
            await pilot.press("down")
            assert options.highlighted == 2
            await pilot.press("up", "enter")
            await wait_for(pilot, lambda: not picker.display)

    asyncio.run(scenario())


@pytest.mark.parametrize("language,once,deny,scope", [
    ("en", "1. Allow once [y]", "› 2. Deny [n]", "This operation only"),
    ("zh-CN", "1. 允许一次 [y]", "› 2. 拒绝 [n]", "仅此操作"),
])
@pytest.mark.parametrize("command,ending", [
    ("git reset --hard", "Approve executing this command once?"),
    ("echo ready", "switch permanently with /permissions no-limit.)"),
])
def test_single_operation_picker(broker_session, language, once, deny, scope, command, ending):
    session = broker_session(ui=FullscreenUI(), settings={"language": language})
    operation = (selected(session).tool_name, {"command": command})

    async def scenario():
        async with approval(session, operation, (120, 36)) as (app, _, picker, options):
            question = picker.query_one("#approval-question")
            assert question.max_scroll_y == 0 and not question.show_vertical_scrollbar
            assert scope in rendered_rows(app, picker.query_one("#approval-title").region)[0]
            assert rendered_rows(app, options.region) == [once, deny]
            assert " ".join(rendered_rows(app, question.scrollable_content_region)).endswith(ending)
            assert "[y/n]" not in picker.confirmation.question.lower()

    asyncio.run(scenario())


def test_short_question_with_cli_hint_fits_without_scrollbar(make_session):
    ui = FullscreenUI()
    app = VirgoApp(make_session(ui=ui), ui)
    hint = "Allow for this session [y] / Deny [n] / Always allow for project [a]"

    async def scenario():
        async with app.run_test(size=(40, 16)) as pilot:
            picker = app.query_one(ApprovalPicker)
            picker.open(Confirmation(1, f"Grant access?\n{hint}", "main"))
            await pilot.pause()
            question = picker.query_one("#approval-question")
            assert rendered_rows(app, question.scrollable_content_region) == ["Grant access?"]
            assert question.max_scroll_y == 0 and not question.show_vertical_scrollbar

    asyncio.run(scenario())
