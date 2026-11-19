"""Run the locked official SDK with its default asyncio stdio transport."""

import asyncio
import json
from importlib.metadata import version
from pathlib import Path

from mcp.server.mcpserver import MCPServer

server = MCPServer("sdk-boundary")


@server.tool()
async def probe(path: str = "") -> str:
    """Report the real SDK runtime and optionally write an ordinary file."""
    if path:
        Path(path).write_text("official SDK", encoding="utf-8")
    return json.dumps({"sdk": version("mcp"), "loop": type(asyncio.get_running_loop()).__name__})


server.run()
