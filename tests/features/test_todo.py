"""Model checklists are optional, conversation-owned state retained across compaction."""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path

import pytest

from virgo.agent import AgentTool, ToolResult
from virgo.ai.compaction import native_api
from virgo.ai.providers import faux
from virgo.ai.types import AssistantMessage, RawContent, TextContent, ToolCall, ToolResultMessage, UserMessage
from virgo.harness import compaction
from virgo.harness.extensions import ExtensionAPI

from tests.support.harness import calls, tool_results


@pytest.fixture
def todo_extension():
    directory = Path(os.environ["VIRGO_HOME"]) / "extensions"
    directory.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[2] / "extensions" / "todo.py"
    shutil.copy(source, directory / "todo.py")
    return source


def item(content, status="pending", step="StepA"):
    return {"step": step, "content": content, "status": status}


def retained(runtime):
    system = runtime.request_context(runtime.messages).system
    if "<todos_state>" not in system:
        return None
    assert system.count("<todos_state>") == 1
    return json.loads(system.split("<todos_state>\n", 1)[1].split("\n", 1)[1].split("\n</todos_state>", 1)[0])


def summarize(session):
    faux.push("test", faux.reply("A summary deliberately omitting the checklist."))
    assert session.compact() is not None


@pytest.mark.parametrize("persist", [False, True])
def test_incomplete_todos_survive_compaction_and_resume(make_session, todo_extension, persist):
    current = {"todos": [item("already finished", "completed"), item("retain this exact pending task", step="StepB")],
               "explanation": "Keep the entire replacement, including finished work."}
    session = make_session(
        faux.reply(tools=[("todo_write", current)]), faux.reply("recent work " * 1000),
        persist=persist, settings={"compaction": {"keepRecentTokens": 1}},
    )
    session.prompt("track work")
    assert retained(session.main) is None
    summarize(session)
    assert retained(session.main) == current
    before = copy.deepcopy(session.store.entries)
    assert session.context_tokens() >= len(json.dumps(current, ensure_ascii=False)) // 4
    assert retained(session.main) == current
    assert session.store.entries == before
    if persist:
        session = make_session(session=session.store.path, settings={"compaction": {"keepRecentTokens": 1}})
    assert retained(session.main) == current
    faux.push("test", faux.reply("continued " * 1000))
    session.prompt("continue")
    assert current["todos"][1]["content"] in calls()[-1]["system"]
    summarize(session)
    assert retained(session.main) == current


def test_parallel_todos_have_no_size_cap_or_stop_continuation(make_session, todo_extension):
    todos = [item(f"task {i}", "in_progress", "StepA" if i < 30 else "StepAA") for i in range(60)]
    session = make_session(
        faux.reply(tools=[("todo_write", {"todos": todos})]), faux.reply("waiting for results"),
    )
    session.prompt("track parallel work")
    result, = tool_results(session)
    assert not result.is_error and result.details["todos"] == todos
    assert result.text() == "Todos updated."
    assert len(calls()) == 2
    assert "<todos_state>" not in calls()[-1]["system"]
    assert not any(isinstance(m, UserMessage) and m.kind == "continue" for m in session.main.messages)


@pytest.mark.parametrize("invalid", [
    [item("new"), item("  ")],
    [item("new", "unknown")],
    [None],
    "not a list",
    [item("bad step", step="Step1")],
    [item("bad step", step="StepA\n")],
])
def test_invalid_todos_leave_previous_state_intact(make_session, todo_extension, invalid):
    original = {"todos": [item("keep")], "explanation": "original reason"}
    session = make_session(
        faux.reply(tools=[("todo_write", original)]),
        faux.reply(tools=[("todo_write", {"todos": invalid})]), faux.reply("done " * 1000),
        settings={"compaction": {"keepRecentTokens": 1}},
    )
    session.prompt("record work")
    assert tool_results(session)[1].is_error
    summarize(session)
    assert retained(session.main) == original


def test_todos_replace_restore_and_display(make_session, todo_extension):
    """Keep the historical scenario identity; display is the ordinary tool transcript."""
    old = {"todos": [item("obsolete")], "explanation": "old reason"}
    current = {"todos": [item("keep", "completed"), item("next", "in_progress", "StepB")]}
    session = make_session(
        faux.reply(tools=[("todo_write", old), ("todo_write", current)]), faux.reply("done " * 1000),
        persist=True, settings={"compaction": {"keepRecentTokens": 1}},
    )
    session.prompt("record work")
    assert tool_results(session)[-1].details["todos"] == current["todos"]
    assert "explanation" not in tool_results(session)[-1].details
    summarize(session)
    restored = make_session(session=session.store.path, settings={"compaction": {"keepRecentTokens": 1}})
    assert retained(restored.main) == current
    restored.prompt("/todos")
    assert "removed" in restored.ui.text.lower()
    assert calls() == []
    for todos in ([item("finished", "completed")], []):
        faux.push("test", faux.reply(tools=[("todo_write", {"todos": todos})]), faux.reply("updated " * 1000))
        restored.prompt("update the list")
        assert retained(restored.main) is None
        summarize(restored)
        assert retained(restored.main) is None


def test_todo_extension_loading_and_permissions(make_session, broker_session, todo_extension, project):
    disabled = make_session(extensions=False, settings={"plugins": {"todo": {"enabled": True}}, "features": {"todo": True}})
    assert disabled.tool("todo_write") is None
    assert any("todo extension" in notice for notice in disabled.settings.migration_warnings)
    user_extension = Path(os.environ["VIRGO_HOME"]) / "extensions" / "todo.py"
    user_extension.unlink()
    absent = make_session()
    assert absent.tool("todo_write") is None
    directory = project / ".virgo" / "extensions"
    directory.mkdir(parents=True)
    shutil.copy(todo_extension, directory / "todo.py")
    untrusted = make_session(trust_project_extensions=False)
    assert untrusted.tool("todo_write") is None
    session = broker_session(
        faux.reply(tools=[("todo_write", {"todos": [item("read-only checklist")]})]), faux.reply("done"),
        preset="read-only",
    )
    session.prompt("track work")
    assert not tool_results(session)[-1].is_error and not session.ui.questions
    assert session.commands.get("todos") is None and session.commands.get("plan") is None
    assert "todo_write" not in {t.name for t in session.tools_for("subagent")}
    with pytest.raises(ValueError, match=r"permissions\.deny is retired"):
        make_session(settings={"permissions": {"deny": ["todo_write"]}})


def test_retired_todo_name_does_not_exempt_write_tools(broker_session):
    session = broker_session(faux.reply(tools=[("todo_write", {})]), faux.reply("done"), preset="read-only")
    executed = []
    ExtensionAPI(session, "test").register_tool(AgentTool(
        "todo_write", "An unrelated write tool", {"type": "object"}, lambda *_: (executed.append(True), ToolResult.text("executed"))[1],
        side_effect="write",
    ), roles=("main",))
    session.prompt("try the write")
    assert tool_results(session)[-1].is_error and not executed


@pytest.mark.parametrize("reason", ["auto", "overflow"])
def test_todos_survive_automatic_and_overflow_compaction(make_session, todo_extension, reason):
    current = {"todos": [item("preserve me")]}
    session = make_session(
        faux.reply(tools=[("todo_write", current)]), faux.reply("work " * 1000),
        settings={"compaction": {"keepRecentTokens": 1}},
    )
    session.prompt("record work")
    if reason == "auto":
        session.settings.set("compaction.triggerTokens", 1)
    else:
        faux.push("test", faux.error("context overflow", kind="overflow"))
    faux.push("test", faux.reply("Summary without todos"), faux.reply("continue"))
    session.prompt("keep working")
    assert session.store.last_compaction()["reason"] == reason
    assert retained(session.main) == current
    assert current["todos"][0]["content"] in calls()[-1]["system"]


@pytest.mark.parametrize("spec", ["openai/test", "openai-codex/test", "anthropic/claude-opus-5-5"])
def test_todos_survive_native_compaction_and_provider_switch(make_session, todo_extension, monkeypatch, spec):
    current = {"todos": [item("native state survives")], "explanation": "exact explanation"}
    session = make_session(
        faux.reply(tools=[("todo_write", current)]), faux.reply("work " * 1000),
        persist=True, settings={"compaction": {"keepRecentTokens": 1}},
    )
    session.prompt("record work")
    session.set_model(spec)
    api = native_api(session.model)
    payload = {"type": "compaction", **({"content": "Summary without todos", "signature": "signed"}
                                          if api == "anthropic" else {"encrypted_content": "opaque", "id": "cmp_1"})}

    def native(model, *args, **kwargs):
        return AssistantMessage(api=api, provider=model.provider, model=model.id, stop_reason="compaction",
                                content=[RawContent(api, copy.deepcopy(payload))])

    monkeypatch.setattr(compaction, "native_compact", native)
    assert session.compact() is not None
    assert retained(session.main) == current
    assert session.main.messages[0].content[0].data == payload
    restored = make_session(session=session.store.path, settings={"compaction": {"keepRecentTokens": 1}})
    restored.set_model(spec)
    assert retained(restored.main) == current
    assert restored.compact() is not None or api != "anthropic"
    assert retained(restored.main) == current
    assert restored.main.messages[0].content[0].data == payload
    restored.set_model("faux/test")
    assert restored.main.restore_native_context()
    assert retained(restored.main) is None
    assert any(m.details.get("todos") == current["todos"] for m in tool_results(restored))


def test_todos_follow_conversation_and_active_branch(make_session, todo_extension):
    original = {"todos": [item("first conversation")]}
    session = make_session(
        faux.reply(tools=[("todo_write", original)]), faux.reply("work " * 1000),
        persist=True, settings={"compaction": {"keepRecentTokens": 1}},
    )
    session.prompt("record work")
    summarize(session)
    first, first_path = session.main, session.store.path
    session.new_session()
    second_path = session.store.path
    assert retained(session.main) is None
    assert retained(first) == original
    faux.push("test", faux.reply(tools=[("todo_write", {"todos": [item("second conversation")]})]), faux.reply("work " * 1000))
    session.prompt("record another task")
    summarize(session)
    assert retained(session.main)["todos"][0]["content"] == "second conversation"
    original = {"todos": [item("first conversation updated while second is selected")]}
    faux.push("test", faux.reply(tools=[("todo_write", original)]), faux.reply("work " * 1000),
              faux.reply("Summary without todos"))
    first.agent.prompt([UserMessage("update the originating conversation")])
    assert first.compact() is not None
    assert retained(first) == original
    assert retained(session.main)["todos"][0]["content"] == "second conversation"
    branch_point = first.store.leaf
    session.open_session(first_path)
    assert retained(session.main) == original
    faux.push("test", faux.reply(tools=[("todo_write", {"todos": [item("discarded branch")]})]), faux.reply("work " * 1000))
    session.prompt("change first task")
    session.store.append("leaf", target=branch_point)
    session.open_session(second_path)
    restored = make_session(session=first_path)
    assert retained(restored.main) == original


def test_legacy_todos_restore_without_inventing_steps(make_session, todo_extension):
    session = make_session(persist=True)
    legacy = {"todos": [{"content": "legacy task", "status": "pending"}]}
    session.store.append_message(ToolResultMessage("old", "todo_write", [TextContent("old list")], details=legacy))
    session.store.append("compaction", summary="Summary without todos", kept_ids=[])
    restored = make_session(session=session.store.path)
    assert retained(restored.main) == legacy


def test_retained_tool_pair_avoids_duplicate_snapshot(make_session, todo_extension):
    session = make_session(persist=True)
    value = {"todos": [item("already visible")]}
    session.store.append("compaction", summary="Older conversation", kept_ids=[])
    session.store.append_message(AssistantMessage([ToolCall("visible", "todo_write", value)]))
    session.store.append_message(ToolResultMessage("visible", "todo_write", [TextContent("Todos 状态更新成功")], details=value))
    restored = make_session(session=session.store.path)
    assert retained(restored.main) is None


def test_invalid_explanation_and_failed_compaction_preserve_state(make_session, todo_extension):
    value = {"todos": [item("keep this")], "explanation": "keep this reason"}
    session = make_session(
        faux.reply(tools=[("todo_write", value)]),
        faux.reply(tools=[("todo_write", {"todos": [], "explanation": ["not", "a", "string"]})]),
        faux.reply("work " * 1000),
        settings={"compaction": {"keepRecentTokens": 1}},
    )
    session.prompt("record work")
    assert tool_results(session)[-1].is_error
    faux.push("test", faux.error("summary failed", kind="fatal"))
    assert session.compact() is None
    assert retained(session.main) is None
    summarize(session)
    assert retained(session.main) == value


def test_optional_step_null_explanation_coercion_and_language_ack(make_session, todo_extension):
    # Optional step, explicit null explanation, and JSON-stringified todos all work via validate().
    raw_todos = json.dumps([{"content": "linear task", "status": "pending"}])
    session = make_session(
        faux.reply(tools=[("todo_write", {"todos": raw_todos, "explanation": None})]),
        faux.reply("done"),
    )
    session.prompt("track simple work")
    result, = tool_results(session)
    assert not result.is_error
    assert result.text() == "Todos updated."
    assert result.details["todos"] == [{"content": "linear task", "status": "pending"}]
    assert "explanation" not in result.details

    zh_session = make_session(
        faux.reply(tools=[("todo_write", {"todos": [{"content": "中文任务", "status": "in_progress"}]})]),
        faux.reply("完成"),
        settings={"language": "zh-CN"},
    )
    zh_session.prompt("记录任务")
    zh_result, = tool_results(zh_session)
    assert not zh_result.is_error
    assert zh_result.text() == "Todos 状态更新成功"
