"""Notifications in the live timeline and saved history."""

import asyncio
import json

import pytest

from virgo.agent.types import AgentEvent
from virgo.ai.providers import faux
from virgo.ai.types import ImageContent, TextContent, UserMessage
from virgo.cli.tui.app import VirgoApp
from virgo.cli.tui.bridge import FullscreenUI
from virgo.cli.tui.transcript import EventView, ToolView, Transcript
from virgo.harness.store import SessionStore

from tests.support.harness import calls, tool_results
from tests.support.shell import selected
from tests.support.tui import TranscriptApp, has_reply, saved_entry, wait_for

OPEN = '<notification source="diagnostics">'
CLOSE = "</notification>"


def test_compacted_permission_failures_in_live_and_reopened_transcript(broker_session, tmp_path):
    async def scenario():
        ui = FullscreenUI()
        session = broker_session(ui=ui, persist=True, refresh_models=False,
                                 settings={"compaction": {"keepRecentTokens": 1}, "language": "en"})
        shell = selected(session).tool_name
        outside = tmp_path / "outside.txt"
        outside.write_text("outside the workspace")
        faux.push("test", faux.reply(tools=[
            ("read", {"path": str(outside)}), (shell, {"command": "git push --dry-run --force origin feature"}),
        ]), faux.reply("Both operations were blocked."))
        app = VirgoApp(session, ui, initial="Check the file and the command")
        async with app.run_test(size=(100, 32)) as pilot:
            for _ in range(2):
                await wait_for(pilot, lambda: app.active_confirmation is not None)
                await pilot.press("n")
            await wait_for(pilot, lambda: has_reply(app, "Both operations were blocked.") and not app.controller.busy)
            ids = {result.tool_name: result.details["error"]["error_id"] for result in tool_results(session)}
            assert ids.keys() == {"read", shell} and len(set(ids.values())) == 2
            faux.push("test", faux.reply("Two operations were blocked."))
            app.controller.submit("/compact")
            await wait_for(pilot, lambda: session.store.last_compaction() is not None and not app.controller.busy)

            faux.push("test", faux.reply(tools=[("diagnostic_get", {"error_id": i}) for i in ids.values()]),
                      faux.reply("Checked both diagnostics."))
            app.controller.submit("Look up both failures")
            await wait_for(pilot, lambda: has_reply(app, "Checked both diagnostics.") and not app.controller.busy)
            pages = [json.loads(r.text()) for r in tool_results(session) if r.tool_name == "diagnostic_get"]
            assert {p["error_id"] for p in pages if p["status"] == "ok"} == set(ids.values())

            # Both requests in this turn carry the same single, model-visible checkpoint.
            def checkpoints(request):
                return [m["content"] for m in request["messages"] if m["role"] == "user"
                        and "Retained diagnostic references" in m["content"]]

            [wire] = checkpoints(calls()[-2])
            assert checkpoints(calls()[-1]) == [wire]
            assert wire.startswith(OPEN + "\n") and wire.endswith("\n" + CLOSE)
            body = wire.removeprefix(OPEN + "\n").removesuffix("\n" + CLOSE)
            assert "diagnostic_get(error_id, section)" in body
            assert all(f"{i}: tool.permission / TOOL_PERMISSION_FAILED" in body for i in ids.values())

            def check_transcript(label):
                rows = [row for row in app.transcript.query(ToolView) if row.status == "blocked"]
                assert len(rows) == 2 and {row.tool_name for row in rows} == set(ids)
                for row in rows:
                    boundary = row.details["boundary"]
                    assert boundary["kind"] == ("resource_denied" if row.tool_name == "read" else "high_risk_denied")
                    assert boundary["payload_started"] is False and boundary["side_effects"] == "none"
                    assert row.details["error"]["error_id"] == ids[row.tool_name]
                [event] = [e for e in app.transcript.query(EventView) if "Retained diagnostic references" in e.text]
                assert not event.output.display
                assert event.header.content.plain.startswith(f"▸ {label} · Retained diagnostic references")
                assert OPEN not in event.header.content.plain and CLOSE not in event.header.content.plain
                assert event.kind == "diagnostics"
                event.action_toggle()
                assert event.output.display and event.output.content == body
                return event

            event = check_transcript("Diagnostics")
            event.action_toggle()
            session.set_language("zh-CN", persist=False)
            await wait_for(pilot, lambda: event.header.content.plain.startswith("▸ 诊断 · "))
            restored = SessionStore.load(session.store.path)
            await app.transcript.load_history(restored.branch())
            check_transcript("诊断")

    asyncio.run(scenario())


def test_user_authored_diagnostic_xml_stays_literal():
    async def scenario():
        literal = f"{OPEN}\nhello\n{CLOSE}"
        message = UserMessage([TextContent(literal), ImageContent("", "image/png")])
        expected = literal + "\n[image: image/png]"
        async with TranscriptApp().run_test() as pilot:
            view = pilot.app.query_one(Transcript)
            await view.consume(AgentEvent("agent_start"))
            await view.consume(AgentEvent("turn_start"))
            for agent in ("main", "main", "explorer"):
                await view.consume(AgentEvent("message_end", agent, message=message))
            assert view.query_one(".user-message").content.plain == "> " + expected
            rows = list(view.query(EventView))
            assert [(row.kind, row.agent) for row in rows] == [("user", "main"), ("user", "explorer")]
            for row in rows:
                row.action_toggle()
                assert row.output.content == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", ["live", "history"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
@pytest.mark.parametrize("opening", ['<notification source="jobs">', '<notification source="calendar">', "<notification>"])
def test_notification_envelope_unwraps_in_transcript(opening, newline, delivery):
    async def scenario():
        body = f"Job finished{newline}  Details follow.  {newline}"
        message = UserMessage([TextContent(f"{opening}{newline}{body}{newline}{CLOSE}")], kind="notification")
        async with TranscriptApp().run_test() as pilot:
            view = pilot.app.query_one(Transcript)
            if delivery == "live":
                await view.consume(AgentEvent("message_end", message=message))
            else:
                await view.load_history([saved_entry(message)])
            row = view.query_one(EventView)
            assert row.kind == "notification" and not row.output.display
            header = row.header.content.plain
            assert header.startswith("▸ Notification · Job finished")
            assert "<notification" not in header and CLOSE not in header
            row.action_toggle()
            assert row.output.display and row.output.content == body

    asyncio.run(scenario())


@pytest.mark.parametrize("text", [
    "Background news", f'<notification source=jobs>\nJob finished\n{CLOSE}',
    f"before {OPEN}\nhello\n{CLOSE}", f"{OPEN}\nhello\n{CLOSE}\nafter", f"{OPEN}\nunterminated",
])
def test_unframed_and_malformed_notifications_stay_literal(text):
    async def scenario():
        async with TranscriptApp().run_test() as pilot:
            view = pilot.app.query_one(Transcript)
            await view.consume(AgentEvent("message_end", message=UserMessage(text, kind="notification")))
            row = view.query_one(EventView)
            assert row.kind == "notification" and row.header.content.plain.startswith("▸ Notification · ")
            row.action_toggle()
            assert row.output.content == text

    asyncio.run(scenario())


@pytest.mark.parametrize("body", ["", "  \t", "\n  unexpected </notification> in input\r\n  next line\n"])
def test_diagnostic_content_blocks_preserve_body_and_images(body):
    async def scenario():
        async with TranscriptApp().run_test() as pilot:
            view = pilot.app.query_one(Transcript)
            message = UserMessage([TextContent(f"{OPEN}\r\n{body}\r\n{CLOSE}"), ImageContent("", "image/png")],
                                  kind="notification")
            await view.load_history([saved_entry(message)])
            row = view.query_one(EventView)
            assert row.kind == "diagnostics"
            row.action_toggle()
            assert row.output.content == body + "\n[image: image/png]"

    asyncio.run(scenario())
