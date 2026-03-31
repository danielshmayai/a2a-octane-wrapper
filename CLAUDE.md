# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python main.py                          # Start A2A wrapper (port 9000)
python mock_oauth_server.py             # Start mock OAuth server (port 8090)
pytest                                  # Run unit tests (tests/ only, per pytest.ini)
pytest tests/e2e/ -v                    # Run end-to-end tests (requires live MCP server)
pytest tests/test_main_handlers.py -v   # Run a single test file
pytest -k "test_health"                 # Run a single test by name
pytest --html=reports/report.html       # Tests with HTML report
```

## Architecture

```
Gemini Enterprise ──A2A──▶  main.py (FastAPI)  ──MCP──▶  Opentext SDP /mcp
                                │
                    ┌───────────┼────────────┐
               gemini_agent.py  │       tool_router.py
               (ADK + LlmAgent) │       (TOOL_REGISTRY + keyword fallback)
                                │
                           mcp_client.py
                           (httpx + MCP SDK)
```

**Request flow (streaming path):**
1. `POST /message:stream` → `stream_message()` in `main.py`
2. Token resolution: no A2A key → use `config.API_KEY`; A2A key matches → use `config.API_KEY`; else passthrough
3. `GeminiAgent.run_streaming()` spawns `run()` as a background task and yields SSE events from a queue
4. `run()` rebuilds the ADK `Runner` each turn (to capture fresh `bearer_token` in tool closures), then calls `runner.run_async()`
5. Dynamic tool functions (built by `_make_dynamic_tool_fn`) call `_invoke()` → `execute_tool()` → `mcp_client.call_tool()`
6. If ADK produces no final text but MCP was called → `_synthesize_from_artifacts()` does a direct Gemini API call
7. Final SSE event `{"type":"final", ...}` is yielded; `data: [DONE]` closes the stream

**Dual-mode operation:**
- `GEMINI_API_KEY` set → `GeminiAgent` (ADK multi-step function-calling loop)
- No `GEMINI_API_KEY` → `_handle_with_keywords()` keyword router in `main.py` (fallback)

**Tool discovery lifecycle:**
- Startup: `mcp.list_tools()` → `populate_registry_from_mcp()` → `agent.refresh_tools()` (non-fatal on failure)
- Periodic: `MCP_TOOL_POLL_INTERVAL_SECONDS` (default 86400 s / once per day)
- Manual: `POST /discover-tools`; also triggered automatically by `POST /config`

## Key Files

| File | Role |
|---|---|
| `main.py` | FastAPI app, all HTTP endpoints, startup/token logic |
| `gemini_agent.py` | `GeminiAgent` class, ADK runner, system prompt, dynamic tool builder |
| `tool_router.py` | `TOOL_REGISTRY`, `populate_registry_from_mcp()`, `execute_tool()`, keyword router |
| `mcp_client.py` | `OctaneMcpClient` — wraps MCP SDK, injects `sharedSpaceId`/`workSpaceId` |
| `a2a_models.py` | All Pydantic models (A2A protocol, tasks, artifacts, agent card) |
| `config.py` | All env-var config with defaults |
| `static/index.html` | Chat UI — SSE stream consumer, live tool-call display |

## Critical Patterns

**Session management:** `GeminiAgent` uses `InMemorySessionService` keyed by `contextId`. The runner is rebuilt every turn but the session service is shared, preserving history. If a turn fails mid-flight, ADK may leave an orphaned user event in the session — this is cleaned up at the start of the next `run()` call.

**MCP params:** `sharedSpaceId` and `workSpaceId` are auto-injected by `mcp_client.call_tool()` and excluded from ADK tool schemas (`_EXCLUDED_MCP_PARAMS`). Never pass them from the agent.

**AQQL filter rule:** `filter` must always be paired with a non-empty `keywords` value (use `keywords='*'` when no keyword is needed). Empty `keywords` with a filter causes a server error.

**A2A protocol:** Both `POST /` (JSON-RPC 2.0, used by Gemini Enterprise) and `POST /message:send` / `POST /message:stream` (HTTP+JSON) are supported. The JSON-RPC path wraps the same `_handle_with_agent()` / `_handle_with_keywords()` logic.

**Param description patches:** The Octane MCP server ships incorrect parameter descriptions (all labelled "Shared Space ID"). `populate_registry_from_mcp()` patches them at load time via `_PARAM_DESCRIPTION_OVERRIDES` in `tool_router.py`.

## Testing

- Unit tests mock `main.mcp` via `conftest.py` autouse fixture — no network calls
- E2E tests in `tests/e2e/` require a live MCP server and manage their own fixtures
- `asyncio_mode = strict` in `pytest.ini` — all async tests need `@pytest.mark.asyncio`
- Verify both JSON-RPC envelope (`jsonrpc="2.0"`, echoed `id`) and REST response shapes

## Configuration Reference

| Env var | Default | Purpose |
|---|---|---|
| `OCTANE_BASE_URL` | `http://localhost:8080` | Octane server base URL |
| `API_KEY` | — | Octane API key (server-side passthrough) |
| `DEFAULT_WORKSPACE_ID` | `1002` | Workspace injected into every MCP call |
| `DEFAULT_SHARED_SPACE_ID` | `1001` | Shared space injected into every MCP call |
| `GEMINI_API_KEY` | — | Enables Gemini agent mode |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Model used for agent + synthesis |
| `A2A_API_KEY` | — | Inbound auth (empty = disabled) |
| `MCP_TOOL_POLL_INTERVAL_SECONDS` | `86400` | Tool re-discovery interval (0 = off) |
| `MCP_REQUEST_TIMEOUT_SECONDS` | `10` | Per-MCP-call timeout |
| `GEMINI_REQUEST_TIMEOUT_SECONDS` | `10` | Gemini synthesis/joke timeout |

## Reference Infrastructure

For MCP patterns, security, auth (OAuth/PKCE), testing, deployment, and performance guidance:
`C:/Daniel/AI/claude/claude-app-infrastructure/`
