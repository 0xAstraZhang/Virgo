"""Validate review conclusions against the Harness-owned target snapshot."""
from __future__ import annotations

import json
import math

from ...diagnostics import safe_text


class InvalidReviewReport(ValueError):
    """The model did not supply a usable, correctly located review report."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidReviewReport(f"Duplicate report field: {key}")
        result[key] = value
    return result


def _constant(value):
    raise InvalidReviewReport(f"Non-finite JSON number: {value}")


def _text(value, field, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvalidReviewReport(f"{field} must be nonempty text of at most {maximum} characters")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise InvalidReviewReport(f"{field} must contain valid Unicode text") from error
    return value.strip()


def validate_report(text: str, snapshot: dict) -> dict:
    """Parse exact JSON; malformed or unrelated findings never become a clean report."""
    if len(text.encode("utf-8")) > 1_048_576:
        raise InvalidReviewReport("Review report exceeds the 1 MiB limit")
    try:
        data = json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, TypeError, RecursionError) as error:
        raise InvalidReviewReport(f"Review report must be one valid JSON object: {error}") from error
    if not isinstance(data, dict) or set(data) - {"status", "summary", "findings"}:
        raise InvalidReviewReport("Review report requires only status, summary and findings fields")
    status = data.get("status", "completed")
    if not isinstance(status, str) or status not in {"completed", "incomplete"}:
        raise InvalidReviewReport("Model review status must be completed or incomplete")
    summary = _text(data.get("summary"), "summary", 8000)
    findings = data.get("findings")
    if not isinstance(findings, list) or len(findings) > 100:
        raise InvalidReviewReport("findings must be a list with at most 100 entries")
    changes = {change["id"]: change for change in snapshot["changes"]}
    validated = []
    fields = {"change_id", "path", "side", "start_line", "end_line", "priority",
              "title", "explanation", "confidence"}
    for index, finding in enumerate(findings, 1):
        label = f"Finding {index}"
        if not isinstance(finding, dict) or set(finding) != fields:
            raise InvalidReviewReport(f"{label} must contain exactly {', '.join(sorted(fields))}")
        change_id = finding["change_id"]
        if not isinstance(change_id, str) or change_id not in changes:
            raise InvalidReviewReport(f"{label} does not identify a change in this snapshot")
        change = changes[change_id]
        side = finding["side"]
        if not isinstance(side, str) or side not in {"old", "new"}:
            raise InvalidReviewReport(f"{label} side must be old or new")
        path = finding["path"]
        if not isinstance(path, str) or not path or path != change[f"{side}_path"]:
            raise InvalidReviewReport(f"{label} path does not match the selected diff side")
        start, end = finding["start_line"], finding["end_line"]
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= change[f"{side}_lines"]:
            raise InvalidReviewReport(f"{label} line range does not exist in the selected diff side")
        if end - start >= 20:
            raise InvalidReviewReport(f"{label} must use a focused range of at most 20 lines")
        if not any(start <= line <= end for line in change[f"{side}_changed_lines"]):
            raise InvalidReviewReport(f"{label} location does not overlap a changed line")
        priority, confidence = finding["priority"], finding["confidence"]
        if type(priority) is not int or not 0 <= priority <= 3:
            raise InvalidReviewReport(f"{label} priority must be an integer from 0 to 3")
        if type(confidence) not in {int, float} or not 0 <= confidence <= 1 or not math.isfinite(confidence):
            raise InvalidReviewReport(f"{label} confidence must be a finite number from 0 to 1")
        validated.append({
            **finding,
            "title": _text(finding["title"], f"{label} title", 200),
            "explanation": _text(finding["explanation"], f"{label} explanation", 8000),
        })
    return {"status": status, "summary": summary, "findings": validated}


def render_report(report: dict) -> str:
    """One explicit status-bearing representation for the plain and full-screen hosts."""
    snapshot = report.get("snapshot") or {}
    target = snapshot.get("target") or report.get("target") or {}
    target_label = " ".join(str(part) for part in (target.get("kind"), target.get("ref")) if part)
    freshness = report.get("freshness", "unknown")
    lines = [f"Code review {report['id']} — {report['status']}", f"Target: {safe_text(target_label)}"]
    if snapshot.get("id"):
        lines.append(f"Snapshot: {snapshot['id']}")
    lines.append(f"Freshness: {freshness}")
    if freshness == "stale":
        lines.append("STALE: the source workspace or selected target changed after this snapshot.")
    elif freshness == "unknown":
        lines.append("The current snapshot could not be verified.")
    if report.get("freshness_reason"):
        lines.append(safe_text(report["freshness_reason"]))
    lines.extend(["", safe_text(report.get("summary", "Review has no terminal result."))])
    findings = report.get("findings") or []
    changes = {change["id"]: change for change in snapshot.get("changes", [])}
    for finding in findings:
        change = changes.get(finding["change_id"], {})
        location = f"{finding['path']}:{finding['start_line']}-{finding['end_line']}"
        lines.extend([
            "", f"[P{finding['priority']}] {safe_text(finding['title'])}",
            f"  {safe_text(location)} ({change.get('layer', 'diff')}, {finding['side']} side)",
            f"  {safe_text(finding['explanation'])}",
            f"  Confidence: {finding['confidence']:.2f}",
        ])
    if not findings:
        lines.extend(["", "No findings reported for this snapshot." if report["status"] == "completed"
                      else "No complete review conclusion is available."])
    error = report.get("error")
    if isinstance(error, dict) and error.get("error_id"):
        lines.append(f"Diagnostic: {error['error_id']}")
    return "\n".join(lines)
