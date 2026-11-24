"""Tool output budgets report truncation only when text is cut."""

from __future__ import annotations

from virgo.tools.output import MAX_LINES, budget


def test_budget_reports_truncation_only_when_text_is_cut(tmp_path):
    exact = "".join(f"{i}\n" for i in range(MAX_LINES))
    assert budget(exact, tmp_path, "t") == exact
    assert not (tmp_path / ".virgo").exists()
    longer = "".join(f"{i}\n" for i in range(MAX_LINES + 5))
    for keep, where in (("head", "first"), ("tail", "last")):
        preview = budget(longer, tmp_path, "t", keep=keep)
        body, notice = preview.rsplit("\n\n", 1)
        assert longer.startswith(body) if keep == "head" else longer.endswith(body)
        assert len(preview.splitlines()) <= MAX_LINES
        assert f"showing the {where} {len(body.splitlines())} of {MAX_LINES + 5} lines" in notice
