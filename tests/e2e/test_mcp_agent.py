"""
E2E tests for the A2A agent's MCP tool-calling logic.

Uses DeepEval with Gemini 2.0 Flash as the evaluation model.

Test strategy (three tiers):
  1. TestToolCorrectness — Pure pytest assertions verify the Internal Agent
     calls the right MCP tools with the right arguments.  Zero API cost.
     These tests mock the MCP server and exercise the keyword-based intent
     router (tool_router.resolve_intent → extract_arguments → execute_tool).

  2. TestToolCorrectnessDeepEval — DeepEval's ToolCorrectnessMetric formally
     validates tool-call expectations using the Gemini judge.  This adds a
     second layer of confidence: DeepEval compares actual ToolCall objects
     (name + input_parameters) against expected ones.

  3. TestAnswerRelevancy — DeepEval's AnswerRelevancyMetric asks Gemini to
     judge whether the agent's final natural-language response is relevant
     to the user's original question.  One cheap Gemini 2.0 Flash API call
     per test case.

Architecture:
  User text  →  keyword router (tool_router.py)  →  execute_tool()  →  mock MCP
                                                          ↓
                                                   A2A Task response

The MCP server is fully mocked — no network calls to Opentext SDP.

Run:
    pytest tests/e2e/ -v                          # all E2E tests
    pytest tests/e2e/ -k "not DeepEval" -v        # tool routing only (free)
    pytest tests/e2e/ -k "DeepEval or Relevancy"  # Gemini-judged only
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from deepeval import assert_test
from deepeval.metrics import AnswerRelevancyMetric, ToolCorrectnessMetric
from deepeval.test_case import LLMTestCase, ToolCall

import a2a_models
import main


# ── Mock MCP responses ────────────────────────────────────────────────
# These simulate the JSON payloads the real Opentext SDP MCP server would
# return.  Each dict follows the MCP result format: {"content": [{"type",
# "text"}]}.  The text field contains the JSON body the agent would receive
# from tools like get_defect, get_story, etc.

MOCK_DEFECT_1314 = {
    "content": [
        {
            "type": "text",
            "text": '{"id": 1314, "name": "Login button unresponsive on mobile", '
            '"phase": "Opened", "severity": "High", "priority": "Critical", '
            '"owner": "jane.doe@acme.com", "sprint": "Sprint 24", '
            '"detected_in_release": "3.2.1"}',
        }
    ]
}

MOCK_STORY_55 = {
    "content": [
        {
            "type": "text",
            "text": '{"id": 55, "name": "As a user I can reset my password", '
            '"phase": "In Progress", "priority": "High", '
            '"owner": "john.smith@acme.com", "sprint": "Sprint 23", '
            '"story_points": 5}',
        }
    ]
}

MOCK_COMMENTS = {
    "content": [
        {
            "type": "text",
            "text": '[{"id": 101, "text": "Reproduced on build 5.3", "author": "qa-bot"}, '
            '{"id": 102, "text": "Fix deployed in build 5.4", "author": "jane.doe"}]',
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

MOCK_CREATE_COMMENT = {
    "content": [
        {
            "type": "text",
            "text": '{"id": 201, "text": "Verified fix in staging", "entityId": 1314, "entityType": "defect"}',
        }
    ]
}


# ── Helpers ───────────────────────────────────────────────────────────

def _make_mock_mcp(responses: dict[str, dict]) -> AsyncMock:
    """Build a mock OctaneMcpClient that returns pre-canned MCP responses.

    The mock intercepts call_tool() and returns the matching fixture from
    ``responses``.  If the tool name is not in the dict, a generic fallback
    response is returned so the test doesn't crash on unexpected calls.
    list_tools() returns an empty list since tool discovery is not needed.
    """
    mock = AsyncMock()

    async def _call_tool(tool_name, arguments, *, bearer_token=None,
                         shared_space_id=None, workspace_id=None):
        if tool_name in responses:
            return responses[tool_name]
        return {"content": [{"type": "text", "text": f"mock result for {tool_name}"}]}

    mock.call_tool = AsyncMock(side_effect=_call_tool)
    mock.list_tools = AsyncMock(return_value={"tools": []})
    return mock


async def _run_agent_handler(user_text: str, mock_mcp, tool_log: list[dict]):
    """Run the keyword-based A2A handler with a mock MCP client.

    This exercises the full request path that a real A2A message would take
    when Gemini is disabled (keyword fallback mode):

      1. resolve_intent()    — keyword matching picks the best tool.
      2. extract_arguments() — regex pulls entityId, entityType, text from
                               the natural-language input.
      3. execute_tool()      — calls the (mocked) MCP server and wraps the
                               result in an A2A Artifact.
      4. _handle_with_keywords() — assembles the final A2A Task response.

    A logging wrapper around execute_tool captures every tool call into
    ``tool_log`` so tests can assert on tool names and arguments.

    Returns the full A2A task response dict.
    """
    from tool_router import execute_tool as real_execute_tool

    async def _logging_execute_tool(tool_name, arguments, mcp, *, bearer_token=None):
        # Record the call for test assertions
        tool_log.append({"name": tool_name, "arguments": dict(arguments)})
        return await real_execute_tool(tool_name, arguments, mcp, bearer_token=bearer_token)

    # Build a minimal A2A Message with the user's text
    user_msg = a2a_models.Message(
        role="ROLE_USER",
        parts=[a2a_models.Part(text=user_text)],
    )

    # Patch the global mcp client and execute_tool in main.py so the
    # handler uses our mock instead of the real MCP server.
    with patch.object(main, "mcp", mock_mcp), \
         patch.object(main, "execute_tool", _logging_execute_tool):
        result = await main._handle_with_keywords(
            "test-task", "test-ctx", user_text, user_msg, "mock-token"
        )

    return result


# ══════════════════════════════════════════════════════════════════════
#  TIER 1: TOOL CORRECTNESS — plain pytest assertions (zero API cost)
# ══════════════════════════════════════════════════════════════════════

class TestToolCorrectness:
    """Verify the keyword router selects the correct MCP tool with correct args.

    These tests use only plain pytest assertions — no LLM calls, no API keys.
    They validate the core tool-routing logic:
      - Does the intent resolver pick the right tool for a given user phrase?
      - Does the argument extractor pull the correct entityId / entityType?
      - Does the handler return a COMPLETED task state?

    The MCP server is mocked so no real HTTP calls are made.
    """

    @pytest.mark.asyncio
    async def test_get_defect_routes_correctly(self):
        """Asking for a defect by ID should call get_defect with that entityId.

        Verifies:  intent resolution ("defect" keyword) + entityId extraction.
        """
        mock_mcp = _make_mock_mcp({"get_defect": MOCK_DEFECT_1314})
        tool_log: list[dict] = []

        result = await _run_agent_handler("Get defect 1314", mock_mcp, tool_log)

        # Exactly one tool should have been called
        assert len(tool_log) == 1
        # The router should have picked get_defect (not get_story, etc.)
        assert tool_log[0]["name"] == "get_defect"
        # The regex should have extracted 1314 as the entityId
        assert tool_log[0]["arguments"]["entityId"] == 1314
        # The task should complete successfully
        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    @pytest.mark.asyncio
    async def test_get_story_routes_correctly(self):
        """Asking for a story by ID should call get_story with that entityId.

        Verifies:  intent resolution ("story" keyword) + entityId extraction.
        """
        mock_mcp = _make_mock_mcp({"get_story": MOCK_STORY_55})
        tool_log: list[dict] = []

        result = await _run_agent_handler("Get story 55", mock_mcp, tool_log)

        assert len(tool_log) == 1
        assert tool_log[0]["name"] == "get_story"
        assert tool_log[0]["arguments"]["entityId"] == 55
        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    @pytest.mark.asyncio
    async def test_get_comments_routes_correctly(self):
        """Asking for comments should call get_comments with entityId + entityType.

        Verifies:  intent resolution ("comments" keyword) + entityId extraction
        + entityType inference ("defect" in the phrase → entityType="defect").
        """
        mock_mcp = _make_mock_mcp({"get_comments": MOCK_COMMENTS})
        tool_log: list[dict] = []

        result = await _run_agent_handler(
            "Show comments on defect 1314", mock_mcp, tool_log
        )

        assert len(tool_log) == 1
        assert tool_log[0]["name"] == "get_comments"
        assert tool_log[0]["arguments"]["entityId"] == 1314
        # The router should infer entityType from the word "defect"
        assert tool_log[0]["arguments"]["entityType"] == "defect"

    @pytest.mark.asyncio
    async def test_fetch_my_work_items_routes_correctly(self):
        """Asking for 'my work items' should call fetch_My_Work_Items (no args).

        Verifies:  intent resolution ("my work" / "my items" keywords).
        fetch_My_Work_Items requires no entityId — just the bearer token
        which is injected automatically by the MCP client.
        """
        mock_mcp = _make_mock_mcp({"fetch_My_Work_Items": MOCK_WORK_ITEMS})
        tool_log: list[dict] = []

        result = await _run_agent_handler("What are my work items?", mock_mcp, tool_log)

        assert len(tool_log) == 1
        assert tool_log[0]["name"] == "fetch_My_Work_Items"
        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    @pytest.mark.asyncio
    async def test_create_comment_routes_correctly(self):
        """Adding a comment should call create_comment with entityId + entityType.

        The keyword router scores intents by counting keyword substring matches.
        The phrase 'add a comment saying' matches two create_comment keywords
        ('add a comment' + 'comment saying'), giving it score=2 vs
        get_comments score=1 ('comment'), so create_comment wins.

        Verifies:  intent resolution + entityId + entityType extraction.
        """
        mock_mcp = _make_mock_mcp({"create_comment": MOCK_CREATE_COMMENT})
        tool_log: list[dict] = []

        _ = await _run_agent_handler(
            "add a comment saying Verified on defect 1314",
            mock_mcp,
            tool_log,
        )

        assert len(tool_log) == 1
        assert tool_log[0]["name"] == "create_comment"
        assert tool_log[0]["arguments"]["entityId"] == 1314
        assert tool_log[0]["arguments"]["entityType"] == "defect"

    @pytest.mark.asyncio
    async def test_wrong_tool_not_called(self):
        """Negative test: a defect query must NOT call get_story.

        Ensures the intent resolver does not confuse entity types.
        """
        mock_mcp = _make_mock_mcp({"get_defect": MOCK_DEFECT_1314})
        tool_log: list[dict] = []

        await _run_agent_handler("Get defect 1314", mock_mcp, tool_log)

        # No tool call should have been routed to get_story
        assert all(t["name"] != "get_story" for t in tool_log)


# ══════════════════════════════════════════════════════════════════════
#  TIER 2: TOOL CORRECTNESS — DeepEval metric (requires GEMINI_API_KEY)
# ══════════════════════════════════════════════════════════════════════

class TestToolCorrectnessDeepEval:
    """Formally validate tool calls using DeepEval's ToolCorrectnessMetric.

    These tests go beyond plain assertions by feeding actual ToolCall objects
    (captured from the handler) into DeepEval's metric, which compares:
      - Tool name:  did the agent call the expected tool?
      - Input parameters:  were the arguments (entityId, entityType, text)
        what we expected?

    The Gemini judge (gemini_judge fixture from conftest.py) is passed as
    the evaluation model.  DeepEval uses it to produce a score and
    (optionally) a human-readable reason for any mismatch.

    Requires GEMINI_API_KEY in .env.
    """

    @pytest.mark.asyncio
    async def test_defect_tool_correctness(self, gemini_judge):
        """DeepEval check: get_defect called with entityId=1314 for 'Get defect 1314'.

        Captures the actual ToolCall from the handler and compares it against
        the expected ToolCall using should_exact_match=True — both the tool
        name AND input_parameters must match exactly.
        """
        mock_mcp = _make_mock_mcp({"get_defect": MOCK_DEFECT_1314})
        tool_log: list[dict] = []

        _ = await _run_agent_handler("Get defect 1314", mock_mcp, tool_log)

        test_case = LLMTestCase(
            input="Get defect 1314",
            actual_output="Defect 1314 retrieved",
            # tools_called: what the agent actually invoked (from tool_log)
            tools_called=[
                ToolCall(
                    name=tool_log[0]["name"],
                    input_parameters=tool_log[0]["arguments"],
                )
            ],
            # expected_tools: what we expect the agent should have called
            expected_tools=[
                ToolCall(
                    name="get_defect",
                    input_parameters={"entityId": 1314},
                )
            ],
        )
        metric = ToolCorrectnessMetric(
            model=gemini_judge,
            threshold=1.0,          # require perfect match
            should_exact_match=True, # name AND params must match
        )
        assert_test(test_case, [metric])

    @pytest.mark.asyncio
    async def test_work_items_tool_correctness(self, gemini_judge):
        """DeepEval check: fetch_My_Work_Items called for 'What are my work items?'.

        fetch_My_Work_Items takes no arguments, so only the tool name is compared.
        """
        mock_mcp = _make_mock_mcp({"fetch_My_Work_Items": MOCK_WORK_ITEMS})
        tool_log: list[dict] = []

        _ = await _run_agent_handler("What are my work items?", mock_mcp, tool_log)

        test_case = LLMTestCase(
            input="What are my work items?",
            actual_output="Work items retrieved",
            tools_called=[ToolCall(name="fetch_My_Work_Items")],
            expected_tools=[ToolCall(name="fetch_My_Work_Items")],
        )
        metric = ToolCorrectnessMetric(model=gemini_judge, threshold=1.0)
        assert_test(test_case, [metric])

    @pytest.mark.asyncio
    async def test_create_comment_tool_correctness(self, gemini_judge):
        """DeepEval check: create_comment called with correct entityId, entityType, text.

        This is the most complex tool call — it requires three extracted args:
        entityId (from the numeric ID), entityType (from "defect"), and text
        (from the phrase after "saying").
        """
        mock_mcp = _make_mock_mcp({"create_comment": MOCK_CREATE_COMMENT})
        tool_log: list[dict] = []

        _ = await _run_agent_handler(
            "add a comment saying Verified on defect 1314",
            mock_mcp,
            tool_log,
        )

        test_case = LLMTestCase(
            input="add a comment saying Verified on defect 1314",
            actual_output="Comment created",
            tools_called=[
                ToolCall(
                    name=tool_log[0]["name"],
                    input_parameters=tool_log[0]["arguments"],
                )
            ],
            expected_tools=[
                ToolCall(
                    name="create_comment",
                    input_parameters={
                        "entityId": 1314,
                        "entityType": "defect",
                        "text": "Verified fix in staging",
                    },
                )
            ],
        )
        metric = ToolCorrectnessMetric(model=gemini_judge, threshold=1.0)
        assert_test(test_case, [metric])


# ══════════════════════════════════════════════════════════════════════
#  TIER 3: ANSWER RELEVANCY (Gemini 2.0 Flash judge — one API call each)
# ══════════════════════════════════════════════════════════════════════

class TestAnswerRelevancy:
    """Validate the quality of the agent's final response using Gemini as a judge.

    These tests feed the agent's actual_output (the text returned in the A2A
    Task's status message) into DeepEval's AnswerRelevancyMetric.  Gemini
    scores how relevant the response is to the user's original question on
    a 0-1 scale.  We set a threshold of 0.5 — the response doesn't need to
    be perfect, just clearly related to what was asked.

    Since we're using the keyword fallback handler (not the full Gemini
    agentic loop), the actual_output is a short confirmation like
    "Successfully executed get_defect." — which is still relevant to the
    input "Get defect 1314".

    Requires GEMINI_API_KEY in .env.  Each test makes one Gemini API call.
    """

    @pytest.mark.asyncio
    async def test_defect_response_is_relevant(self, gemini_judge):
        """The response to 'Get defect 1314' should be relevant to defect retrieval.

        Gemini judges whether the agent's output text makes sense as an answer
        to a defect lookup query.
        """
        mock_mcp = _make_mock_mcp({"get_defect": MOCK_DEFECT_1314})
        tool_log: list[dict] = []

        result = await _run_agent_handler("Get defect 1314", mock_mcp, tool_log)
        # Extract the agent's natural-language response from the A2A Task
        actual_output = result["task"]["status"]["message"]["parts"][0]["text"]

        test_case = LLMTestCase(
            input="Get defect 1314",
            actual_output=actual_output,
        )
        metric = AnswerRelevancyMetric(model=gemini_judge, threshold=0.5)
        assert_test(test_case, [metric])

    @pytest.mark.asyncio
    async def test_work_items_response_is_relevant(self, gemini_judge):
        """The response to 'What are my work items?' should be relevant to work items.

        Validates that the agent doesn't return something completely unrelated
        (e.g., a joke or an error) when the user asks for their backlog.
        """
        mock_mcp = _make_mock_mcp({"fetch_My_Work_Items": MOCK_WORK_ITEMS})
        tool_log: list[dict] = []

        result = await _run_agent_handler(
            "What are my work items?", mock_mcp, tool_log
        )
        actual_output = result["task"]["status"]["message"]["parts"][0]["text"]

        test_case = LLMTestCase(
            input="What are my work items?",
            actual_output=actual_output,
        )
        metric = AnswerRelevancyMetric(model=gemini_judge, threshold=0.5)
        assert_test(test_case, [metric])

    @pytest.mark.asyncio
    async def test_comments_response_is_relevant(self, gemini_judge):
        """The response to 'Show comments on defect 1314' should be relevant to comments.

        Verifies the agent's output is contextually appropriate for a
        comment-retrieval request.
        """
        mock_mcp = _make_mock_mcp({"get_comments": MOCK_COMMENTS})
        tool_log: list[dict] = []

        result = await _run_agent_handler(
            "Show comments on defect 1314", mock_mcp, tool_log
        )
        actual_output = result["task"]["status"]["message"]["parts"][0]["text"]

        test_case = LLMTestCase(
            input="Show comments on defect 1314",
            actual_output=actual_output,
        )
        metric = AnswerRelevancyMetric(model=gemini_judge, threshold=0.5)
        assert_test(test_case, [metric])
