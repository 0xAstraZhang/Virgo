"""Minimal stdio MCP server used by the tests (and as an example)."""

import json
import sys
from pathlib import Path

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "add",
        "description": "Add two numbers",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    msg["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if rid is None:
        continue  # notification
    if method == "initialize":
        if len(sys.argv) > 1:
            Path(sys.argv[1]).write_text(json.dumps(req["params"]))
        reply(
            rid,
            {
                "protocolVersion": req["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo", "version": "1"},
            },
        )
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = req["params"]["name"], req["params"].get("arguments", {})
        if name == "echo":
            reply(rid, {"content": [{"type": "text", "text": args["text"]}]})
        elif name == "add":
            reply(rid, {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]})
        else:
            reply(rid, {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True})
    else:
        reply(rid, error={"code": -32601, "message": f"unknown method {method}"})
