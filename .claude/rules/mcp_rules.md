---
paths:
  - mcp_client.py
  - tool_router.py
  - gemini_agent.py
---

# MCP Integration Rules

## MCP client patterns
- Uses official `mcp` Python SDK with Streamable HTTP transport (spec 2025-03-26)
- Connection pooling via persistent `httpx.AsyncClient` with HTTP/2 support
- Every request carries: Authorization Bearer header + sharedSpaceId + workSpaceId in params
- Short-lived ClientSession per call (stateless like the server)

## Current Octane MCP tool API (generic, not entity-specific)
The Octane MCP server exposes a **generic** API — do NOT add entity-specific hardcoded
tools (get_defect, get_story, etc.). The live tool set as of 2026-03:

| Tool | Purpose |
|------|---------|
| `get_entity_types` | FIRST CALL — list all entity type identifiers in the tenant |
| `get_entity_field_metadata` | Field names, types, enums for a given entityType |
| `get_filter_metadata` | Filter syntax, operators, value rules — call before building any filter |
| `get_entities` | Fetch multiple entities with optional filter/fields/keywords |
| `get_entity` | Fetch a single entity by entityType + entityId |

**Discovery-first pattern**: always call `get_entity_types` → `get_entity_field_metadata`
→ `get_filter_metadata` before building a filtered query. Never guess field names or
filter syntax.

## Tool registry (TOOL_REGISTRY in tool_router.py)
- **Seed contains only local-only tools** (`tell_joke`). All MCP tools come from
  `populate_registry_from_mcp()` at startup / refresh.
- Local-only tools marked with `_local_only: True` — never sent to MCP server.
- Each registry entry from MCP discovery stores: `description`, `default_arguments`,
  `required`, and `inputSchema` (full JSON Schema properties with types — needed
  for dynamic ADK function generation).
- `_EXCLUDED_MCP_PARAMS`: `sharedSpaceId`, `workSpaceId` — auto-injected by
  `mcp_client.py`, never exposed to the agent or user.

## Dynamic tool generation (gemini_agent.py)
- `_build_tools()` generates ALL MCP tool functions dynamically from TOOL_REGISTRY.
- `_make_dynamic_tool_fn()` creates typed async callables using `inspect.Signature`
  and `__annotations__` so ADK can infer correct Gemini FunctionDeclaration schemas.
- JSON Schema types are mapped: string→str, integer→int, number→float, boolean→bool.
- Do NOT add hardcoded tool functions for MCP tools — they will become stale when
  the MCP server API changes.

## Tool discovery lifecycle
1. **Startup** (`main.py _startup`): calls `mcp.list_tools()` → `populate_registry_from_mcp()`
   → `agent.refresh_tools()`. Failure is logged but non-fatal (only `tell_joke` remains).
2. **Periodic** (`MCP_TOOL_POLL_INTERVAL_SECONDS` env var, default disabled): background
   task re-runs discovery and rebuilds agent runner.
3. **Manual** (`POST /discover-tools`): admin endpoint, auth-optional when A2A_API_KEY
   not set. UI triggers this automatically when MCP comes online (`_mcpWasOnline` flag).
4. **Config change** (`POST /config`): always re-discovers tools after URL/key update.

## Critical Octane parameters
- `sharedSpaceId`: from `config.DEFAULT_SHARED_SPACE_ID` (default 1001) — never hardcode
- `workSpaceId`: from `config.DEFAULT_WORKSPACE_ID` — never hardcode
- Both are injected by `mcp_client.call_tool()` — do not pass them from the agent

## Error handling
- `OctaneMcpError` for server-side errors (code, message, data)
- Timeout via `MCP_REQUEST_TIMEOUT_SECONDS` (default 10s)
- Always catch `httpx.TimeoutException` and `httpx.HTTPStatusError`
- Return A2A Task with `state=FAILED` and user-friendly error message

## When modifying tool_router.py
- Never add MCP tool entries to the TOOL_REGISTRY seed — they are auto-populated
- `populate_registry_from_mcp()` replaces the registry on each discovery; local-only
  tools are preserved via `_LOCAL_ONLY_TOOLS` frozenset
- `inputSchema` must be stored per tool entry (done by `populate_registry_from_mcp`)
  so that `_make_dynamic_tool_fn` can build typed signatures
