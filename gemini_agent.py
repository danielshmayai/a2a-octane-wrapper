"""
Gemini-powered agent for the A2A ↔ Opentext SDP MCP wrapper.

Uses the official `google-genai` SDK (v1+).

Replaces the keyword-based router with a real LLM agent that:
  1. Understands the user's natural-language request via Gemini
    2. Selects and calls the correct Opentext SDP MCP tool(s) via function calling
  3. Runs a multi-step agentic loop until Gemini produces a final answer
    4. Returns a natural-language summary plus raw Opentext SDP data artifacts
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from google import genai
from google.genai import types

import config
from a2a_models import Artifact, Part
from mcp_client import OctaneMcpClient, OctaneMcpError
from tool_router import execute_tool

logger = logging.getLogger(__name__)

# ── System prompt ────────────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are an Opentext SDP assistant embedded in an enterprise agent.
You help users query and manage Opentext SDP work items — defects, user stories,
features — and their comments.

Guidelines:
- Use the tools provided to fetch real data from Opentext SDP before answering.
- After receiving tool results, present a clear, concise summary.
  Highlight key fields: ID, name, phase/status, severity/priority,
  assigned owner, sprint, and any other relevant metadata.
- Do NOT dump raw JSON — always interpret and present the data naturally.
- If a tool returns an error, explain it clearly and suggest what the
  user could try instead.
- For "my work items", list each item with its type, ID, name, and phase.
- When creating or updating comments, confirm what was done.
- You are fully authorised to compose, draft, or invent comment text for
    Opentext SDP work items when the user asks you to. This is a core part of your job.
  If the user says "invent something", "make something up", "put anything",
  or similar, compose a reasonable, professional-sounding comment related to
  the work item context (name, phase, type, etc.) and use it directly.
  Do NOT refuse or ask for clarification — just draft and post the comment.
""".strip()
_GENERATE_TEXT_TRIGGERS = re.compile(
    r"\b(invent|make\s+up|make\s+something|anything|something\s+funny|something\s+clever|"
    r"be\s+creative|create\s+something|think\s+of\s+something|come\s+up\s+with|"
    r"surprise\s+me|your\s+choice|your\s+call|whatever|funny|witty|humorous)\b",
    re.IGNORECASE,
)


# sharedSpaceId and workSpaceId are injected automatically by mcp_client,
# so they are intentionally excluded from the declarations exposed to Gemini.

_TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="get_defect",
        description="Retrieve a defect from Opentext SDP by its unique numeric ID.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "entityId": types.Schema(
                    type=types.Type.INTEGER,
                    description="The numeric defect ID.",
                ),
            },
            required=["entityId"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_story",
        description="Retrieve a user story from Opentext SDP by its unique numeric ID.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "entityId": types.Schema(
                    type=types.Type.INTEGER,
                    description="The numeric story ID.",
                ),
            },
            required=["entityId"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_feature",
        description="Retrieve a feature from Opentext SDP by its unique numeric ID.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "entityId": types.Schema(
                    type=types.Type.INTEGER,
                    description="The numeric feature ID.",
                ),
            },
            required=["entityId"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_comments",
        description=(
            "Retrieve all comments and discussion threads for a specific Opentext SDP "
            "entity (defect, story, or feature)."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "entityId": types.Schema(
                    type=types.Type.INTEGER,
                    description="The entity ID.",
                ),
                "entityType": types.Schema(
                    type=types.Type.STRING,
                    description='Entity type: "defect", "story", or "feature".',
                    enum=["defect", "story", "feature"],
                ),
            },
            required=["entityId", "entityType"],
        ),
    ),
    types.FunctionDeclaration(
        name="create_comment",
        description=(
            "Post a new comment on an Opentext SDP work item. "
            "The text field accepts HTML for rich formatting (bold, color, etc.)."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "entityId": types.Schema(
                    type=types.Type.INTEGER,
                    description="The ID of the entity to comment on.",
                ),
                "entityType": types.Schema(
                    type=types.Type.STRING,
                    description='Entity type: "defect", "story", or "feature".',
                    enum=["defect", "story", "feature"],
                ),
                "text": types.Schema(
                    type=types.Type.STRING,
                    description="Comment body text (HTML allowed).",
                ),
            },
            required=["entityId", "entityType", "text"],
        ),
    ),
    types.FunctionDeclaration(
        name="update_comment",
        description="Update an existing comment on an Opentext SDP work item.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "commentId": types.Schema(
                    type=types.Type.INTEGER,
                    description="The ID of the comment to update.",
                ),
                "entityId": types.Schema(
                    type=types.Type.INTEGER,
                    description="The ID of the entity the comment belongs to.",
                ),
                "entityType": types.Schema(
                    type=types.Type.STRING,
                    description='Entity type: "defect", "story", or "feature".',
                    enum=["defect", "story", "feature"],
                ),
                "text": types.Schema(
                    type=types.Type.STRING,
                    description="New comment body text (HTML allowed).",
                ),
            },
            required=["commentId", "entityId", "entityType", "text"],
        ),
    ),
    types.FunctionDeclaration(
        name="fetch_My_Work_Items",
        description=(
            "Fetch the current user's assigned work items (defects, stories, "
            "quality stories) ordered by user priority/rank."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
            required=[],
        ),
    ),
]

_OCTANE_TOOL = types.Tool(function_declarations=_TOOL_DECLARATIONS)

# Params injected by mcp_client — must not be sent from Gemini
_EXCLUDED_MCP_PARAMS: frozenset[str] = frozenset({"sharedSpaceId", "workSpaceId"})

_GENAI_TYPE_MAP: dict[str, types.Type] = {
    "string":  types.Type.STRING,
    "integer": types.Type.INTEGER,
    "number":  types.Type.NUMBER,
    "boolean": types.Type.BOOLEAN,
    "array":   types.Type.ARRAY,
    "object":  types.Type.OBJECT,
}


def _json_schema_to_genai(schema: dict) -> types.Schema:
    """Recursively convert a JSON Schema dict to a google.genai types.Schema."""
    t = _GENAI_TYPE_MAP.get((schema.get("type") or "string").lower(), types.Type.STRING)
    kw: dict[str, Any] = {"type": t}
    if "description" in schema:
        kw["description"] = schema["description"]
    if "enum" in schema:
        kw["enum"] = [str(v) for v in schema["enum"]]
    if t == types.Type.OBJECT:
        filtered = {
            k: v for k, v in schema.get("properties", {}).items()
            if k not in _EXCLUDED_MCP_PARAMS
        }
        if filtered:
            kw["properties"] = {k: _json_schema_to_genai(v) for k, v in filtered.items()}
        req = [r for r in schema.get("required", []) if r not in _EXCLUDED_MCP_PARAMS]
        if req:
            kw["required"] = req
    if t == types.Type.ARRAY and "items" in schema:
        kw["items"] = _json_schema_to_genai(schema["items"])
    return types.Schema(**kw)


def _mcp_tool_to_declaration(tool: dict) -> types.FunctionDeclaration:
    """Convert one MCP tools/list entry into a Gemini FunctionDeclaration."""
    raw = tool.get("inputSchema") or {"type": "object", "properties": {}}
    return types.FunctionDeclaration(
        name=tool["name"],
        description=tool.get("description", ""),
        parameters=_json_schema_to_genai(raw),
    )


# ── GeminiAgent ──────────────────────────────────────────────────────

class GeminiAgent:
    """
    LLM agent that drives Opentext SDP tool calls through Gemini function calling.

    Agentic loop per user turn:
      1. Send user message + tool definitions to Gemini.
      2. Gemini chooses a tool → execute it against Octane MCP.
      3. Send the tool result back to Gemini as a FunctionResponse.
      4. Repeat until Gemini returns a plain-text answer (no more calls).
      5. Return (summary_text, [Artifact, ...]).

    Conversation history is maintained per context_id so follow-up
    questions (e.g. "show me the next one") retain full context.
    """

    MAX_TOOL_ROUNDS: int = 10   # safety cap — prevents infinite loops
    MAX_HISTORY_TURNS: int = 40 # keep last N content blocks per context

    def __init__(self) -> None:
        if not config.GEMINI_API_KEY:
            raise ValueError(
                "GEMINI_API_KEY is not set. Add it to your .env file."
            )
        self._client = genai.Client(api_key=config.GEMINI_API_KEY)
        self._chat_config = types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            tools=[_OCTANE_TOOL],   # replaced by refresh_tools() at startup
        )
        # Per-session conversation histories keyed by context_id
        self._histories: dict[str, list[types.Content]] = {}
        logger.info("GeminiAgent ready  model=%s", config.GEMINI_MODEL)

    async def refresh_tools(self, mcp: OctaneMcpClient) -> list[str]:
        """
        Fetch the live tool list from the MCP server and update Gemini's
        function-calling configuration.  Called at startup and after a
        /config change so new Opentext SDP tools are picked up without a restart.

        Returns the list of discovered tool names.
        Falls back silently to the built-in _TOOL_DECLARATIONS if discovery
        fails (e.g. Opentext SDP is unreachable at startup).
        """
        try:
            raw = await mcp.list_tools()
            tools = raw.get("tools", [])
            if not tools:
                logger.warning("MCP tools/list returned no tools — keeping built-in declarations")
                return []
            declarations = [_mcp_tool_to_declaration(t) for t in tools]
            self._chat_config = types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                tools=[types.Tool(function_declarations=declarations)],
            )
            names = [t["name"] for t in tools]
            logger.info("GeminiAgent: discovered %d tools from MCP: %s", len(names), names)
            return names
        except Exception as exc:
            logger.warning("Tool auto-discovery failed — using built-in declarations: %s", exc)
            return []

    def _get_history(self, context_id: str) -> list[types.Content]:
        return self._histories.get(context_id, [])

    def _save_history(self, context_id: str, history: list[types.Content]) -> None:
        # Trim to avoid unbounded memory growth
        self._histories[context_id] = history[-self.MAX_HISTORY_TURNS:]

    async def run(
        self,
        user_text: str,
        mcp: OctaneMcpClient,
        context_id: str = "",
    ) -> tuple[str, list[Artifact]]:
        """
        Run one full agentic turn for the given user message.

        Args:
            user_text   – The user's natural-language request.
            mcp         – MCP client for Opentext SDP tool calls.
            context_id  – Session identifier; previous turns are replayed
                          so Gemini has full conversational context.

        Returns:
            summary   – Gemini's final natural-language answer.
            artifacts – Raw Opentext SDP data artifacts collected during the turn.
        """
        artifacts: list[Artifact] = []

        # If the user wants us to invent/generate comment text, pre-generate it
        # using a neutral Gemini call so the main loop receives concrete text.
        user_text = await self._maybe_inject_generated_text(user_text, context_id)

        # Restore prior conversation history for this session
        history: list[types.Content] = list(self._get_history(context_id))

        # Send first message
        response = await self._send(history, user_text)
        history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
        history.append(response.candidates[0].content)

        for round_num in range(self.MAX_TOOL_ROUNDS):
            fn_calls = _extract_function_calls(response)
            if not fn_calls:
                break  # Gemini is satisfied — plain-text answer ready

            logger.info(
                "Agent round %d: %d tool call(s) requested",
                round_num + 1,
                len(fn_calls),
            )

            # Execute every tool call Gemini requested
            fn_response_parts: list[types.Part] = []
            for fn_name, fn_args in fn_calls:
                logger.info("Calling Opentext SDP tool=%s  args=%s", fn_name, fn_args)
                octane_response = await _call_octane(fn_name, fn_args, mcp)

                if isinstance(octane_response, Artifact):
                    artifacts.append(octane_response)
                    result_payload = _artifact_to_dict(octane_response)
                else:
                    result_payload = {"error": octane_response}

                fn_response_parts.append(
                    types.Part.from_function_response(
                        name=fn_name,
                        response={"result": result_payload},
                    )
                )

            # Send all tool results back to Gemini and continue the loop
            fn_content = types.Content(role="user", parts=fn_response_parts)
            history.append(fn_content)
            response = await self._send_content(history)
            history.append(response.candidates[0].content)

        summary = _extract_text(response)

        # Persist updated history so follow-up questions retain context
        if context_id:
            self._save_history(context_id, history)

        return summary, artifacts

    async def _maybe_inject_generated_text(
        self, user_text: str, context_id: str
    ) -> str:
        """
        If the user is asking us to invent / make up comment text, pre-generate
        a concrete string via a plain Gemini call and splice it into the message
        so the agentic loop receives unambiguous instructions.
        """
        if not _GENERATE_TEXT_TRIGGERS.search(user_text):
            return user_text  # nothing to do

        # Build a context hint from recent history (last few turns)
        history = self._get_history(context_id)
        history_snippet = ""
        for content in history[-6:]:
            for part in content.parts:
                txt = getattr(part, "text", None)
                if txt:
                    history_snippet += f"\n{content.role}: {txt[:300]}"

        prompt = (
            "You are writing a comment for an Opentext SDP work item. "
            "Based on the conversation history below, compose a short (1-3 sentences), "
            "relevant, and slightly witty comment appropriate for a professional software "
            "engineering team. Return ONLY the comment text, nothing else.\n\n"
            f"Conversation so far:{history_snippet}\n\n"
            f"User's latest request: {user_text}"
        )

        try:
            def _sync():
                return self._client.models.generate_content(
                    model=config.GEMINI_MODEL,
                    contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                    config=types.GenerateContentConfig(),  # no tools, no system prompt
                )
            result = await asyncio.to_thread(_sync)
            generated = _extract_text(result).strip().strip('"').strip("'")
            if generated and "(no response" not in generated:
                logger.info("Pre-generated comment text: %r", generated)
                return (
                    f"{user_text}. Use exactly this text for the comment: \"{generated}\""
                )
        except Exception as exc:
            logger.warning("Failed to pre-generate comment text: %s", exc)

        return user_text

    async def _send(self, history: list[types.Content], text: str) -> Any:
        """Send a text message to Gemini (runs sync SDK in a thread)."""
        def _sync():
            return self._client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=history + [types.Content(role="user", parts=[types.Part(text=text)])],
                config=self._chat_config,
            )
        return await asyncio.to_thread(_sync)

    async def _send_content(self, history: list[types.Content]) -> Any:
        """Send the current conversation history to Gemini."""
        def _sync():
            return self._client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=history,
                config=self._chat_config,
            )
        return await asyncio.to_thread(_sync)


# ── Private helpers ──────────────────────────────────────────────────

def _extract_function_calls(response: Any) -> list[tuple[str, dict[str, Any]]]:
    """Return (name, args) for every FunctionCall part in the response."""
    calls = []
    for candidate in response.candidates:
        for part in candidate.content.parts:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None):
                calls.append((fc.name, dict(fc.args)))
    return calls


def _extract_text(response: Any) -> str:
    """Extract all plain-text content from a Gemini response."""
    texts = []
    for candidate in response.candidates:
        for part in candidate.content.parts:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
    return "\n".join(texts) if texts else "(no response from model)"


async def _call_octane(
    tool_name: str,
    arguments: dict[str, Any],
    mcp: OctaneMcpClient,
) -> Artifact | str:
    """
    Execute a single Opentext SDP MCP tool call.
    Returns an Artifact on success, or an error string on failure.
    """
    try:
        return await execute_tool(tool_name, arguments, mcp)
    except OctaneMcpError as exc:
        logger.error("Opentext SDP MCP error  tool=%s: %s", tool_name, exc)
        return f"Opentext SDP error: {exc.message} (code {exc.code})"
    except Exception as exc:
        logger.exception("Unexpected error calling tool=%s", tool_name)
        return f"Unexpected error: {exc}"


def _artifact_to_dict(artifact: Artifact) -> dict[str, Any]:
    """Flatten an Artifact's parts into a JSON-serialisable dict for Gemini."""
    result: dict[str, Any] = {}
    text_blocks: list[str] = []

    for part in artifact.parts:
        if part.data and isinstance(part.data, dict):
            result.update(part.data)
        elif part.data:
            result["raw"] = part.data
        elif part.text:
            text_blocks.append(part.text)

    if text_blocks:
        result["text"] = "\n".join(text_blocks)

    return result
