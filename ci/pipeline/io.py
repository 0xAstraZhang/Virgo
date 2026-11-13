"""Strict JSON artifacts and GitHub Actions step outputs."""

from __future__ import annotations

import json
import os


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def outputs(values):
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                value = value if isinstance(value, str) else json.dumps(value)
                if "\n" in value or "\r" in value:
                    raise ValueError(f"Multiline workflow output: {key}")
                stream.write(f"{key}={value}\n")
