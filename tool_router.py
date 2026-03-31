"""
Tool Router – translates inbound A2A intents into Opentext SDP MCP tool calls.

The router does two things:
  1. **Intent extraction** – parses the A2A Message to figure out which
      Opentext SDP tool to call and with what arguments.
  2. **Result formatting** – converts the raw MCP tool result into an
      A2A Artifact that Gemini can consume.

The Octane MCP server exposes a generic API — all MCP tools are auto-populated
at startup via populate_registry_from_mcp().  Only local-only tools (tell_joke)
live in the seed registry.  Never hardcode entity-specific MCP tools here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from a2a_models import Artifact, Part, Message
from mcp_client import OctaneMcpClient

logger = logging.getLogger(__name__)

# ── Supported tool definitions (used by the router and the AgentCard) ── Fallback behavior: When the Opentext SDP MCP server is unreachable

# Seed registry — only local-only tools that are never served by the MCP server.
# MCP tools are populated at startup (and on refresh) by populate_registry_from_mcp().
TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "tell_joke": {
        "description": (
            "Tell a funny, light-hearted joke. Use this whenever the user asks for a joke, "
            "something funny, or wants to lighten the mood. Optionally pass a topic hint."
        ),
        "example_prompts": [
            "Tell me a joke",
            "Make me laugh",
        ],
        "default_arguments": {"topic": ""},
        "required": [],
        "_local_only": True,
    },
}

# Tools that exist locally in the agent and must never be dropped by MCP discovery
_LOCAL_ONLY_TOOLS: frozenset[str] = frozenset({"tell_joke"})

# Params auto-injected by mcp_client — never exposed to Gemini or the router
_EXCLUDED_MCP_PARAMS: frozenset[str] = frozenset({"sharedSpaceId", "workSpaceId"})

# The Octane MCP server ships incorrect descriptions for several parameters
# (all labelled "Shared Space ID"). These overrides correct them.
_PARAM_DESCRIPTION_OVERRIDES: dict[str, str] = {
    "entityId":      "Numeric ID of the entity to retrieve.",
    "fields":        "Comma-separated list of field names to include in the response (e.g. 'id,name,severity').",
    "search_fields": "Comma-separated list of fields to search within when using the keywords parameter.",
    "filter":        "AQQL filter string (e.g. \"severity EQ {id='list_node.severity.critical'}\"). Use ';' for AND, '||' for OR.",
    "keywords":      "Full-text search keywords. Required (use '*' as wildcard) when filter is set.",
    "entityType":    "Entity type identifier (e.g. 'defect', 'story', 'release'). Use get_entity_types to discover valid values.",
}


def populate_registry_from_mcp(tools: list[dict]) -> None:
    """
    Update TOOL_REGISTRY in-place with live tool definitions from the MCP server.

    Mutates the existing dict object (clear + update) so all importers that hold a
    reference via `from tool_router import TOOL_REGISTRY` see the new contents
    without needing to re-import.  Reassigning the name (TOOL_REGISTRY = {...})
    would silently leave stale references in other modules.

    Falls back silently if tools list is empty — built-in registry stays intact.
    """
    if not tools:
        return
    local_tools = {k: v for k, v in TOOL_REGISTRY.items() if k in _LOCAL_ONLY_TOOLS}
    new_entries = {
        t["name"]: {
            "description": t.get("description", ""),
            "example_prompts": [],
            "default_arguments": {
                k: None
                for k in t.get("inputSchema", {}).get("properties", {})
                if k not in _EXCLUDED_MCP_PARAMS
            },
            "required": [
                r for r in t.get("inputSchema", {}).get("required", [])
                if r not in _EXCLUDED_MCP_PARAMS
            ],
            # Full property schemas (with types) needed for dynamic ADK tool generation.
            # Apply description overrides for params where the MCP server is wrong.
            "inputSchema": {
                k: {**v, "description": _PARAM_DESCRIPTION_OVERRIDES.get(k, v.get("description", ""))}
                for k, v in t.get("inputSchema", {}).get("properties", {}).items()
                if k not in _EXCLUDED_MCP_PARAMS
            },
        }
        for t in tools
    }
    new_entries.update(local_tools)
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(new_entries)
    logger.info("TOOL_REGISTRY: loaded %d tools from MCP server (+%d local)", len(TOOL_REGISTRY) - len(local_tools), len(local_tools))


# ── Keyword-based intent matching (lightweight fallback, no LLM needed) ────
#
# This router is only used when no Gemini API key is configured.
# With Gemini active, all intent resolution is handled by the LLM agent.
# Keywords map to tool names as discovered from the MCP server — update these
# when the MCP server's tool API changes.


_INTENT_KEYWORDS: dict[str, list[str]] = {
    "get_entities": [
        "list", "show", "get all", "find", "search", "filter",
        "defects", "stories", "features", "requirements", "tasks",
        "my work", "my items", "assigned to me", "backlog",
    ],
    "get_entity": [
        "get", "fetch", "show me", "details", "info about",
    ],
}


def resolve_intent(user_text: str) -> str | None:
    """
    Keyword-based intent resolution.

    Priority order (highest first) is encoded in _INTENT_KEYWORDS dict order.
    When scores tie, the tool defined earlier in the dict wins, which gives
    write/comment tools precedence over bare entity-fetch tools.

    Returns the best-matching tool name, or None if nothing matched.
    """
    text_lower = user_text.lower()

    # Explicit JSON payload with a "tool" key bypasses scoring entirely
    if text_lower.strip().startswith("{"):
        try:
            payload = json.loads(user_text)
            if "tool" in payload and payload["tool"] in TOOL_REGISTRY:
                return payload["tool"]
        except json.JSONDecodeError:
            pass

    best_tool: str | None = None
    best_score: int = 0

    for tool, keywords in _INTENT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > best_score:
            best_score = score
            best_tool = tool
        # Ties: earlier entry (higher priority) wins — do nothing on equal score

    return best_tool  # None when best_score == 0


def _extract_entity_id(text: str) -> int | None:
    """Extract the first numeric ID (3+ digits) from free text."""
    import re
    match = re.search(r"#?(\d+)", text)
    return int(match.group(1)) if match else None


def _extract_entity_type(text: str) -> str | None:
    """Derive the Opentext SDP entityType string from keywords in free text."""
    tl = text.lower()
    if "user story" in tl or "story" in tl:
        return "story"
    if "feature" in tl:
        return "feature"
    if "defect" in tl or "bug" in tl:
        return "defect"
    return None


def extract_arguments(tool_name: str, message: Message) -> dict[str, Any]:
    """
    Pull tool-call arguments from the inbound A2A message.

    Supports two input styles:
      - **Structured**: a Part with mediaType "application/json" containing
        a data dict.
      - **Natural language**: extracts entity IDs and types by pattern matching.
    """
    # Try structured data parts first
    for part in message.parts:
        if part.data and isinstance(part.data, dict):
            return {**TOOL_REGISTRY[tool_name]["default_arguments"], **part.data}

    # Fall back to natural-language extraction from text parts
    text = " ".join(p.text for p in message.parts if p.text) or ""
    args: dict[str, Any] = dict(TOOL_REGISTRY[tool_name]["default_arguments"])

    if tool_name == "get_entity":
        args["entityId"] = _extract_entity_id(text)
        args["entityType"] = _extract_entity_type(text)

    elif tool_name == "get_entities":
        entity_type = _extract_entity_type(text)
        if entity_type:
            args["entityType"] = entity_type
        # filter extraction is left to the Gemini agent; keyword router sends no filter

    return args


# ── Execute and format ───────────────────────────────────────────────

async def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    mcp: OctaneMcpClient,
    *,
    bearer_token: str | None = None,
) -> Artifact:
    """
    Call the Opentext SDP MCP server and wrap the result in an A2A Artifact.
    """
    import time
    start = time.monotonic()
    logger.info("Routing tool=%s  args=%s", tool_name, arguments)

    result = await mcp.call_tool(tool_name, arguments, bearer_token=bearer_token)
    elapsed = time.monotonic() - start
    logger.info("Tool routing duration=%.3fs tool=%s", elapsed, tool_name)

    # The MCP result typically has a "content" list of {type, text} items
    content_items = result.get("content", [])

    # Build A2A artifact parts from the MCP content blocks
    parts: list[Part] = []
    for item in content_items:
        if item.get("type") == "text":
            # Try to parse as JSON for structured output
            try:
                parsed = json.loads(item["text"])
                parts.append(Part(data=parsed, mediaType="application/json"))
            except (json.JSONDecodeError, TypeError):
                parts.append(Part(text=item["text"]))
        else:
            parts.append(Part(text=str(item)))

    # Fallback: if MCP returned the result at top level (no content wrapper)
    if not parts:
        parts.append(Part(data=result, mediaType="application/json"))

    return Artifact(
        name=f"{tool_name}_result",
        description=f"Result from Opentext SDP tool: {tool_name}",
        parts=parts,
        metadata={"tool": tool_name, "arguments": arguments},
    )
