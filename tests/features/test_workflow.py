"""Workflow scripts run agents in parallel and resume from their journal."""

from __future__ import annotations

import textwrap
import threading

import pytest

from virgo.ai.providers import faux
from virgo.features.workflow import WorkflowError, run_workflow
from virgo.harness.extensions import ExtensionAPI

from tests.support.harness import calls


def test_workflow_parallel_and_resume(make_session, project):
    wf = project / ".virgo" / "workflows"
    wf.mkdir(parents=True)
    (wf / "fanout.py").write_text(
        textwrap.dedent("""
        META = {"name": "fanout", "description": "score items"}
        SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}
        def run(ctx, args):
            ctx.phase("scoring")
            scores = ctx.map(args["items"], lambda i: ctx.agent(f"score {i}", schema=SCHEMA, label=i)["score"])
            if args.get("fail_after"):
                raise RuntimeError("boom")
            return {"total": sum(scores)}
    """)
    )
    attempts = {"n": 0}

    def respond(ctx):
        text = str(ctx.messages[-1].content)
        if "score" in text and "workflow step" in ctx.system:
            attempts["n"] += 1
            n = int(text.split("score ")[1].split()[0])
            return faux.reply("not json" if n == 2 and attempts["n"] < 3 else f'{{"score": {n}}}')
        return faux.reply("done")

    s = make_session()
    faux.handler("test", respond)
    from virgo.features.workflow import run_workflow
    from virgo.harness.extensions import ExtensionAPI

    api = ExtensionAPI(s, "test")
    with pytest.raises(Exception, match="resume with"):
        run_workflow(api, "fanout", {"items": [1, 2, 3], "fail_after": True}, None, threading.Event())
    run_id = sorted((project / ".virgo" / "runs").glob("*.jsonl"))[0].stem
    before = len(calls())
    # resuming replays from the journal: no new model calls, still fails at the same point
    with pytest.raises(Exception, match="boom"):
        run_workflow(api, None, None, run_id, threading.Event())
    assert len(calls()) == before
    out = run_workflow(api, "fanout", {"items": [1, 2, 3]}, None, threading.Event())
    assert out["result"] == {"total": 6} and out["agents"] == 3


def test_unknown_workflow_or_escaping_run_id_leaves_no_journal(make_session, project):
    api = ExtensionAPI(make_session(), "test")
    with pytest.raises(WorkflowError, match="unknown workflow"):
        run_workflow(api, "missing", {}, None, threading.Event())
    assert not (project / ".virgo" / "runs").exists()
    with pytest.raises(WorkflowError, match="no run"):
        run_workflow(api, None, None, "../escape", threading.Event())


def test_workflow_tool_steps_do_not_register_agent_types(make_session, project):
    flows = project / ".virgo" / "workflows"
    flows.mkdir(parents=True)
    (flows / "probe.py").write_text("def run(ctx, args):\n    return ctx.agent('look around', tools=['read'])\n")
    session = make_session()
    faux.handler("test", lambda ctx: faux.reply("looked"))
    out = run_workflow(ExtensionAPI(session, "test"), "probe", {}, None, threading.Event())
    assert out["result"] == "looked"
    assert not any(name.startswith("wf:") for name in session.services["subagents"].types)
