"""
E2E tests for the A2A agent's MCP tool-calling logic.

Uses DeepEval with Gemini 2.0 Flash as the evaluation model.

Test strategy (three tiers):
  1. TestToolCorrectness — Pure pytest assertions verify the keyword-based
     fallback router selects the right MCP tool and extracts the right args.
     The Octane MCP server exposes a generic API: get_entity (single entity
     by entityType + entityId) and get_entities (list with optional filter).
     Zero API cost — MCP is fully mocked.

  2. TestToolCorrectnessDeepEval — DeepEval's ToolCorrectnessMetric formally
     validates tool-call expectations using the Gemini judge.

  3. TestAnswerRelevancy — DeepEval's AnswerRelevancyMetric asks Gemini to
     judge whether the agent's final natural-language response is relevant
     to the user's original question.

Architecture (keyword fallback path — no Gemini API key required):
  User text  →  resolve_intent()  →  extract_arguments()  →  execute_tool()  →  mock MCP
                                                                    ↓
                                                             A2A Task response

The MCP server is fully mocked — no network calls to Opentext SDP.
For the full agentic path (Gemini + real MCP), see tests/e2e/test_preflight.py.

Run:
    pytest tests/e2e/test_mcp_agent.py -v
    pytest tests/e2e/test_mcp_agent.py -k "not DeepEval and not Relevancy" -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from deepeval import assert_test
from deepeval.metrics import AnswerRelevancyMetric, ToolCorrectnessMetric
from deepeval.test_case import LLMTestCase, ToolCall

import a2a_models
import main


# ── Mock MCP tool schemas ─────────────────────────────────────────────
# The Octane MCP server exposes a generic API.  These schemas match what
# populate_registry_from_mcp() expects from the real server's list_tools()
# response so the keyword router can look up default_arguments correctly.

MOCK_MCP_TOOL_DEFS = [
    {
        "name": "get_entity",
        "description": "Fetch a single entity by entityType and entityId",
        "inputSchema": {
            "properties": {
                "entityType": {"type": "string"},
                "entityId": {"type": "integer"},
            },
            "required": ["entityType", "entityId"],
        },
    },
    {
        "name": "get_entities",
        "description": "Fetch multiple entities with optional filter, fields, and keywords",
        "inputSchema": {
            "properties": {
                "entityType": {"type": "string"},
                "filter": {"type": "array"},
                "fields": {"type": "array"},
                "keywords": {"type": "string"},
            },
            "required": [],
        },
    },
]


# ── Mock MCP responses ────────────────────────────────────────────────
# These simulate the JSON payloads the real Opentext SDP MCP server returns.

MOCK_DEFECT_1314 = {
    "content": [
        {
            "type": "text",
            "text": '{"id": 1314, "name": "Login button unresponsive on mobile", '
            '"phase": "Opened", "severity": "High", "priority": "Critical", '
            '"owner": "jane.doe@acme.com", "sprint": "Sprint 24"}',
        }
    ]
}

MOCK_STORY_55 = {
    "content": [
        {
            "type": "text",
            "text": '{"id": 55, "name": "As a user I can reset my password", '
            '"phase": "In Progress", "priority": "High", '
            '"owner": "john.smith@acme.com", "story_points": 5}',
        }
    ]
}

MOCK_DEFECTS_LIST = {
    "content": [
        {
            "type": "text",
            "text": '[{"id": 1314, "name": "Login button unresponsive", "phase": "Opened"}, '
            '{"id": 1315, "name": "Crash on checkout", "phase": "Fixed"}]',
        }
    ]
}

MOCK_WORK_ITEMS = {
    "content": [
        {
            "type": "text",
            "text": '[{"type": "defect", "id": 1314, "name": "Login button unresponsive", "phase": "Opened"}, '
            '{"type": "story", "id": 55, "name": "Password reset flow", "phase": "In Progress"}]',
        }
    ]
}


# ── Helpers ───────────────────────────────────────────────────────────

def _make_mock_mcp(responses: dict[str, dict]) -> AsyncMock:
    """Build a mock OctaneMcpClient.

    list_tools() returns MOCK_MCP_TOOL_DEFS so populate_registry_from_mcp()
    can populate TOOL_REGISTRY correctly before the handler runs.
    call_tool() returns the matching fixture from ``responses``.
    """
    mock = AsyncMock()

    async def _call_tool(tool_name, arguments, *, bearer_token=None,
                         shared_space_id=None, workspace_id=None):
        if tool_name in responses:
            return responses[tool_name]
        return {"content": [{"type": "text", "text": f"mock result for {tool_name}"}]}

    mock.call_tool = AsyncMock(side_effect=_call_tool)
    mock.list_tools = AsyncMock(return_value={"tools": MOCK_MCP_TOOL_DEFS})
    return mock


async def _run_agent_handler(user_text: str, mock_mcp, tool_log: list[dict]):
    """Run the keyword-based A2A handler with a mock MCP client.

    Pre-populates TOOL_REGISTRY from the mock's list_tools() response so
    resolve_intent() and extract_arguments() can find the tool definitions.
    TOOL_REGISTRY is restored after the test to avoid cross-test contamination.

    Returns the full A2A task response dict.
    """
    from tool_router import (
        execute_tool as real_execute_tool,
        populate_registry_from_mcp,
        TOOL_REGISTRY,
    )

    # Snapshot current registry so we can restore it after the test
    registry_snapshot = dict(TOOL_REGISTRY)

    # Populate with mock tool schemas so extract_arguments() can look up defaults
    mock_tools_raw = (await mock_mcp.list_tools()).get("tools", [])
    populate_registry_from_mcp(mock_tools_raw)

    async def _logging_execute_tool(tool_name, arguments, mcp, *, bearer_token=None):
        tool_log.append({"name": tool_name, "arguments": dict(arguments)})
        return await real_execute_tool(tool_name, arguments, mcp, bearer_token=bearer_token)

    user_msg = a2a_models.Message(
        role="ROLE_USER",
        parts=[a2a_models.Part(text=user_text)],
    )

    try:
        with patch.object(main, "mcp", mock_mcp), \
             patch.object(main, "execute_tool", _logging_execute_tool):
            result = await main._handle_with_keywords(
                "test-task", "test-ctx", user_text, user_msg, "mock-token"
            )
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(registry_snapshot)

    return result


# ══════════════════════════════════════════════════════════════════════
#  TIER 1: TOOL CORRECTNESS — plain pytest assertions (zero API cost)
# ══════════════════════════════════════════════════════════════════════

class TestToolCorrectness:
    """Verify the keyword router picks the correct generic MCP tool.

    The Octane MCP server uses a generic API:
    - get_entity  — single entity by entityType + entityId
    - get_entities — list with optional filter / entityType

    These tests confirm intent resolution + argument extraction without
    any LLM calls or real network requests.
    """

    @pytest.mark.asyncio
    async def test_get_entity_for_defect(self):
        """'Get defect 1314' → get_entity(entityType='defect', entityId=1314).

        'get' keyword routes to get_entity; entityId and entityType are
        extracted from the free-text phrase.
        """
        mock_mcp = _make_mock_mcp({"get_entity": MOCK_DEFECT_1314})
        tool_log: list[dict] = []

        result = await _run_agent_handler("Get defect 1314", mock_mcp, tool_log)

        assert len(tool_log) == 1
        assert tool_log[0]["name"] == "get_entity"
        assert tool_log[0]["arguments"]["entityId"] == 1314
        assert tool_log[0]["arguments"]["entityType"] == "defect"
        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    @pytest.mark.asyncio
    async def test_get_entity_for_story(self):
        """'Fetch story 55' → get_entity(entityType='story', entityId=55).

        'fetch' keyword routes to get_entity; entityType inferred from 'story'.
        """
        mock_mcp = _make_mock_mcp({"get_entity": MOCK_STORY_55})
        tool_log: list[dict] = []

        result = await _run_agent_handler("Fetch story 55", mock_mcp, tool_log)

        assert len(tool_log) == 1
        assert tool_log[0]["name"] == "get_entity"
        assert tool_log[0]["arguments"]["entityId"] == 55
        assert tool_log[0]["arguments"]["entityType"] == "story"
        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    @pytest.mark.asyncio
    async def test_list_defects_uses_get_entities(self):
        """'List defects' → get_entities(entityType='defect').

        'list' + 'defects' keywords both score for get_entities; entityType
        is extracted from the word 'defects'.
        """
        mock_mcp = _make_mock_mcp({"get_entities": MOCK_DEFECTS_LIST})
        tool_log: list[dict] = []

        result = await _run_agent_handler("List defects", mock_mcp, tool_log)

        assert len(tool_log) == 1
        assert tool_log[0]["name"] == "get_entities"
        assert tool_log[0]["arguments"]["entityType"] == "defect"
        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    @pytest.mark.asyncio
    async def test_my_work_items_uses_get_entities(self):
        """'What are my work items?' → get_entities().

        'my items' keyword routes to get_entities; no specific entityType is
        extracted so the tool returns all types.
        """
        mock_mcp = _make_mock_mcp({"get_entities": MOCK_WORK_ITEMS})
        tool_log: list[dict] = []

        result = await _run_agent_handler("What are my work items?", mock_mcp, tool_log)

        assert len(tool_log) == 1
        assert tool_log[0]["name"] == "get_entities"
        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    @pytest.mark.asyncio
    async def test_entity_lookup_does_not_call_list_tool(self):
        """Negative: 'Get defect 1314' must NOT call get_entities.

        A single-entity lookup by ID should always route to get_entity,
        not the list tool.
        """
        mock_mcp = _make_mock_mcp({"get_entity": MOCK_DEFECT_1314})
        tool_log: list[dict] = []

        await _run_agent_handler("Get defect 1314", mock_mcp, tool_log)

        assert all(t["name"] != "get_entities" for t in tool_log)


# ══════════════════════════════════════════════════════════════════════
#  TIER 2: TOOL CORRECTNESS — DeepEval metric (requires GEMINI_API_KEY)
# ══════════════════════════════════════════════════════════════════════

class TestToolCorrectnessDeepEval:
    """Formally validate tool calls using DeepEval's ToolCorrectnessMetric."""

    @pytest.mark.asyncio
    async def test_defect_entity_tool_correctness(self, gemini_judge):
        """DeepEval check: get_entity called with entityType='defect', entityId=1314."""
        mock_mcp = _make_mock_mcp({"get_entity": MOCK_DEFECT_1314})
        tool_log: list[dict] = []

        _ = await _run_agent_handler("Get defect 1314", mock_mcp, tool_log)

        assert len(tool_log) == 1, "Expected exactly one tool call"

        test_case = LLMTestCase(
            input="Get defect 1314",
            actual_output="Defect 1314 retrieved",
            tools_called=[
                ToolCall(
                    name=tool_log[0]["name"],
                    input_parameters=tool_log[0]["arguments"],
                )
            ],
            expected_tools=[
                ToolCall(
                    name="get_entity",
                    input_parameters={"entityType": "defect", "entityId": 1314},
                )
            ],
        )
        metric = ToolCorrectnessMetric(
            model=gemini_judge,
            threshold=1.0,
            should_exact_match=True,
        )
        assert_test(test_case, [metric])

    @pytest.mark.asyncio
    async def test_work_items_tool_correctness(self, gemini_judge):
        """DeepEval check: get_entities called for 'What are my work items?'."""
        mock_mcp = _make_mock_mcp({"get_entities": MOCK_WORK_ITEMS})
        tool_log: list[dict] = []

        _ = await _run_agent_handler("What are my work items?", mock_mcp, tool_log)

        assert len(tool_log) == 1, "Expected exactly one tool call"

        test_case = LLMTestCase(
            input="What are my work items?",
            actual_output="Work items retrieved",
            tools_called=[ToolCall(name=tool_log[0]["name"])],
            expected_tools=[ToolCall(name="get_entities")],
        )
        metric = ToolCorrectnessMetric(model=gemini_judge, threshold=1.0)
        assert_test(test_case, [metric])


# ══════════════════════════════════════════════════════════════════════
#  TIER 3: ANSWER RELEVANCY (Gemini judge — one API call each)
# ══════════════════════════════════════════════════════════════════════

class TestAnswerRelevancy:
    """Validate the quality of the agent's final response using Gemini as a judge."""

    @pytest.mark.asyncio
    async def test_defect_response_is_relevant(self, gemini_judge):
        """The response to 'Get defect 1314' should be relevant to defect retrieval."""
        mock_mcp = _make_mock_mcp({"get_entity": MOCK_DEFECT_1314})
        tool_log: list[dict] = []

        result = await _run_agent_handler("Get defect 1314", mock_mcp, tool_log)
        actual_output = result["task"]["status"]["message"]["parts"][0]["text"]

        test_case = LLMTestCase(input="Get defect 1314", actual_output=actual_output)
        metric = AnswerRelevancyMetric(model=gemini_judge, threshold=0.5)
        assert_test(test_case, [metric])

    @pytest.mark.asyncio
    async def test_work_items_response_is_relevant(self, gemini_judge):
        """The response to 'What are my work items?' should be relevant to work items."""
        mock_mcp = _make_mock_mcp({"get_entities": MOCK_WORK_ITEMS})
        tool_log: list[dict] = []

        result = await _run_agent_handler("What are my work items?", mock_mcp, tool_log)
        actual_output = result["task"]["status"]["message"]["parts"][0]["text"]

        test_case = LLMTestCase(
            input="What are my work items?",
            actual_output=actual_output,
        )
        metric = AnswerRelevancyMetric(model=gemini_judge, threshold=0.5)
        assert_test(test_case, [metric])

    @pytest.mark.asyncio
    async def test_list_defects_response_is_relevant(self, gemini_judge):
        """The response to 'List defects' should be relevant to a defect listing."""
        mock_mcp = _make_mock_mcp({"get_entities": MOCK_DEFECTS_LIST})
        tool_log: list[dict] = []

        result = await _run_agent_handler("List defects", mock_mcp, tool_log)
        actual_output = result["task"]["status"]["message"]["parts"][0]["text"]

        test_case = LLMTestCase(input="List defects", actual_output=actual_output)
        metric = AnswerRelevancyMetric(model=gemini_judge, threshold=0.5)
        assert_test(test_case, [metric])
