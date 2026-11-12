"""Tool output budgets report truncation only when text is cut."""

from __future__ import annotations

from virgo.tools.truncate import MAX_LINES, budget


def test_budget_reports_truncation_only_when_text_is_cut(tmp_path):
    exact = "".join(f"{i}\n" for i in range(MAX_LINES))
    assert budget(exact, tmp_path, "t") == exact
    assert not (tmp_path / ".virgo").exists()
    longer = "".join(f"{i}\n" for i in range(MAX_LINES + 5))
    assert f"showing the first {MAX_LINES} of {MAX_LINES + 5} lines" in budget(longer, tmp_path, "t")
    assert f"showing the last {MAX_LINES} of {MAX_LINES + 5} lines" in budget(longer, tmp_path, "t", keep="tail")
