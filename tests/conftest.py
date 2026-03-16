"""
Unit-test conftest: auto-mock the MCP client so tests never make real network
calls and the FastAPI startup event completes instantly.

Why this is needed
------------------
main._startup() calls mcp.list_tools() on every TestClient startup.
If the Opentext SDP server is not running the call blocks for up to
MCP_REQUEST_TIMEOUT_SECONDS (default 30 s) before timing out —
making the whole unit-test suite slow or appear to hang.

The autouse fixture below patches main.mcp *before* TestClient enters
its lifespan so the startup event receives an instant mock response.
E2E tests under tests/e2e/ have their own conftest and do real (or
separately mocked) MCP calls, so they are excluded via a marker check.
"""

from __future__ import annotations

import sys
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

# Ensure repo root is importable
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def mock_mcp_client(request):
    """Replace the global MCP client with an instant-returning mock.

    Skipped automatically for tests in the e2e sub-package (they manage
    their own mocking) and for any test explicitly marked ``no_mcp_mock``.
    """
    # Don't interfere with e2e tests — they live in tests/e2e/
    if "e2e" in str(request.fspath):
        yield
        return

    # Allow individual tests to opt out with @pytest.mark.no_mcp_mock
    if request.node.get_closest_marker("no_mcp_mock"):
        yield
        return

    mock = AsyncMock()
    mock.list_tools = AsyncMock(return_value={"tools": []})
    mock.call_tool = AsyncMock(return_value={
        "content": [{"type": "text", "text": "mock"}]
    })

    import main  # imported here so sys.path insert above takes effect

    with patch.object(main, "mcp", mock):
        yield mock
