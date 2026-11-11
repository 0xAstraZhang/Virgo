"""Workflows: Python scripts own a fixed orchestration; the model only launches them.

A workflow is ``.virgo/workflows/<name>.py`` (or ``~/.virgo/workflows``)::

    META = {"name": "review-changes", "description": "Review the working tree diff"}

    def run(ctx, args):
        diff = ctx.agent("Summarize `git diff` in this repo.", tools="read-only")
        dims = ["correctness", "security", "tests"]
        reviews = ctx.map(dims, lambda d: ctx.agent(f"Review this diff for {d}:\\n{diff}",
                                                   schema=FINDINGS, label=f"review-{d}"))
        return {"findings": reviews}

Every ``ctx.agent`` result is journaled (``.virgo/runs/<run_id>.jsonl``) under a hash of the call, so a
failed or interrupted run can be resumed: the script re-runs from the top and finished calls return
instantly from the journal.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..ai.types import UserMessage
from ..ai.validate import parse_json_loose, validate
from ..harness.extensions import ExtensionAPI
from ..tools.truncate import budget


class WorkflowError(Exception):
    pass


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, Any] = {}
        self.header: dict = {}
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: Path, header: dict | None = None) -> Journal:
        j = cls(path)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                if e["type"] == "run":
                    j.header = e
                elif e["type"] == "agent":
                    j.entries[e["key"]] = e["value"]
        elif header is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            j.header = {"type": "run", **header}
            j._append(j.header)
        return j

    def _append(self, obj: dict) -> None:
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")

    def record(self, key: str, value: Any) -> None:
        self.entries[key] = value
        self._append({"type": "agent", "key": key, "value": value})

    def finish(self, status: str, result: Any) -> None:
        self._append({"type": status, "result": result, "time": time.time()})


class Context:
    def __init__(self, api: ExtensionAPI, run_id: str, journal: Journal, args: dict, signal: threading.Event):
        self.api = api
        self.session = api.session
        self.run_id = run_id
        self.journal = journal
        self.args = args
        self.signal = signal
        self.stats = {"agents": 0, "cached": 0}
        self._lock = threading.Lock()
        s = api.settings
        self.max_agents = int(s.get("workflow.maxAgents", 200))
        self.concurrency = int(s.get("workflow.concurrency", 8))

    # -- primitives -----------------------------------------------------------
    def log(self, msg: str) -> None:
        self.api.ui.print(f"  [workflow {self.run_id}] {msg}", "agent")

    def phase(self, title: str) -> None:
        self.log(f"== {title} ==")

    def agent(
        self,
        prompt: str,
        schema: dict | None = None,
        tools: str | list | None = None,
        label: str | None = None,
        system: str | None = None,
    ) -> Any:
        if self.signal.is_set():
            raise WorkflowError("aborted")
        key = hashlib.sha256(
            json.dumps([label, prompt, schema, tools, system], sort_keys=True, default=str).encode()
        ).hexdigest()[:20]
        if key in self.journal.entries:
            with self._lock:
                self.stats["cached"] += 1
            return self.journal.entries[key]
        with self._lock:
            if self.stats["agents"] >= self.max_agents:
                raise WorkflowError(f"agent call limit ({self.max_agents}) reached")
            self.stats["agents"] += 1
        self.log(f"agent: {label or prompt[:60]}")
        value = self._run_agent(prompt, schema, tools, system)
        self.journal.record(key, value)
        return value

    def parallel(self, thunks: list[Callable[[], Any]]) -> list:
        """Run callables concurrently; waits for all, then raises the first error (if any)."""
        with ThreadPoolExecutor(max_workers=max(1, min(self.concurrency, len(thunks)))) as pool:
            futures = [pool.submit(t) for t in thunks]
            results, errors = [], []
            for f in futures:
                try:
                    results.append(f.result())
                except Exception as e:  # noqa: BLE001
                    results.append(None)
                    errors.append(e)
        if errors:
            raise errors[0]
        return results

    def map(self, items: list, fn: Callable[[Any], Any]) -> list:
        return self.parallel([lambda i=i: fn(i) for i in items])

    # -- internals ----------------------------------------------------------------
    def _run_agent(self, prompt: str, schema: dict | None, tools, system: str | None) -> Any:
        instructions = prompt
        if schema:
            instructions += (
                "\n\nReply with only a JSON value matching this JSON Schema (no prose, no code fences):\n"
                + json.dumps(schema)
            )
        for _attempt in range(2):
            if tools:
                text = self._with_tools(instructions, tools)
            else:
                sys_prompt = (
                    system
                    or "You are a focused workflow step. Complete exactly the requested step and return only the result."
                )
                msg = self.session.complete(
                    "workflow",
                    sys_prompt,
                    instructions,
                    model=self.session.model,
                    effort=self.session.effort,
                    signal=self.signal,
                )
                if msg.stop_reason in ("error", "refusal", "aborted"):
                    raise WorkflowError(f"model call failed: {msg.error_message}")
                text = msg.text()
            if not schema:
                return text
            try:
                value, errors = validate(parse_json_loose(text), schema)
            except ValueError as e:
                errors, value = [str(e)], None
            if not errors:
                return value
            instructions = f"{prompt}\n\nYour previous reply was invalid ({'; '.join(errors[:3])}). Return only valid JSON matching the schema:\n{json.dumps(schema)}"
        raise WorkflowError("the model did not return valid JSON for the schema")

    def _with_tools(self, prompt: str, tools) -> str:
        subs = self.session.services.get("subagents")
        if subs is None:
            raise WorkflowError("tools in workflows need the subagent feature")
        type_name = "explore" if tools == "read-only" else "general"
        if isinstance(tools, list):
            from .subagent import AgentType

            subs.types.setdefault(
                f"wf:{','.join(tools)}", AgentType(f"wf:{','.join(tools)}", "workflow step", tools=tools)
            )
            type_name = f"wf:{','.join(tools)}"
        res = subs.run(prompt, type_name, f"workflow {self.run_id}", self.signal)
        if res.is_error:
            raise WorkflowError(res.as_text())
        return res.as_text()


def discover(api: ExtensionAPI) -> dict[str, Path]:
    out = {}
    for d in api.paths.both("workflows"):
        if d.is_dir():
            for f in sorted(d.glob("*.py")):
                out[f.stem] = f
    return out


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"virgo_workflow_{path.stem}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "run", None)):
        raise WorkflowError(f"{path} has no run(ctx, args) function")
    return mod


def run_workflow(
    api: ExtensionAPI, name: str | None, args: dict | None, resume: str | None, signal: threading.Event
) -> dict:
    runs = api.paths.project / "runs"
    if resume:
        path = runs / f"{resume}.jsonl"
        if not path.exists():
            raise WorkflowError(f"no run {resume}")
        journal = Journal.open(path)
        name = journal.header.get("workflow")
        if args and args != journal.header.get("args"):
            raise WorkflowError("resume uses the original args; omit args")
        args, run_id = journal.header.get("args") or {}, resume
    else:
        run_id = f"{name}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        journal = Journal.open(
            runs / f"{run_id}.jsonl", {"workflow": name, "args": args or {}, "started": time.time()}
        )
        args = args or {}
    flows = discover(api)
    if name not in flows:
        raise WorkflowError(f"unknown workflow {name!r}. Available: {', '.join(flows) or 'none'}")
    mod = load_module(flows[name])
    ctx = Context(api, run_id, journal, args, signal)
    try:
        result = mod.run(ctx, args)
    except Exception as e:
        journal.finish("failed", str(e))
        raise WorkflowError(f"{type(e).__name__}: {e} (resume with resume='{run_id}')") from e
    journal.finish("done", result)
    return {"run_id": run_id, "result": result, **ctx.stats}


def setup(api: ExtensionAPI) -> None:
    def execute(args: dict, tctx: ToolContext) -> ToolResult:
        try:
            out = run_workflow(api, args.get("name"), args.get("args"), args.get("resume"), tctx.signal)
        except (WorkflowError, json.JSONDecodeError) as e:
            return ToolResult.error(f"Workflow failed: {e}")
        text = (
            json.dumps(out["result"], indent=2, ensure_ascii=False, default=str)
            if not isinstance(out["result"], str)
            else out["result"]
        )
        head = f"Workflow run {out['run_id']} finished ({out['agents']} agent calls, {out['cached']} from journal).\n"
        return ToolResult.text(head + budget(text, tctx.cwd, out["run_id"]), details=out | {"result": None})

    flows = discover(api)

    def section(role: str):
        if role != "main" or not flows:
            return None
        lines = []
        for name, path in flows.items():
            try:
                meta = getattr(load_module(path), "META", {})
            except Exception:  # noqa: BLE001
                meta = {}
            lines.append(f"- {name}: {meta.get('description', '')}")
        return (
            "<workflows>\nScripted workflows you can launch with the workflow tool:\n"
            + "\n".join(lines)
            + "\n</workflows>"
        )

    api.add_system_section("workflows", section, order=35)
    api.register_tool(
        AgentTool(
            "workflow",
            "Run a scripted multi-agent workflow by name (see <workflows>). The script fixes the orchestration; "
            "pass args as an object. If a run fails, call again with resume=<run_id> to continue where it stopped.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "args": {"type": "object"},
                    "resume": {"type": "string"},
                },
            },
            execute,
            snippet="run a scripted multi-agent workflow (resumable)",
            read_only=True,  # each step is permission-checked
        ),
        roles=("main",),
    )

    def cmd(session, a):
        parts = a.split(maxsplit=1)
        if parts and parts[0] == "resume" and len(parts) == 2:
            session.run(
                [UserMessage(f"Resume workflow run {parts[1]} with the workflow tool (resume='{parts[1]}').")]
            )
            return
        if not flows:
            api.ui.print("No workflows. Add .virgo/workflows/<name>.py with run(ctx, args).")
            return
        api.ui.print("\n".join(f"  {n}  ({p})" for n, p in flows.items()))
        runs = sorted(
            (api.paths.project / "runs").glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:5]
        if runs:
            api.ui.print("recent runs:\n" + "\n".join(f"  {r.stem}" for r in runs))

    api.register_command("workflow", cmd, "list workflows and recent runs (/workflow resume <run_id>)")
