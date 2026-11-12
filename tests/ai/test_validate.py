"""Tool argument parsing and schema coercion."""

from __future__ import annotations

from virgo.ai.validate import INVALID_ARGUMENTS, parse_json_loose, parse_tool_arguments, validate


def test_validate_coercions():
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer"},
            "flag": {"type": "boolean"},
            "items": {"type": "array", "items": {"type": "string"}},
            "obj": {"type": "object", "properties": {"k": {"type": "string"}}, "required": ["k"]},
        },
        "required": ["n"],
    }
    v, errs = validate({"n": "4", "flag": "true", "items": "one", "obj": '{"k": "v"}'}, schema)
    assert errs == [] and v == {"n": 4, "flag": True, "items": ["one"], "obj": {"k": "v"}}
    _, errs = validate({"obj": {}}, schema)
    assert any("n: required" in e for e in errs) and any("k: required" in e for e in errs)


def test_tool_arguments_must_be_a_json_object():
    assert parse_tool_arguments('{"path": "a"}') == {"path": "a"}
    assert parse_tool_arguments("") == parse_tool_arguments("  ") == {}
    for raw in ("null", '"src"', "[1, 2]", "3", '{"path": '):
        assert parse_tool_arguments(raw) == {INVALID_ARGUMENTS: raw}


def test_parse_json_loose():
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Sure! {"ok": true} hope that helps') == {"ok": True}
