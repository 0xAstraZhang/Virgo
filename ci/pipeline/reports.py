"""Report integrity and test-name normalization."""

from __future__ import annotations

import math
from collections import Counter

BAD = {"failed", "error"}
STATUSES = BAD | {"passed", "skipped"}


def validate_report(data):
    if not isinstance(data, dict):
        raise ValueError("Report must be an object")
    for key in ("platform", "sha"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"Missing or invalid {key}")
    if data.get("complete") is not True or type(data.get("exit_code")) is not int or data["exit_code"] not in {0, 1}:
        raise ValueError("pytest did not complete normally")
    duration = data.get("duration")
    if type(duration) not in {float, int} or not math.isfinite(duration) or duration < 0:
        raise ValueError("Invalid duration")
    for key in ("problems", "collection_skips"):
        if not isinstance(data.get(key), list) or not all(isinstance(value, str) for value in data[key]):
            raise ValueError(f"Invalid {key}")
    if not isinstance(data.get("tests"), dict) or not data["tests"]:
        raise ValueError("No individual test outcomes in report")
    counts = Counter()
    for name, case in data["tests"].items():
        if not name or not isinstance(case, dict) or case.get("status") not in STATUSES:
            raise ValueError(f"Invalid test outcome: {name}")
        counts[case["status"]] += 1
    counts["skipped"] += len(data["collection_skips"])
    actual = data.get("counts")
    if (not isinstance(actual, dict) or any(key not in STATUSES or type(value) is not int or value < 0
                                          for key, value in actual.items()) or Counter(actual) != counts):
        raise ValueError("Test counts do not match individual outcomes")
    if bool(data["exit_code"]) != bool(counts["failed"] + counts["error"]):
        raise ValueError("pytest exit code does not match test outcomes")
