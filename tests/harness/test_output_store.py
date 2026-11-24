"""Tests for session-persisted Broker OutputStore, piped shell capture, and read_output (S3-09 / VER-08)."""
from __future__ import annotations

import os
import time

import pytest

from virgo.ai.providers import faux
from virgo.execution import HostContext
from virgo.i18n import Translator
from virgo.output_store import OutputStore
from virgo.tools.output import MAX_BYTES, MAX_LINES, CommandOutput, budget, display_output, save_output

from tests.support.harness import tool_results
from tests.support.sandbox import python_command, run


@pytest.mark.parametrize(("language", "notice"), [("en", "Output truncated"), ("zh-CN", "输出已截断")],
                         ids=["en", "zh-CN"])
def test_display_output_reports_custom_byte_budget(language, notice):
    output = CommandOutput("界" * 3000, truncated=True, total_bytes=9000)
    preview = display_output(output, max_bytes=4096, t=Translator(language))
    heading, tail = preview.split("\n", 1)
    assert notice in heading and "4KB" in heading
    assert len(preview.encode()) <= 4096 and tail and output.text.endswith(tail)


@pytest.mark.parametrize("language", ["en", "zh-CN"])
@pytest.mark.parametrize("keep", ["head", "tail"])
@pytest.mark.parametrize("original", [
    "row \n" * (MAX_LINES + 1),
    "🚀界" * (MAX_BYTES // 7 + 1),
], ids=["lines", "utf8-bytes"])
def test_budget_includes_localized_notice_and_id(tmp_path, original, keep, language):
    ctx = HostContext.unrestricted(tmp_path, outputs=OutputStore(tmp_path / "records"))
    t = Translator(language)
    preview = budget(original, tmp_path, "test", keep, t=t, execution_context=ctx)
    assert len(preview.encode()) <= MAX_BYTES
    assert len(preview.splitlines()) <= MAX_LINES
    [path] = ctx.outputs.directory.glob("*.log")
    assert path.read_bytes() == original.encode()
    body, _ = preview.rsplit("\n\n", 1)
    assert body and (original.startswith(body) if keep == "head" else body in original)
    assert preview == t("tools.output.budget", shown=body, count=len(body.splitlines()),
                        where=t("tools.output.first" if keep == "head" else "tools.output.last"),
                        total=len(original.splitlines()), path=path.stem)


def test_ver08_read_output_survives_session_resume(make_session, project):
    """VER-08: Output spilled in a persisted session remains readable after session resume."""
    payload = "line-content-" + ("你好世界🚀" * 10000)
    first = make_session(session="new", extensions=False, refresh_models=False)
    try:
        record = save_output(payload, project, "shell", execution_context=first.main.execution_context)
        output_id = str(record)
        session_path = first.store.path
        assert session_path is not None
        # Ensure the session file is non-empty so shutdown retains it.
        faux.script("test", faux.reply("saved"))
        first.prompt("keep session")
    finally:
        first.shutdown()

    resumed = make_session(session=session_path, extensions=False, refresh_models=False)
    try:
        collected = []
        offset = 0
        while True:
            faux.script(
                "test",
                faux.reply(tools=[("read_output", {"output_id": output_id, "offset": offset, "limit": 4096})]),
                faux.reply("done"),
            )
            resumed.prompt("read output page")
            res = tool_results(resumed)[-1]
            assert not res.is_error, res.text()
            text = res.text()
            assert "\ufffd" not in text, "UTF-8 boundary alignment must not split multibyte characters"
            collected.append(text)
            next_offset = res.details["next_offset"]
            if next_offset is None:
                break
            offset = next_offset
        assert "".join(collected) == payload
    finally:
        resumed.shutdown()


def test_s309_untruncated_command_leaves_no_log_files(make_session, project):
    """S3-09: Normal un-truncated foreground commands must not leave leftover .log files."""
    session = make_session(
        session="new", settings={"execution": {"mode": "no-limit"}},
        extensions=False, refresh_models=False,
    )
    try:
        store = session.main.execution_context.outputs
        result = run(session, python_command(session, "print('hello-short-output')"))
        assert not result.is_error and "hello-short-output" in result.text()
        assert result.details["output_id"] is None
        logs = list(store.directory.glob("*.log")) if store.directory.exists() else []
        assert logs == [], f"Unexpected leftover log files: {logs}"
    finally:
        session.shutdown()


def test_s309_truncated_command_persists_record_and_trash_cleans_up(make_session, project, trash):
    """S3-09: Truncated command persists an output_id; trashing the session deletes its outputs."""
    session = make_session(
        session="new", settings={"execution": {"mode": "no-limit"}},
        extensions=False, refresh_models=False,
    )
    try:
        session_path = session.store.path
        assert session_path is not None
        out_dir = session.main.execution_context.outputs.directory
        result = run(session, python_command(session, f"print('x' * {MAX_BYTES + 4096})"))
        assert not result.is_error and result.details["truncated"] is True
        output_id = result.details["output_id"]
        assert output_id and output_id.startswith("out_")
        assert list(out_dir.glob("*.log"))

        faux.script(
            "test",
            faux.reply(tools=[("read_output", {"output_id": output_id, "offset": 0, "limit": 100})]),
            faux.reply("done"),
        )
        session.prompt("inspect saved output")
        page = tool_results(session)[-1]
        assert not page.is_error and page.text() == "x" * 100

        session.trash_session(session_path)
        assert not out_dir.exists()
    finally:
        session.shutdown()


def test_s309_utf8_pagination_and_ttl_and_cross_session_isolation(tmp_path):
    """S3-09: UTF-8 boundary alignment, TTL pruning, and cross-session output isolation."""
    outputs_root = tmp_path / "outputs"
    store_a = OutputStore(outputs_root / "session-a")
    store_b = OutputStore(outputs_root / "session-b")

    sample = "中文测试🎉🚀αβγ" * 50
    rec, pipe = store_a.create("test")
    with pipe:
        pipe.write(sample.encode("utf-8"))

    # Read with small odd byte limit (7 bytes) to force cuts inside 3-byte and 4-byte UTF-8 chars.
    chunks = []
    offset = 0
    while True:
        data, total, start = store_a.read(rec, offset=offset, limit=7, align_utf8=True)
        decoded = data.decode("utf-8")  # Must decode strictly without errors!
        chunks.append(decoded)
        end = start + len(data)
        if end >= total:
            break
        offset = end
    assert "".join(chunks) == sample

    # Cross-session isolation.
    from virgo.execution import FileAccessDenied
    try:
        store_b.lookup(rec.id)
    except FileAccessDenied:
        pass
    else:
        raise AssertionError("Session B must not look up Session A's output record")

    # TTL pruning removes old session dirs while preserving recent ones.
    old_dir = outputs_root / "session-expired"
    old_dir.mkdir(parents=True)
    (old_dir / "old.log").write_text("stale")
    old_time = time.time() - 3600 * 48
    os.utime(old_dir, (old_time, old_time))
    removed = OutputStore.prune_expired(outputs_root, max_age_seconds=3600)
    assert removed == 1
    assert not old_dir.exists()
    assert (outputs_root / "session-a").exists()


def test_read_output_search_tail_and_line_paging(make_session, project):
    lines = [f"line-{i:03d} payload" for i in range(1, 26)]
    lines[4] = "line-005 ERROR: failed on item[0]"
    lines[11] = "line-012 warning: retry later"
    lines[19] = "line-020 error: second item[0] failure"
    payload = "\n".join(lines) + "\n"

    session = make_session(session="new", extensions=False, refresh_models=False)
    try:
        record = save_output(payload, project, "pytest", execution_context=session.main.execution_context)
        oid = str(record)
        faux.script(
            "test",
            faux.reply(tools=[
                ("read_output", {"output_id": oid, "pattern": "item[0]", "fixed_strings": True, "context": 1}),
                ("read_output", {"output_id": oid, "pattern": "error", "ignore_case": True, "max_matches": 1}),
                ("read_output", {"output_id": oid, "pattern": "error", "ignore_case": True, "line_offset": 6}),
                ("read_output", {"output_id": oid, "tail": True, "line_limit": 3}),
                ("read_output", {"output_id": oid, "line_offset": 10, "line_limit": 3}),
                ("read_output", {"output_id": oid, "pattern": "nonexistent-needle"}),
            ]),
            faux.reply("done"),
        )
        session.prompt("inspect saved test output")
        lit, first_hit, second_hit, tail_res, page_res, miss = tool_results(session)[-6:]

        assert not lit.is_error
        assert lit.text().splitlines() == [
            "4-line-004 payload",
            "5:line-005 ERROR: failed on item[0]",
            "6-line-006 payload",
            "--",
            "19-line-019 payload",
            "20:line-020 error: second item[0] failure",
            "21-line-021 payload",
        ]
        assert lit.details["matches"] == 2 and lit.details["truncated"] is False

        assert not first_hit.is_error
        assert "5:line-005 ERROR" in first_hit.text()
        assert first_hit.details["truncated"] is True
        assert first_hit.details["next_line_offset"] == 20

        assert not second_hit.is_error
        assert second_hit.text() == "20:line-020 error: second item[0] failure"
        assert second_hit.details["truncated"] is False

        assert not tail_res.is_error
        assert tail_res.text().startswith("23|line-023 payload\n24|line-024 payload\n25|line-025 payload\n")
        assert "23-25 of 25" in tail_res.text()
        assert tail_res.details["lines"] == [23, 25]

        assert not page_res.is_error
        assert page_res.text().startswith("10|line-010 payload\n11|line-011 payload\n12|line-012 warning: retry later\n")
        assert page_res.details["next_line_offset"] == 13

        assert not miss.is_error and miss.text() == "(no matches)"
    finally:
        session.shutdown()


def test_read_output_tail_bytes_aligns_utf8_and_validates_conflicts(make_session, project):
    payload = "开头" + ("中文测试🚀" * 100)
    session = make_session(session="new", extensions=False, refresh_models=False)
    try:
        record = save_output(payload, project, "utf8", execution_context=session.main.execution_context)
        oid = str(record)
        faux.script(
            "test",
            faux.reply(tools=[
                ("read_output", {"output_id": oid, "tail": True, "limit": 17}),
                ("read_output", {"output_id": oid, "pattern": "测试", "tail": True}),
                ("read_output", {"output_id": oid, "offset": 0, "tail": True}),
                ("read_output", {"output_id": oid, "tail": True, "line_offset": 2}),
                ("read_output", {"output_id": oid, "ignore_case": True}),
            ]),
            faux.reply("done"),
        )
        session.prompt("check tail utf8 and parameter validation")
        tail_bytes, bad_pat, bad_off, bad_tail, bad_flags = tool_results(session)[-5:]

        assert not tail_bytes.is_error
        assert "\ufffd" not in tail_bytes.text()
        assert payload.endswith(tail_bytes.text()) and tail_bytes.details["tail"] is True
        assert bad_pat.is_error and "pattern" in bad_pat.text()
        assert bad_off.is_error and "offset" in bad_off.text()
        assert bad_tail.is_error and "tail" in bad_tail.text()
        assert bad_flags.is_error and "pattern" in bad_flags.text()
    finally:
        session.shutdown()


def test_p0_tool_descriptions_and_params_localize_to_chinese(broker_session):
    session = broker_session(preset="read-only")
    en, zh = Translator("en"), Translator("zh-CN")
    for name in ("read", "glob", "grep", "read_output"):
        tool = session.tool(name)
        assert tool is not None
        en_spec, zh_spec = tool.spec(en), tool.spec(zh)
        assert zh_spec.description != en_spec.description, f"{name}.description was not localized"
        for prop, schema in en_spec.parameters["properties"].items():
            if "description" in schema:
                assert zh_spec.parameters["properties"][prop]["description"] != schema["description"], (
                    f"{name}.{prop} description was not localized"
                )


def test_read_output_strict_byte_budget_on_first_line_across_modes(make_session, project):
    session = make_session(session="new", extensions=False, refresh_models=False)
    try:
        # 1. Line paging: first line near MAX_BYTES followed by another line must not exceed MAX_BYTES
        #    or get re-spilled by AgentSession._after_tool.
        near_max_payload = ("a" * (MAX_BYTES - 4)) + "\nsecond line\n"
        rec_lines = save_output(near_max_payload, project, "lines", execution_context=session.main.execution_context)
        # 2. Search mode: small byte limit where first match fits byte_limit alone but not with search_more footer.
        search_payload = f"match-{'x' * 180}\nmatch-second\n"
        rec_search = save_output(search_payload, project, "search", execution_context=session.main.execution_context)
        # 3. Tail lines: last line fits byte_limit alone but not with tail_lines footer.
        tail_payload = "first line\n" + ("b" * 180) + "\n"
        rec_tail = save_output(tail_payload, project, "tail", execution_context=session.main.execution_context)

        faux.script(
            "test",
            faux.reply(tools=[
                ("read_output", {"output_id": str(rec_lines), "line_limit": 1}),
                ("read_output", {"output_id": str(rec_search), "pattern": "match", "limit": 200}),
                ("read_output", {"output_id": str(rec_tail), "tail": True, "line_limit": 1, "limit": 200}),
            ]),
            faux.reply("done"),
        )
        session.prompt("check strict byte budgets")
        line_res, search_res, tail_res = tool_results(session)[-3:]

        assert line_res.is_error
        assert len(line_res.text().encode("utf-8")) <= MAX_BYTES
        assert "Output truncated:" not in line_res.text()

        assert not search_res.is_error
        assert len(search_res.text().encode("utf-8")) <= 200
        assert search_res.details["truncated"] is True

        assert not tail_res.is_error
        assert len(tail_res.text().encode("utf-8")) <= 200
        assert tail_res.details["lines"] == [2, 2]
    finally:
        session.shutdown()


def test_read_output_line_paging_eof_and_single_near_limit_line(make_session, project):
    session = make_session(session="new", extensions=False, refresh_models=False)
    try:
        rec_nl = save_output("a\nb\nc\n", project, "nl", execution_context=session.main.execution_context)
        rec_nonl = save_output("a\nb\nc", project, "nonl", execution_context=session.main.execution_context)
        rec_exact = save_output("a" * (MAX_BYTES - 4), project, "exact", execution_context=session.main.execution_context)
        faux.script(
            "test",
            faux.reply(tools=[
                ("read_output", {"output_id": str(rec_nl), "line_offset": 4}),
                ("read_output", {"output_id": str(rec_nonl), "line_offset": 4}),
                ("read_output", {"output_id": str(rec_nonl), "line_offset": 3}),
                ("read_output", {"output_id": str(rec_exact), "line_limit": 1}),
            ]),
            faux.reply("done"),
        )
        session.prompt("check line paging EOF boundaries")
        past_nl, past_nonl, last_nonl, exact_one = tool_results(session)[-4:]

        assert past_nl.is_error and "(3 lines)" in past_nl.text()
        assert past_nonl.is_error and "(3 lines)" in past_nonl.text()
        assert not last_nonl.is_error and last_nonl.text() == "3|c" and last_nonl.details["next_line_offset"] is None
        assert not exact_one.is_error and len(exact_one.text().encode("utf-8")) == MAX_BYTES - 2
    finally:
        session.shutdown()


def test_read_output_tail_lines_bounds_memory_on_wide_lines(make_session, project):
    import tracemalloc

    session = make_session(session="new", extensions=False, refresh_models=False)
    try:
        # Write 16 lines of 1 MiB each (16 MiB total) directly to the output pipe.
        record, pipe = session.main.execution_context.open_output(project, "wide")
        one_mib_line = (b"z" * (1024 * 1024 - 1)) + b"\n"
        with pipe:
            for _ in range(16):
                pipe.write(one_mib_line)

        tracemalloc.start()
        try:
            faux.script(
                "test",
                faux.reply(tools=[("read_output", {"output_id": str(record), "tail": True, "line_limit": 16})]),
                faux.reply("done"),
            )
            session.prompt("tail wide lines")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        res = tool_results(session)[-1]
        assert not res.is_error
        assert len(res.text().encode("utf-8")) <= MAX_BYTES
        assert res.details["total_lines"] == 16
        assert peak < 4 * 1024 * 1024, f"Tail line scan retained too much memory: {peak} bytes"
    finally:
        session.shutdown()
