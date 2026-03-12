"""
Tool Router – translates inbound A2A intents into Opentext SDP MCP tool calls.

The router does two things:
  1. **Intent extraction** – parses the A2A Message to figure out which
      Opentext SDP tool to call and with what arguments.
  2. **Result formatting** – converts the raw MCP tool result into an
      A2A Artifact that Gemini can consume.

Supported tools (matching the Opentext SDP MCP server's McpToolDescriptor beans):
  - get_defect          : Retrieve a single defect by entityId
  - get_story           : Retrieve a single story by entityId
  - get_feature         : Retrieve a single feature by entityId
  - get_comments        : Retrieve comments for an entity
  - create_comment      : Post a new comment on an entity
  - update_comment      : Edit an existing comment
  - fetch_My_Work_Items : List the current user's assigned work items
"""

from __future__ import annotations

import json
import logging
from typing import Any

from a2a_models import Artifact, Part, Message
from mcp_client import OctaneMcpClient

logger = logging.getLogger(__name__)

# ── Supported tool definitions (used by the router and the AgentCard) ── Fallback behavior: When the Opentext SDP MCP server is unreachable

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_defect": {
        "description": "Retrieve a defect raw data from Opentext SDP by its unique identifier.",
        "example_prompts": [
            "Get defect 2110",
            "Show me bug #9001",
            "Fetch defect details for 42",
        ],
        "default_arguments": {"entityId": None},
        "required": ["entityId"],
    },
    "get_story": {
        "description": "Retrieve a story raw data from Opentext SDP by its unique identifier.",
        "example_prompts": [
            "Get story 1234",
            "Show me user story 55",
        ],
        "default_arguments": {"entityId": None},
        "required": ["entityId"],
    },
    "get_feature": {
        "description": "Retrieve raw data for a feature from Opentext SDP by its unique identifier.",
        "example_prompts": [
            "Get feature 77",
            "Show me feature 200",
        ],
        "default_arguments": {"entityId": None},
        "required": ["entityId"],
    },
    "get_comments": {
        "description": (
            "Retrieve all comments and discussion threads for a specific entity "
            "(defect, story, or feature)."
        ),
        "example_prompts": [
            "Show comments on defect 2110",
            "Get the discussion for story 55",
            "List all comments on feature 77",
        ],
        "default_arguments": {"entityId": None, "entityType": None},
        "required": ["entityId", "entityType"],
    },
    "create_comment": {
        "description": (
            "Creates a comment for the specified work item. "
            "The text parameter accepts HTML for rich formatting."
        ),
        "example_prompts": [
            "Add a comment to defect 2110 saying 'Reproduced on build 5.3'",
            "Comment on story 55: needs clarification",
        ],
        "default_arguments": {"entityId": None, "entityType": None, "text": ""},
        "required": ["entityId", "entityType", "text"],
    },
    "update_comment": {
        "description": (
            "Updates an existing comment on a work item. "
            "The text parameter accepts HTML for rich formatting."
        ),
        "example_prompts": [
            "Update comment 99 on defect 2110 with new text",
            "Edit comment 12 on story 55",
        ],
        "default_arguments": {"commentId": None, "entityId": None, "entityType": None, "text": ""},
        "required": ["commentId", "entityId", "entityType", "text"],
    },
    "fetch_My_Work_Items": {
        "description": (
            "Fetches the current user's assigned work items (stories, defects, quality stories) "
            "including metadata like ID, name, phase, priority, story points, sprint, owner, severity. "
            "Results are ordered by user priority/rank."
        ),
        "example_prompts": [
            "What are my work items?",
            "Show my assigned defects and stories",
            "Fetch my backlog",
        ],
        "default_arguments": {},
        "required": [],
    },
}

# Params auto-injected by mcp_client — never exposed to Gemini or the router
_EXCLUDED_MCP_PARAMS: frozenset[str] = frozenset({"sharedSpaceId", "workSpaceId"})


def populate_registry_from_mcp(tools: list[dict]) -> None:
    """
    Replace TOOL_REGISTRY with live tool definitions fetched from the MCP server.

    Falls back silently — if 'tools' is empty the existing built-in registry
    is left intact so the keyword router keeps working.
    """
    global TOOL_REGISTRY
    if not tools:
        return
    TOOL_REGISTRY = {
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
        }
        for t in tools
    }
    logger.info("TOOL_REGISTRY: loaded %d tools from MCP server", len(TOOL_REGISTRY))


# ── Keyword-based intent matching (lightweight, no LLM needed) ─────

# Entity-type keywords used for comment/get routing
_ENTITY_TYPE_KEYWORDS: dict[str, str] = {
    "defect": "defect",
    "bug": "defect",
    "story": "story",
    "user story": "story",
    "feature": "feature",
}

_INTENT_KEYWORDS: dict[str, list[str]] = {
    "fetch_My_Work_Items": [
        "my work", "my items", "my defects", "my stories", "my backlog",
        "assigned to me", "my tasks", "my features", "fetch my",
    ],
    "get_comments": [
        "comments", "comment", "discussion", "discussions", "thread",
        "feedback", "notes",
    ],
    "create_comment": [
        "add comment", "create comment", "post comment", "write comment",
        "add a comment", "post a comment", "comment saying", "comment:",
    ],
    "update_comment": [
        "update comment", "edit comment", "change comment", "modify comment",
    ],
    "get_defect": [
        "defect", "bug",
    ],
    "get_story": [
        "story", "user story",
    ],
    "get_feature": [
        "feature",
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

    if tool_name in ("get_defect", "get_story", "get_feature"):
        args["entityId"] = _extract_entity_id(text)

    elif tool_name == "get_comments":
        args["entityId"] = _extract_entity_id(text)
        args["entityType"] = _extract_entity_type(text)

    elif tool_name == "create_comment":
        args["entityId"] = _extract_entity_id(text)
        args["entityType"] = _extract_entity_type(text)
        # Try to pull quoted text as the comment body
        import re
        quoted = re.search(r"['\"](.+?)['\"]", text)
        if quoted:
            args["text"] = quoted.group(1)
        else:
            # Everything after "saying", ":", or "comment" keyword
            m = re.search(r"(?:saying|comment:|:)\s*(.+)$", text, re.IGNORECASE)
            args["text"] = m.group(1).strip() if m else ""

    elif tool_name == "update_comment":
        args["entityId"] = _extract_entity_id(text)
        args["entityType"] = _extract_entity_type(text)
        # commentId is typically a smaller number — take the second numeric match
        import re
        ids = re.findall(r"#?(\d+)", text)
        if len(ids) >= 2:
            args["commentId"] = int(ids[0])
            args["entityId"] = int(ids[1])
        elif ids:
            args["commentId"] = int(ids[0])
        quoted = re.search(r"['\"](.+?)['\"]", text)
        if quoted:
            args["text"] = quoted.group(1)

    # fetch_My_Work_Items needs no extra arguments beyond the injected context

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
    logger.info("Routing tool=%s  args=%s", tool_name, arguments)

    result = await mcp.call_tool(tool_name, arguments, bearer_token=bearer_token)

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
    )
