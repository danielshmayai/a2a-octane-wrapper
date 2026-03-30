"""
Gemini-powered agent for the A2A ↔ Opentext SDP MCP wrapper.

Uses google-adk (Google Agent Development Kit) with:
  1. LlmAgent — owns the system prompt, tools, and the multi-step function-calling loop
  2. Runner + InMemorySessionService — drives each turn and keeps per-session history
  3. Typed async Python functions (one per MCP tool) — ADK infers Gemini schemas from them
  4. Per-run artifact list captured via closure — same A2A Artifact output as before
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any

from google import genai
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

import config
from a2a_models import Artifact
from mcp_client import OctaneMcpClient, OctaneMcpError
from tool_router import TOOL_REGISTRY, _EXCLUDED_MCP_PARAMS, execute_tool

logger = logging.getLogger(__name__)

# ── System prompt ────────────────────────────────────────────────────

_SYSTEM_PROMPT_BASE = """
You are an expert Opentext SDP (ALM Octane) assistant embedded in an enterprise agent.
You have deep knowledge of Octane concepts: entity types (defects, stories, features,
requirements, epics, tasks, quality stories, etc.), fields, phases, sprints, releases,
teams, comments, and the Octane query language.

Your two modes of operation:
A) LIVE DATA — use the MCP tools listed below to fetch or write real Octane data.
B) KNOWLEDGE — answer conceptual or how-to questions from your Octane expertise,
   even without calling a tool. Never refuse a question just because no tool matches.

Core rules:
  from knowledge if no tool is needed; call a tool only when live data is required.
  If you are unsure which tool to use, reason step-by-step through the tool list.
For ANY question about Octane concepts, entities, fields, or workflows: answer
    from knowledge if no tool is needed; call a tool only when live data is required.
For requests that need live data: ALWAYS prefer calling a tool over guessing.
If you are unsure which tool to use, reason step-by-step through the tool list.
If the user types a tool name directly, call that tool with sensible defaults.
If the user provides a filter expression (in natural language or JSON), ALWAYS pass it to the `filter` argument of the relevant tool (such as `get_entities`).
After receiving tool results, ALWAYS present a clear, concise summary in natural language, even if only tool calls were made. Do NOT dump raw JSON. If you have no additional insights, briefly summarize what the tool(s) returned.
If a tool returns an error, explain it and suggest an alternative.

Reasoning workflow for queries with filters or unfamiliar entity types:
1. Call a DISCOVERY tool first to learn entity type identifiers, field names,
   data types, allowed enum values, and filter operators.
2. Build correct filter arguments from that schema knowledge.
3. Execute the query and present results.
Never guess field names, entity type identifiers, or filter syntax — discover first.
Chain as many tool calls as needed.

Octane query language (AQQL) — critical rules:
- Operators: EQ, LT, GT, LE, GE, IN, BTW — NOT != or <>
- AND = ;   OR = ||   NOT = !()  wrapping the expression
- Reference fields use {id='<value_id>'} not bare strings
  e.g.  severity EQ {id='list_node.severity.high'}
- Negation wraps the whole condition: !(phase EQ {id='list_node.defect.phase.closed'})
- Combined example: (severity EQ {id='list_node.severity.high'} || severity EQ {id='list_node.severity.critical'}) ; !(phase EQ {id='list_node.defect.phase.closed'}) ; release EQ {id=2005}
- List reference IDs follow the pattern list_node.<entity_type>.<field_name>.<value_name>
  — discover the exact IDs from get_entity_field_metadata or get_filter_metadata before using them
- NEVER use != — it is not valid Octane AQQL syntax

Presenting tool lists (when user asks "what tools / capabilities do you have"):
- Format as a markdown table: | Tool | Description |
- Group into: DISCOVERY tools (call first), READ/QUERY tools, WRITE tools.
- After the table, add a short "Notes from your setup" section mentioning
  workSpaceId and sharedSpaceId values, explaining the generic CRUD model, and
  suggesting the recommended workflow (get_entity_types → get_entity_field_metadata
  → get_entities with filter).
- End with a proactive offer, e.g. "Want me to run get_entity_types now to see
  what's available in your workspace?"

Presenting query results:
- Use bullet lists or tables, not raw JSON.
- Highlight key fields: ID, name, type, phase/status, owner, priority.
- If results are empty, say so clearly and suggest a broader query.

Context across turns:
- Resolve "next/previous/same/that/it" from prior conversation history.
  Act on inference; only ask if truly ambiguous.

Writing:
- Compose and post comment text directly when asked — do not refuse or ask for approval.

Jokes:
- Call tell_joke immediately when the user wants to lighten the mood.
""".strip()


def _build_system_prompt() -> str:
    """
    Extend the base prompt with a live tool catalogue built from TOOL_REGISTRY.

    Tools are grouped into three categories inferred from their names:
      - DISCOVERY / SCHEMA  — help the agent learn field names, filter syntax, enums
      - READ / QUERY        — fetch or search data
      - WRITE               — create, update, delete

    The categories are listed explicitly so the agent knows which tools to call
    first when it needs to construct a filter or work with an unfamiliar entity type.
    """
    discovery_kw = {"type", "field", "metadata", "syntax", "filter", "schema", "discover"}
    write_kw     = {"create", "update", "delete", "add", "post", "edit", "modify", "set"}

    discovery: list[tuple[str, str]] = []
    query:     list[tuple[str, str]] = []
    write:     list[tuple[str, str]] = []

    for name, defn in TOOL_REGISTRY.items():
        if defn.get("_local_only"):
            continue
        nl = name.lower()
        desc = (defn.get("description") or "")[:120].rstrip()
        if any(k in nl for k in discovery_kw):
            discovery.append((name, desc))
        elif any(k in nl for k in write_kw):
            write.append((name, desc))
        else:
            query.append((name, desc))

    lines: list[str] = [
        "",
        "---",
        "Here is a summary of the tools available for interacting with your Opentext SDP (ALM Octane) instance:",
    ]

    def table_block(tools: list[tuple[str, str]], header: str) -> list[str]:
        if not tools:
            return []
        block = [f"\n**{header}**",
                 "| Tool | Description |",
                 "|------|-------------|"]
        for n, d in tools:
            block.append(f"| `{n}` | {d} |")
        return block

    lines += table_block(discovery, "DISCOVERY tools (call first)")
    lines += table_block(query, "READ/QUERY tools")
    lines += table_block(write, "WRITE tools")

    if not (discovery or query or write):
        lines.append(
            "  (No live MCP tools are loaded yet. Answer from your Octane knowledge "
            "for conceptual questions. For requests that need live data, let the user "
            "know the MCP connection is not yet established and they can retry shortly.)"
        )

    if discovery:
        first = discovery[0][0]
        lines.append(
            f"\nFor any request involving a filter or entity type you haven't seen before, "
            f"start with `{first}` (or equivalent) before calling a query tool."
        )

    lines.append(
        f"\nSetup context (cite these when presenting tool capabilities to the user):\n"
        f"  workSpaceId={config.DEFAULT_WORKSPACE_ID}  "
        f"sharedSpaceId={config.DEFAULT_SHARED_SPACE_ID}"
    )

    return _SYSTEM_PROMPT_BASE + "\n" + "\n".join(lines)
_GENERATE_TEXT_TRIGGERS = re.compile(
    r"\b(invent|make\s+up|make\s+something|anything|something\s+funny|something\s+clever|"
    r"be\s+creative|create\s+something|think\s+of\s+something|come\s+up\s+with|"
    r"surprise\s+me|your\s+choice|your\s+call|whatever|funny|witty|humorous)\b",
    re.IGNORECASE,
)


# ── Dynamic tool factory ────────────────────────────────────────────

_JSON_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _make_dynamic_tool_fn(
    tool_name: str,
    description: str,
    input_schema_props: dict,
    required_params: list[str],
    mcp: OctaneMcpClient,
    artifacts: list[Artifact],
    bearer_token: str | None,
    mcp_called_flag: list[bool],
) -> Any:
    """Build a typed async callable from an MCP tool's JSON Schema for ADK ingestion.

    Sets __signature__ and __annotations__ explicitly so ADK can infer a correct
    Gemini FunctionDeclaration schema without any static function definition.
    """
    async def _fn(**kwargs: Any) -> str:
        return await _invoke(tool_name, kwargs, mcp, artifacts, bearer_token, mcp_called_flag)

    required_set = set(required_params)
    sig_params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}

    for param_name, param_schema in input_schema_props.items():
        if param_name in _EXCLUDED_MCP_PARAMS:
            continue
        py_type = _JSON_TO_PY.get(param_schema.get("type", "string"), str)
        default = inspect.Parameter.empty if param_name in required_set else None
        sig_params.append(inspect.Parameter(
            param_name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=py_type,
            default=default,
        ))
        annotations[param_name] = py_type

    # Required params (no default) must precede optional params (with default)
    # to form a valid Python signature — MCP schema order is not guaranteed.
    sig_params.sort(key=lambda p: p.default is not inspect.Parameter.empty)

    _fn.__signature__ = inspect.Signature(sig_params, return_annotation=str)
    _fn.__annotations__ = {**annotations, "return": str}
    _fn.__name__ = tool_name
    _fn.__qualname__ = tool_name
    _fn.__doc__ = description
    return _fn


# ── ADK tool functions ──────────────────────────────────────────────
#
# Each function is typed so google-adk can automatically infer a Gemini
# FunctionDeclaration schema. They close over `mcp` and `_artifacts` so
# the runner can call them directly without extra wiring.
# sharedSpaceId / workSpaceId are injected by the MCP client automatically.

def _build_tools(
    mcp: OctaneMcpClient,
    artifacts: list[Artifact],
    bearer_token: str | None = None,
    mcp_called_flag: list[bool] | None = None,
) -> list:
    """
    Build the ADK tool list entirely from TOOL_REGISTRY (populated by MCP discovery).

    Local-only tools (e.g. tell_joke) are hardcoded here because they never
    appear on the MCP server. Every MCP tool is generated dynamically so the
    agent stays in sync with whatever the server currently exposes.
    """

    async def tell_joke(topic: str = "") -> str:
        """Tell a funny, light-hearted joke.
        Use whenever the user asks for a joke or wants to lighten the mood.
        Pass a topic hint if context is available (e.g. 'requirements', 'defects').
        This tool does NOT call the MCP server — it generates the joke locally.
        """
        return await _generate_joke(topic)

    tools: list = [tell_joke]

    for tool_name, tool_def in TOOL_REGISTRY.items():
        if tool_def.get("_local_only"):
            continue  # tell_joke already added above
        dynamic_fn = _make_dynamic_tool_fn(
            tool_name,
            tool_def.get("description", ""),
            tool_def.get("inputSchema", {}),
            tool_def.get("required", []),
            mcp, artifacts, bearer_token, mcp_called_flag,
        )
        tools.append(dynamic_fn)
        logger.debug("Registered MCP tool: %s", tool_name)

    logger.info("_build_tools: %d tools total (%d MCP + tell_joke)", len(tools), len(tools) - 1)
    return tools


async def _invoke(
    tool_name: str,
    arguments: dict[str, Any],
    mcp: OctaneMcpClient,
    artifacts: list[Artifact],
    bearer_token: str | None = None,
    mcp_called_flag: list[bool] | None = None,
) -> str:
    """Execute one MCP tool call, append its artifact, return text for Gemini."""
    try:
        artifact = await execute_tool(tool_name, arguments, mcp, bearer_token=bearer_token)
        artifacts.append(artifact)
        if mcp_called_flag is not None:
            mcp_called_flag[0] = True
        texts = [p.text for p in artifact.parts if p.text]
        raw   = [str(p.data) for p in artifact.parts if p.data and not p.text]
        return "\n".join(texts + raw) or str(artifact)
    except OctaneMcpError as exc:
        logger.error("SDP MCP error  tool=%s: %s", tool_name, exc)
        return f"Opentext SDP error: {exc.message} (code {exc.code})"
    except Exception as exc:
        logger.exception("Unexpected error calling tool=%s", tool_name)
        return f"Unexpected error: {exc}"


# ── GeminiAgent (google-adk) ─────────────────────────────────────────

class GeminiAgent:
    """
    ADK-powered agent that drives Opentext SDP tool calls via Gemini.

    Architecture:
    - google-adk LlmAgent owns the system prompt, tool declarations, and the
      multi-step function-calling loop (no more manual MAX_TOOL_ROUNDS loop).
    - Runner + InMemorySessionService manage per-session conversation history
      automatically (replaces the manual _histories dict).
    - Tool functions are typed async closures; ADK infers Gemini schemas from
      Python type annotations so no FunctionDeclaration boilerplate is needed.
    - A2A Artifacts are collected into a per-run list that the closures write
      into via closure capture (list is .clear()'d, never reassigned).
    """

    def __init__(self) -> None:
        if not config.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not set. Add it to your .env file.")
        self._session_service = InMemorySessionService()
        self._runner: Runner | None = None
        self._current_mcp: OctaneMcpClient | None = None
        # Shared artifact list and MCP-called flag — closures hold references to
        # these objects. Use .clear() / [0]=False in run(), never reassign.
        self._run_artifacts: list[Artifact] = []
        self._run_mcp_called: list[bool] = [False]
        logger.info("GeminiAgent (ADK) ready  model=%s", config.GEMINI_MODEL)

    def _rebuild_runner(self, mcp: OctaneMcpClient, bearer_token: str | None = None) -> None:
        """Construct a fresh LlmAgent + Runner bound to *mcp*."""
        tools = _build_tools(mcp, self._run_artifacts, bearer_token, self._run_mcp_called)
        agent = LlmAgent(
            name="ot_adm_agent",
            model=config.GEMINI_MODEL,
            instruction=_build_system_prompt(),
            tools=tools,
        )
        self._runner = Runner(
            app_name="ot_adm_agent",
            agent=agent,
            session_service=self._session_service,
        )
        self._current_mcp = mcp

    async def refresh_tools(self, mcp: OctaneMcpClient) -> list[str]:
        """
        Rebuild the ADK Runner bound to the current MCP client.
        Called at startup and after /config changes.

        Verifies connectivity by listing live tools; falls back gracefully
        if the Opentext SDP server is unreachable at startup.
        """
        self._rebuild_runner(mcp)
        # Always derive the full tool list from _build_tools so that local-only
        # tools (e.g. tell_joke) are never dropped from the reported list.
        all_names = [fn.__name__ for fn in _build_tools(mcp, [])]
        try:
            raw = await mcp.list_tools()
            mcp_names = [t["name"] for t in raw.get("tools", [])]
            logger.info(
                "GeminiAgent (ADK): %d MCP tools confirmed: %s", len(mcp_names), mcp_names
            )
        except Exception as exc:
            logger.warning("Tool discovery failed (agent still operational): %s", exc)
        return all_names

    async def run(
        self,
        user_text: str,
        mcp: OctaneMcpClient,
        context_id: str = "",
        bearer_token: str | None = None,
    ) -> tuple[str, list[Artifact]]:
        """
        Run one full agentic turn for the given user message.

        ADK's Runner handles the multi-step function-calling loop and stores
        per-session history automatically (keyed by session_id = context_id).

        Returns:
            summary   – Gemini's final natural-language answer.
            artifacts – Raw Opentext SDP data collected during this turn.
        """
        # Rebuild the runner each turn so tool closures capture the current bearer_token.
        self._rebuild_runner(mcp, bearer_token)
        self._current_mcp = mcp

        # Reset per-run state (.clear() / [0]=False keeps the same list objects
        # so the tool closures built in _rebuild_runner still point to them).
        self._run_artifacts.clear()
        self._run_mcp_called[0] = False

        user_text = await self._maybe_inject_generated_text(user_text)
        message = types.Content(role="user", parts=[types.Part(text=user_text)])
        session_id = context_id or "default"
        summary = ""

        # Ensure the session exists — newer ADK versions removed auto_create_session
        # from Runner, so we create it explicitly if it doesn't exist yet.
        existing = await self._session_service.get_session(
            app_name="ot_adm_agent", user_id="a2a_user", session_id=session_id
        )
        if existing is None:
            await self._session_service.create_session(
                app_name="ot_adm_agent", user_id="a2a_user", session_id=session_id
            )

        async for event in self._runner.run_async(
            user_id="a2a_user",
            session_id=session_id,
            new_message=message,
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    summary = "".join(
                        p.text
                        for p in event.content.parts
                        if hasattr(p, "text") and p.text
                    )
                break

        # Fallback: if summary is empty but artifacts exist, synthesize a summary from artifacts
        if not summary and self._run_artifacts:
            artifact_summaries = []
            for art in self._run_artifacts:
                name = art.name or "(unnamed)"
                # Try to extract a short preview from the first part
                preview = ""
                if art.parts:
                    part = art.parts[0]
                    if hasattr(part, "text") and part.text:
                        preview = part.text.strip().replace("\n", " ")[:120]
                artifact_summaries.append(f"- {name}: {preview}" if preview else f"- {name}")
            summary = "Tool results returned.\n" + "\n".join(artifact_summaries)
        return summary or "(no response from model)", list(self._run_artifacts), self._run_mcp_called[0]

    async def _maybe_inject_generated_text(self, user_text: str) -> str:
        """
        If the user asks for invented comment text, pre-generate a concrete
        string via a plain Gemini call and splice it into the message so the
        agentic loop receives unambiguous instructions.
        """
        if not _GENERATE_TEXT_TRIGGERS.search(user_text):
            return user_text

        prompt = (
            "You are writing a comment for an Opentext SDP work item. "
            "Compose a short (1-3 sentences), relevant, and slightly witty comment "
            "appropriate for a professional software engineering team. "
            "Return ONLY the comment text, nothing else.\n\n"
            f"User's request: {user_text}"
        )
        try:
            client = genai.Client(api_key=config.GEMINI_API_KEY)

            def _sync():
                return client.models.generate_content(
                    model=config.GEMINI_MODEL,
                    contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                    config=types.GenerateContentConfig(),
                )

            # Run the blocking genai call on a thread but bound by a configured timeout
            result = await asyncio.wait_for(
                asyncio.to_thread(_sync), timeout=config.GEMINI_REQUEST_TIMEOUT_SECONDS
            )
            generated = _extract_text(result).strip().strip('"').strip("'")
            if generated and "(no response" not in generated:
                logger.info("Pre-generated comment text: %r", generated)
                return f'{user_text}. Use exactly this text for the comment: "{generated}"'
        except Exception as exc:
            logger.warning("Failed to pre-generate comment text: %s", exc)

        return user_text


# ── Private helpers ──────────────────────────────────────────────────

async def _generate_joke(topic: str = "") -> str:
    """Generate a contextual joke via a direct Gemini call (no MCP involved)."""
    topic_hint = f" The joke should be related to: {topic}." if topic else ""
    prompt = (
        "You are a witty software engineer with a great sense of humour. "
        "Tell a single short joke (2–4 lines max) that would make a developer laugh.%s "
        "After the joke, add exactly this line on its own: "
        "'— 🤖 My personal agent joke. No MCP servers were harmed in the making of this joke.'"
        % topic_hint
    )
    try:
        client = genai.Client(api_key=config.GEMINI_API_KEY)

        def _sync():
            return client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                config=types.GenerateContentConfig(),
            )
        # Run the blocking genai call on a thread but bound by a configured timeout
        result = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=config.GEMINI_REQUEST_TIMEOUT_SECONDS)
        joke = _extract_text(result).strip()
        if joke and "(no response" not in joke:
            logger.info("Generated joke (topic=%r)", topic)
            return joke
    except Exception as exc:
        logger.warning("Failed to generate joke: %s", exc)

    return (
        "Why do programmers prefer dark mode?\n"
        "Because light attracts bugs! 🐛\n\n"
        "— 🤖 My personal agent joke. No MCP servers were harmed in the making of this joke."
    )


def _extract_text(response: Any) -> str:
    """Extract all plain-text parts from a google-genai GenerateContentResponse."""
    texts = []
    for candidate in response.candidates:
        for part in candidate.content.parts:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
    return "\n".join(texts) if texts else "(no response from model)"
