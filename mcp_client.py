"""
MCP Client using the official `mcp` Python SDK with Streamable HTTP transport.

The Opentext SDP MCP server exposes a single endpoint at POST <base_url>/mcp.
The MCP SDK's Streamable HTTP transport (spec 2025-03-26) is fully compatible
with this endpoint — it uses the same POST + Accept: application/json,
text/event-stream handshake, and drives the standard MCP initialize →
tools/list → tools/call flow over a ClientSession.

Every request carries:
    - Authorization: Bearer <API_KEY>  header
    - sharedSpaceId and workSpaceId injected into params.arguments
"""

from __future__ import annotations

import logging
import time
from typing import Any
import os

import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared._httpx_utils import create_mcp_http_client

import config

logger = logging.getLogger(__name__)


class OctaneMcpClient:
    """
    Async MCP client for the Opentext SDP HTTP endpoint.

    Uses the official `mcp` SDK with the Streamable HTTP transport:
      - Opens a short-lived ClientSession for each call (stateless like the server)
      - Drives the standard initialize → tools/call (or tools/list) exchange
      - Falls back to None Authorization header when no API key is configured
    """

    def __init__(
        self,
        base_url: str = config.OCTANE_MCP_ENDPOINT,
        api_key: str | None = None,
        timeout: int = config.MCP_REQUEST_TIMEOUT_SECONDS,
    ):
        resolved_key = api_key or config.API_KEY or os.getenv("API_KEY", "")
        if not resolved_key:
            try:
                load_dotenv()
                resolved_key = os.getenv("API_KEY", "")
                if resolved_key:
                    logger.info("Loaded API_KEY from .env at runtime")
            except Exception:
                logger.debug("Could not load .env or no API_KEY present")

        self._url = base_url
        self._headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
        }
        if resolved_key:
            self._headers["Authorization"] = f"Bearer {resolved_key}"
        else:
            logger.debug("No API_KEY configured; requests sent without Authorization header")

        self._timeout = float(timeout)
        # Create a persistent httpx AsyncClient to allow connection reuse and
        # keep-alive across MCP calls. This reduces per-call TLS handshake and
        # connection setup latency.
        try:
            # Prefer an httpx.AsyncClient with HTTP/2 enabled to allow multiplexed
            # connections and further reduce per-request latency when the server
            # supports HTTP/2. Fall back to the MCP helper if this fails.
            self._http_client = httpx.AsyncClient(
                follow_redirects=True,
                headers=self._headers,
                timeout=httpx.Timeout(self._timeout, read=max(self._timeout, 300.0)),
                http2=True,
            )
        except Exception:
            try:
                self._http_client = create_mcp_http_client(
                    headers=self._headers,
                    timeout=httpx.Timeout(self._timeout, read=max(self._timeout, 300.0)),
                )
            except Exception:
                self._http_client = None

    # ── Public API ───────────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        bearer_token: str | None = None,
        shared_space_id: int | None = None,
        workspace_id: int | None = None,
    ) -> dict:
        """
        Invoke an MCP tool on the Opentext SDP server.

        Automatically injects the mandatory sharedSpaceId / workSpaceId
        context variables. Returns a dict with a "content" list, or raises
        OctaneMcpError on server-side errors.
        """
        arguments = dict(arguments)  # don't mutate caller's dict

        if tool_name == "get_entities":
            # filter must be a plain string — if Gemini passes a list, join with " ; "
            f = arguments.get("filter")
            if isinstance(f, list):
                arguments["filter"] = " ; ".join(str(x) for x in f if x) or None
            elif isinstance(f, str) and not f.strip():
                arguments["filter"] = None

            # Octane requires keywords to be non-null when filter is present.
            # Pass an empty string so the server doesn't reject the request.
            if arguments.get("filter") and arguments.get("keywords") is None:
                arguments["keywords"] = ""

        arguments["sharedSpaceId"] = shared_space_id or config.DEFAULT_SHARED_SPACE_ID
        arguments["workSpaceId"] = workspace_id or config.DEFAULT_WORKSPACE_ID

        start = time.monotonic()
        logger.info("MCP >>> call_tool=%s  url=%s  args=%s", tool_name, self._url, arguments)

        headers = dict(self._headers)
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"

        ctx = streamablehttp_client(self._url, headers=headers, terminate_on_close=False)

        async with ctx as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)

        elapsed = time.monotonic() - start
        logger.info("MCP call_tool duration=%.3fs tool=%s", elapsed, tool_name)

        logger.info("MCP <<< call_tool=%s  isError=%s", tool_name, result.isError)

        if result.isError:
            content_items = result.content or []
            msg = next(
                (getattr(c, "text", "") for c in content_items if getattr(c, "text", "")),
                "Tool returned an error",
            )
            raise OctaneMcpError(code=-32000, message=msg, data=str(result))

        return {
            "content": [
                {"type": getattr(c, "type", "text"), "text": getattr(c, "text", None)}
                for c in (result.content or [])
            ]
        }

    async def list_tools(self, *, bearer_token: str | None = None) -> dict:
        """Return the list of tools the Opentext SDP MCP server exposes."""
        start = time.monotonic()
        logger.info("MCP >>> list_tools  url=%s", self._url)

        headers = dict(self._headers)
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"

        ctx = streamablehttp_client(self._url, headers=headers, terminate_on_close=False)

        async with ctx as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()

        elapsed = time.monotonic() - start
        logger.info("MCP list_tools duration=%.3fs", elapsed)

        tools = result.tools or []
        logger.info("MCP <<< list_tools  count=%d", len(tools))

        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": (
                        t.inputSchema
                        if isinstance(t.inputSchema, dict)
                        else t.inputSchema.model_dump() if t.inputSchema else {}
                    ),
                }
                for t in tools
            ]
        }


class OctaneMcpError(Exception):
    """Raised when the Opentext SDP MCP server returns an error."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP Error {code}: {message}")
