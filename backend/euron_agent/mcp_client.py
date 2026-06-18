"""MCP (Model Context Protocol) client integration.

Connects to external MCP servers (stdio or SSE/HTTP) and exposes their tools to
the agent as OpenAI-style function schemas named ``mcp__<server>__<tool>``. This
is the universal, model-independent extensibility layer: any MCP server's tools
become available to whatever model you're running.

Entirely optional and guarded: if the `mcp` package isn't installed or no servers
are configured, this is a no-op and the core agent is unaffected.

Config (config.yaml):
    mcp:
      servers:
        filesystem:
          command: npx
          args: ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
          env: {}
        my_http:
          url: https://example.com/mcp        # SSE endpoint
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any, Optional


def mcp_available() -> bool:
    try:
        import mcp  # noqa: F401

        return True
    except Exception:
        return False


def _tool_name(server: str, tool: str) -> str:
    return f"mcp__{server}__{tool}"


class MCPManager:
    def __init__(self, servers: dict):
        self.servers = servers or {}
        self._stack: Optional[AsyncExitStack] = None
        self._sessions: dict[str, Any] = {}
        self._schemas: list[dict] = []
        self._route: dict[str, tuple[str, str]] = {}  # full name -> (server, tool)
        self.errors: list[str] = []
        self.started = False

    def schemas(self) -> list[dict]:
        return self._schemas

    async def start(self) -> None:
        if self.started or not self.servers or not mcp_available():
            self.started = True
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        for name, conf in self.servers.items():
            try:
                if conf.get("url"):
                    from mcp.client.sse import sse_client

                    read, write = await self._stack.enter_async_context(
                        sse_client(conf["url"])
                    )
                else:
                    params = StdioServerParameters(
                        command=conf["command"],
                        args=conf.get("args", []),
                        env=conf.get("env") or None,
                    )
                    read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
                listed = await session.list_tools()
                for t in listed.tools:
                    full = _tool_name(name, t.name)
                    self._route[full] = (name, t.name)
                    self._schemas.append(
                        {
                            "type": "function",
                            "function": {
                                "name": full,
                                "description": (t.description or "")[:1024],
                                "parameters": t.inputSchema or {"type": "object", "properties": {}},
                            },
                        }
                    )
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{name}: {e}")
        self.started = True

    async def call(self, full_name: str, args: dict) -> str:
        route = self._route.get(full_name)
        if not route:
            return f"unknown MCP tool: {full_name}"
        server, tool = route
        session = self._sessions.get(server)
        if not session:
            return f"MCP server not connected: {server}"
        try:
            result = await session.call_tool(tool, args or {})
        except Exception as e:  # noqa: BLE001
            return f"MCP tool error: {e}"
        # result.content is a list of content blocks
        parts: list[str] = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            parts.append(text if text is not None else str(block))
        return "\n".join(parts) if parts else "(no content)"

    async def stop(self) -> None:
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
        self._sessions.clear()
        self.started = False


def is_mcp_tool(name: str) -> bool:
    return name.startswith("mcp__")
