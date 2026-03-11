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
import logging
import re
from typing import Any

from google import genai
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
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


# ── ADK tool functions ──────────────────────────────────────────────
#
# Each function is typed so google-adk can automatically infer a Gemini
# FunctionDeclaration schema. They close over `mcp` and `_artifacts` so
# the runner can call them directly without extra wiring.
# sharedSpaceId / workSpaceId are injected by the MCP client automatically.

def _build_tools(mcp: OctaneMcpClient, artifacts: list[Artifact]) -> list:
    """Return a list of typed async functions, one per Opentext SDP MCP tool."""

    async def get_defect(entityId: int) -> str:
        """Retrieve a defect from Opentext SDP by its unique numeric ID."""
        return await _invoke("get_defect", {"entityId": entityId}, mcp, artifacts)

    async def get_story(entityId: int) -> str:
        """Retrieve a user story from Opentext SDP by its unique numeric ID."""
        return await _invoke("get_story", {"entityId": entityId}, mcp, artifacts)

    async def get_feature(entityId: int) -> str:
        """Retrieve a feature from Opentext SDP by its unique numeric ID."""
        return await _invoke("get_feature", {"entityId": entityId}, mcp, artifacts)

    async def get_comments(entityId: int, entityType: str) -> str:
        """Retrieve all comments for an entity (defect, story, or feature).
        entityType must be one of: 'defect', 'story', 'feature'.
        """
        return await _invoke(
            "get_comments", {"entityId": entityId, "entityType": entityType},
            mcp, artifacts,
        )

    async def create_comment(entityId: int, entityType: str, text: str) -> str:
        """Post a new comment on an Opentext SDP work item. HTML is allowed in text.
        entityType must be one of: 'defect', 'story', 'feature'.
        """
        return await _invoke(
            "create_comment",
            {"entityId": entityId, "entityType": entityType, "text": text},
            mcp, artifacts,
        )

    async def update_comment(
        commentId: int, entityId: int, entityType: str, text: str
    ) -> str:
        """Update an existing comment on an Opentext SDP work item. HTML allowed.
        entityType must be one of: 'defect', 'story', 'feature'.
        """
        return await _invoke(
            "update_comment",
            {"commentId": commentId, "entityId": entityId,
             "entityType": entityType, "text": text},
            mcp, artifacts,
        )

    async def fetch_My_Work_Items() -> str:
        """Fetch the current user's assigned work items (defects, stories, tasks)."""
        return await _invoke("fetch_My_Work_Items", {}, mcp, artifacts)

    return [
        get_defect, get_story, get_feature, get_comments,
        create_comment, update_comment, fetch_My_Work_Items,
    ]


async def _invoke(
    tool_name: str,
    arguments: dict[str, Any],
    mcp: OctaneMcpClient,
    artifacts: list[Artifact],
) -> str:
    """Execute one MCP tool call, append its artifact, return text for Gemini."""
    try:
        artifact = await execute_tool(tool_name, arguments, mcp)
        artifacts.append(artifact)
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
        # Shared artifact list — closures hold a reference to this list object.
        # Use .clear() in run(), never reassign, so closures always see the same list.
        self._run_artifacts: list[Artifact] = []
        logger.info("GeminiAgent (ADK) ready  model=%s", config.GEMINI_MODEL)

    def _rebuild_runner(self, mcp: OctaneMcpClient) -> None:
        """Construct a fresh LlmAgent + Runner bound to *mcp*."""
        tools = _build_tools(mcp, self._run_artifacts)
        agent = LlmAgent(
            name="ot_adm_agent",
            model=config.GEMINI_MODEL,
            instruction=_SYSTEM_PROMPT,
            tools=tools,
        )
        self._runner = Runner(
            app_name="ot_adm_agent",
            agent=agent,
            session_service=self._session_service,
            auto_create_session=True,
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
        try:
            raw = await mcp.list_tools()
            names = [t["name"] for t in raw.get("tools", [])]
            logger.info(
                "GeminiAgent (ADK): %d MCP tools confirmed: %s", len(names), names
            )
            return names
        except Exception as exc:
            logger.warning("Tool discovery failed (agent still operational): %s", exc)
            return [fn.__name__ for fn in _build_tools(mcp, [])]

    async def run(
        self,
        user_text: str,
        mcp: OctaneMcpClient,
        context_id: str = "",
    ) -> tuple[str, list[Artifact]]:
        """
        Run one full agentic turn for the given user message.

        ADK's Runner handles the multi-step function-calling loop and stores
        per-session history automatically (keyed by session_id = context_id).

        Returns:
            summary   – Gemini's final natural-language answer.
            artifacts – Raw Opentext SDP data collected during this turn.
        """
        if self._runner is None or mcp is not self._current_mcp:
            await self.refresh_tools(mcp)

        # Reset per-run artifact list (.clear() keeps the same list object so
        # the tool closures built in _rebuild_runner still point to it).
        self._run_artifacts.clear()

        user_text = await self._maybe_inject_generated_text(user_text)
        message = types.Content(role="user", parts=[types.Part(text=user_text)])
        session_id = context_id or "default"
        summary = ""

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

        return summary or "(no response from model)", list(self._run_artifacts)

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

            result = await asyncio.to_thread(_sync)
            generated = _extract_text(result).strip().strip('"').strip("'")
            if generated and "(no response" not in generated:
                logger.info("Pre-generated comment text: %r", generated)
                return f'{user_text}. Use exactly this text for the comment: "{generated}"'
        except Exception as exc:
            logger.warning("Failed to pre-generate comment text: %s", exc)

        return user_text


# ── Private helpers ──────────────────────────────────────────────────

def _extract_text(response: Any) -> str:
    """Extract all plain-text parts from a google-genai GenerateContentResponse."""
    texts = []
    for candidate in response.candidates:
        for part in candidate.content.parts:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
    return "\n".join(texts) if texts else "(no response from model)"
