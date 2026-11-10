"""A deliberately small JSON-Schema validator with lenient coercion.

Supports the subset tools actually use: type (incl. lists), properties, required, items,
enum, additionalProperties=false, minItems/maxItems, minLength, minimum/maximum.
Coercions (like Pi's validateToolArguments): numeric/bool strings, JSON strings for
objects/arrays, and a scalar where an array is expected.
"""

from __future__ import annotations

import json
from typing import Any

_PY = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _is(value: Any, t: str) -> bool:
    if t in ("integer", "number") and isinstance(value, bool):
        return False
    if t == "integer" and isinstance(value, float):
        return value.is_integer()
    return isinstance(value, _PY.get(t, object))


def _coerce(value: Any, t: str) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if t == "integer":
            try:
                return int(s)
            except ValueError:
                return value
        if t == "number":
            try:
                return float(s) if any(c in s for c in ".eE") else int(s)
            except ValueError:
                return value
        if t == "boolean" and s.lower() in ("true", "false"):
            return s.lower() == "true"
        if t in ("object", "array") and s[:1] in ("{", "["):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return value
        if t == "null" and s.lower() in ("null", "none", ""):
            return None
    if t == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if t == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    if t == "array" and not isinstance(value, list) and value is not None:
        return [value]
    return value


def validate(value: Any, schema: dict, path: str = "$") -> tuple[Any, list[str]]:
    """Return (coerced_value, errors)."""
    if not schema:
        return value, []
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        tlist = types if isinstance(types, list) else [types]
        if not any(_is(value, t) for t in tlist):
            for t in tlist:
                coerced = _coerce(value, t)
                if _is(coerced, t):
                    value = coerced
                    break
            else:
                return value, [f"{path}: expected {'/'.join(tlist)}, got {type(value).__name__}"]

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: must have at least {schema['minLength']} characters")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: must have at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: must have at most {schema['maxItems']} items")
        if "items" in schema:
            out = []
            for i, item in enumerate(value):
                v, errs = validate(item, schema["items"], f"{path}[{i}]")
                out.append(v)
                errors.extend(errs)
            value = out
    elif isinstance(value, dict):
        props = schema.get("properties", {})
        out = dict(value)
        for req in schema.get("required", []):
            if req not in out:
                errors.append(f"{path}.{req}: required")
        for key, sub in props.items():
            if key in out and not (out[key] is None and key not in schema.get("required", [])):
                out[key], errs = validate(out[key], sub, f"{path}.{key}")
                errors.extend(errs)
            elif key in out and out[key] is None:
                del out[key]  # treat explicit null for optional fields as absent
        if schema.get("additionalProperties") is False:
            extra = [k for k in out if k not in props]
            if extra:
                errors.append(f"{path}: unexpected properties {extra}")
        value = out
    return value, errors


def parse_json_loose(text: str) -> Any:
    """Parse JSON from model output: strips code fences, falls back to the first {...} / [...]."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch in "{[":
            try:
                return decoder.raw_decode(s[i:])[0]
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON value found in model output")
