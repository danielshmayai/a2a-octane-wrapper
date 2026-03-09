"""
Stateless HTTP-POST MCP Client for the Octane MCP Server.

The Octane MCP server does NOT use SSE or STDIO transports.
It exposes a single endpoint at  POST <base_url>/mcp  that accepts
standard JSON-RPC 2.0 payloads and returns JSON-RPC 2.0 responses.

Every request must carry:
  - Authorization: Bearer <API_KEY>  header
  - sharedSpaceId and workSpaceId inside params.arguments
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

# Monotonically increasing JSON-RPC request id (per-process)
_jsonrpc_id_counter: int = 0


def _next_id() -> int:
    global _jsonrpc_id_counter
    _jsonrpc_id_counter += 1
    return _jsonrpc_id_counter


class OctaneMcpClient:
    """Thin async wrapper around Octane's stateless HTTP MCP endpoint."""

    def __init__(
        self,
        base_url: str = config.OCTANE_MCP_ENDPOINT,
        api_key: str = config.API_KEY,
        timeout: int = config.MCP_REQUEST_TIMEOUT_SECONDS,
    ):
        self._url = base_url
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {api_key}",
        }
        self._timeout = timeout

    # ── Core transport ───────────────────────────────────────────────

    async def _post(self, payload: dict) -> dict:
        """Send a single JSON-RPC 2.0 request and return the parsed response."""
        logger.info("MCP >>> %s  payload=%s", self._url, payload)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._url, json=payload, headers=self._headers
            )
            if resp.is_error:
                # Log the full response body before raising so we can see why Octane rejected the request
                try:
                    err_body = resp.json()
                except Exception:
                    err_body = resp.text
                logger.error(
                    "MCP HTTP %s error  url=%s  request=%s  response=%s",
                    resp.status_code, self._url, payload, err_body,
                )
                resp.raise_for_status()
            body = resp.json()

        logger.info("MCP <<< status=%s  body=%s", resp.status_code, body)
        return body

    # ── Public helpers ───────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        shared_space_id: int | None = None,
        workspace_id: int | None = None,
    ) -> dict:
        """
        Invoke an MCP tool on the Octane server.

        Automatically injects the mandatory sharedSpaceId / workSpaceId
        context variables into the arguments dict.

        Returns the raw JSON-RPC result object, or raises on transport error.
        """
        # Inject mandatory Octane context into every call
        arguments["sharedSpaceId"] = (
            shared_space_id or config.DEFAULT_SHARED_SPACE_ID
        )
        arguments["workSpaceId"] = (
            workspace_id or config.DEFAULT_WORKSPACE_ID
        )

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
            "id": _next_id(),
        }

        body = await self._post(payload)
        return self._parse_response(body)

    async def list_tools(self) -> dict:
        """Ask the Octane MCP server which tools it exposes."""
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            "id": _next_id(),
        }
        body = await self._post(payload)
        return self._parse_response(body)

    # ── Response parsing ─────────────────────────────────────────────

    @staticmethod
    def _parse_response(body: dict) -> dict:
        """
        Parse a JSON-RPC 2.0 response from Octane.

        Octane may return errors in two ways:
          1. Standard JSON-RPC "error" key  → {"error": {"code": ..., "message": ...}}
          2. A successful result containing an isError flag inside content items.

        Both cases are normalised into a consistent structure.
        """
        # Case 1: top-level JSON-RPC error object
        if "error" in body:
            err = body["error"]
            raise OctaneMcpError(
                code=err.get("code", -1),
                message=err.get("message", "Unknown MCP error"),
                data=err.get("data"),
            )

        result = body.get("result", {})

        # Case 2: Octane wraps errors inside content with isError=true
        if result.get("isError"):
            # Extract the first text content block as the error message
            content_items = result.get("content", [])
            msg = next(
                (c.get("text", "") for c in content_items if c.get("type") == "text"),
                "Tool returned an error",
            )
            raise OctaneMcpError(code=-32000, message=msg, data=result)

        return result


class OctaneMcpError(Exception):
    """Raised when the Octane MCP server returns an error."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP Error {code}: {message}")
