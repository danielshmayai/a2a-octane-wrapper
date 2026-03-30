"""
Preflight test suite — validates the full A2A ↔ MCP ↔ Gemini configuration
using real parameters from .env. Every assertion carries a plain-language
diagnosis and remediation hint so failures are immediately actionable.

Run:
    pytest tests/e2e/test_preflight.py -v -s

Tests execute in declaration order and cover:
  1. Config  — required env vars present and sane
  2. MCP     — connectivity, tool list, schema structure
  3. Registry — populate_registry_from_mcp in-place update (critical bug check)
  4. Tools   — dynamic ADK function generation and signatures
  5. Agent   — GeminiAgent initialises and wires up all MCP tools
  6. E2E     — full round-trip: user message → Gemini → MCP call → response
"""

from __future__ import annotations

import inspect
import sys
import pathlib

import pytest
import pytest_asyncio

# Ensure repo root is importable (e2e conftest already does this, belt-and-suspenders)
_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

# e2e/conftest.py already calls load_dotenv(_ROOT / ".env"), so config is ready.
import config
from mcp_client import OctaneMcpClient
from tool_router import (
    TOOL_REGISTRY,
    _LOCAL_ONLY_TOOLS,
    _EXCLUDED_MCP_PARAMS,
    populate_registry_from_mcp,
)

# Apply asyncio marker to every async test in this module
pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fail(msg: str) -> None:
    """Raise a clean AssertionError with a formatted message."""
    pytest.fail("\n" + msg.strip())


# ── 1. Config ─────────────────────────────────────────────────────────────────

class TestConfig:
    """Verify all required .env values are present and plausible."""

    def test_octane_base_url(self):
        v = config.OCTANE_BASE_URL
        assert v and v != "http://localhost:8080", (
            "OCTANE_BASE_URL is not set or still the default placeholder.\n"
            "Fix: add OCTANE_BASE_URL=https://<your-octane-host> to .env"
        )
        assert v.startswith("http"), (
            f"OCTANE_BASE_URL='{v}' is not a valid URL.\n"
            "Fix: must start with http:// or https://"
        )

    def test_mcp_endpoint_derived(self):
        ep = config.OCTANE_MCP_ENDPOINT
        assert ep.endswith("/mcp"), (
            f"OCTANE_MCP_ENDPOINT='{ep}' does not end with /mcp.\n"
            "This is derived from OCTANE_BASE_URL + '/mcp'. "
            "Ensure OCTANE_BASE_URL is set correctly."
        )

    def test_api_key(self):
        assert config.API_KEY, (
            "API_KEY is empty.\n"
            "Fix: add API_KEY=<your-octane-api-token> to .env"
        )
        assert len(config.API_KEY) > 16, (
            f"API_KEY looks suspiciously short (len={len(config.API_KEY)}).\n"
            "Ensure .env contains a full, valid Octane API key."
        )

    def test_workspace_ids(self):
        assert config.DEFAULT_SHARED_SPACE_ID > 0, (
            "DEFAULT_SHARED_SPACE_ID is 0 or not set.\n"
            "Fix: add DEFAULT_SHARED_SPACE_ID=1001 (or your value) to .env"
        )
        assert config.DEFAULT_WORKSPACE_ID > 0, (
            "DEFAULT_WORKSPACE_ID is 0 or not set.\n"
            "Fix: add DEFAULT_WORKSPACE_ID=<your-workspace-id> to .env"
        )

    def test_gemini_api_key(self):
        assert config.GEMINI_API_KEY, (
            "GEMINI_API_KEY is empty — the Gemini agent will not initialise.\n"
            "Fix: add GEMINI_API_KEY=<your-google-ai-key> to .env"
        )

    def test_gemini_model(self):
        assert config.GEMINI_MODEL, (
            "GEMINI_MODEL is empty.\n"
            "Fix: add GEMINI_MODEL=gemini-2.0-flash (or another model) to .env, "
            "or remove the override to use the default."
        )


# ── 2. MCP connectivity ───────────────────────────────────────────────────────

class TestMcpConnectivity:

    async def test_list_tools_reachable(self):
        """MCP server must respond to list_tools within the configured timeout."""
        mcp = OctaneMcpClient()
        try:
            result = await mcp.list_tools()
        except Exception as exc:
            _fail(
                f"mcp.list_tools() raised {type(exc).__name__}: {exc}\n\n"
                f"  Endpoint : {config.OCTANE_MCP_ENDPOINT}\n"
                f"  Shared   : {config.DEFAULT_SHARED_SPACE_ID}\n"
                f"  Workspace: {config.DEFAULT_WORKSPACE_ID}\n\n"
                "Possible causes:\n"
                "  • Octane / MCP server is not running\n"
                "  • OCTANE_BASE_URL is wrong or unreachable\n"
                "  • API_KEY is invalid, expired, or lacks MCP scope\n"
                "  • Network / firewall / VPN issue\n"
                "  • MCP_REQUEST_TIMEOUT_SECONDS too short "
                f"(currently {config.MCP_REQUEST_TIMEOUT_SECONDS}s)"
            )

        tools = result.get("tools", [])
        assert tools, (
            "mcp.list_tools() returned an empty tool list.\n"
            "The server is reachable but exposed no tools — check Octane MCP configuration."
        )

    async def test_expected_tools_present(self):
        """The generic Octane MCP API must expose the discovery + query tools."""
        mcp = OctaneMcpClient()
        result = await mcp.list_tools()
        names = {t["name"] for t in result.get("tools", [])}

        REQUIRED = {
            "get_entity_types",
            "get_entities",
            "get_entity",
            "get_entity_field_metadata",
            "get_filter_metadata",
        }
        missing = REQUIRED - names
        assert not missing, (
            f"Required MCP tools are missing: {sorted(missing)}\n"
            f"Tools the server returned  : {sorted(names)}\n\n"
            "The MCP server API may have changed. Either:\n"
            "  • Update REQUIRED in this test to match the new tool names, or\n"
            "  • Investigate why these tools are absent from the Octane MCP server."
        )

    async def test_tool_schemas_have_required_fields(self):
        """Every tool must have name, description, and a valid inputSchema."""
        mcp = OctaneMcpClient()
        result = await mcp.list_tools()
        errors: list[str] = []

        for t in result.get("tools", []):
            name = t.get("name", "<unnamed>")
            if "description" not in t:
                errors.append(f"  '{name}' — missing 'description'")
            if "inputSchema" not in t:
                errors.append(f"  '{name}' — missing 'inputSchema'")
            elif "properties" not in t["inputSchema"]:
                errors.append(
                    f"  '{name}' — inputSchema missing 'properties' "
                    f"(got keys: {list(t['inputSchema'].keys())})"
                )

        assert not errors, (
            "Some MCP tools have incomplete schemas:\n"
            + "\n".join(errors)
            + "\n\nValid schemas are required for dynamic ADK function generation."
        )

    async def test_excluded_params_present_in_schemas(self):
        """sharedSpaceId / workSpaceId must appear in tool schemas (auto-injected later)."""
        mcp = OctaneMcpClient()
        result = await mcp.list_tools()
        warnings: list[str] = []

        for t in result.get("tools", []):
            name = t.get("name", "<unnamed>")
            props = t.get("inputSchema", {}).get("properties", {})
            missing_ctx = _EXCLUDED_MCP_PARAMS - props.keys()
            if missing_ctx:
                warnings.append(f"  '{name}' — missing auto-injected params: {missing_ctx}")

        # Non-fatal — tools without these params still work (they may be optional)
        if warnings:
            print(
                "\n[WARN] Some tools are missing the auto-injected context params "
                "(may be intentional):\n" + "\n".join(warnings)
            )


# ── 3. Tool registry ──────────────────────────────────────────────────────────

class TestToolRegistry:

    async def test_populate_updates_in_place(self):
        """
        CRITICAL: populate_registry_from_mcp must mutate the existing dict,
        not rebind the name. Other modules import the dict by reference —
        if the name is rebound they see the old empty dict forever.
        """
        ref_before = id(TOOL_REGISTRY)

        mcp = OctaneMcpClient()
        result = await mcp.list_tools()
        populate_registry_from_mcp(result.get("tools", []))

        assert id(TOOL_REGISTRY) == ref_before, (
            "populate_registry_from_mcp() replaced the TOOL_REGISTRY dict object!\n\n"
            "  id before : {ref_before}\n"
            "  id after  : {id(TOOL_REGISTRY)}\n\n"
            "This means any module that did `from tool_router import TOOL_REGISTRY`\n"
            "still holds a reference to the OLD empty dict.\n\n"
            "Fix in tool_router.py:\n"
            "  TOOL_REGISTRY.clear()\n"
            "  TOOL_REGISTRY.update(new_entries)   # ← mutate, don't rebind"
        )

    async def test_mcp_tools_visible_after_populate(self):
        """After populate, TOOL_REGISTRY must contain the MCP tools."""
        mcp = OctaneMcpClient()
        result = await mcp.list_tools()
        populate_registry_from_mcp(result.get("tools", []))

        mcp_names = [k for k in TOOL_REGISTRY if k not in _LOCAL_ONLY_TOOLS]
        assert mcp_names, (
            "TOOL_REGISTRY has no MCP tools after populate_registry_from_mcp.\n"
            "Only local tools remain. Inspect the populate logic in tool_router.py."
        )

        expected_mcp = {"get_entity_types", "get_entities", "get_entity"}
        missing = expected_mcp - set(mcp_names)
        assert not missing, (
            f"Expected tools missing from TOOL_REGISTRY after populate: {missing}\n"
            f"Present tools: {sorted(mcp_names)}"
        )

    async def test_local_tools_preserved_after_populate(self):
        """Local-only tools (tell_joke) must survive registry replacement."""
        mcp = OctaneMcpClient()
        result = await mcp.list_tools()
        populate_registry_from_mcp(result.get("tools", []))

        for name in _LOCAL_ONLY_TOOLS:
            assert name in TOOL_REGISTRY, (
                f"Local-only tool '{name}' was dropped by populate_registry_from_mcp.\n"
                "Fix: preserve _LOCAL_ONLY_TOOLS entries during populate."
            )
            assert TOOL_REGISTRY[name].get("_local_only"), (
                f"Tool '{name}' lost its _local_only flag after populate."
            )

    async def test_entries_contain_input_schema(self):
        """Each MCP entry must store inputSchema for dynamic ADK function generation."""
        mcp = OctaneMcpClient()
        result = await mcp.list_tools()
        populate_registry_from_mcp(result.get("tools", []))

        errors: list[str] = []
        for name, defn in TOOL_REGISTRY.items():
            if defn.get("_local_only"):
                continue
            if "inputSchema" not in defn:
                errors.append(f"  '{name}' — missing 'inputSchema' in registry entry")

        assert not errors, (
            "Some TOOL_REGISTRY entries are missing 'inputSchema':\n"
            + "\n".join(errors)
            + "\n\nFix: populate_registry_from_mcp must store inputSchema per tool."
        )

    async def test_excluded_params_stripped_from_registry(self):
        """sharedSpaceId / workSpaceId must NOT be in default_arguments or inputSchema."""
        mcp = OctaneMcpClient()
        result = await mcp.list_tools()
        populate_registry_from_mcp(result.get("tools", []))

        errors: list[str] = []
        for name, defn in TOOL_REGISTRY.items():
            if defn.get("_local_only"):
                continue
            leaked = _EXCLUDED_MCP_PARAMS & defn.get("default_arguments", {}).keys()
            if leaked:
                errors.append(f"  '{name}' — excluded params in default_arguments: {leaked}")
            leaked_schema = _EXCLUDED_MCP_PARAMS & defn.get("inputSchema", {}).keys()
            if leaked_schema:
                errors.append(f"  '{name}' — excluded params in inputSchema: {leaked_schema}")

        assert not errors, (
            "Auto-injected params leaked into TOOL_REGISTRY entries:\n"
            + "\n".join(errors)
            + "\n\nThese params are injected by mcp_client.call_tool() and must never "
            "be exposed to the Gemini agent."
        )


# ── 4. Dynamic tool generation ───────────────────────────────────────────────

class TestDynamicTools:

    async def test_build_tools_includes_all_registry_tools(self):
        """_build_tools() must return a function for every tool in TOOL_REGISTRY."""
        from gemini_agent import _build_tools

        mcp = OctaneMcpClient()
        populate_registry_from_mcp((await mcp.list_tools()).get("tools", []))

        tools = _build_tools(mcp, [], bearer_token=None, mcp_called_flag=[False])
        built_names = {fn.__name__ for fn in tools}

        missing: list[str] = []
        for name in TOOL_REGISTRY:
            if name not in built_names:
                missing.append(name)

        assert not missing, (
            f"These TOOL_REGISTRY tools were NOT included in _build_tools() output:\n"
            + "\n".join(f"  • {n}" for n in sorted(missing))
            + f"\n\nAll built tools: {sorted(built_names)}\n"
            "Fix: check the dynamic tool generation loop in _build_tools."
        )

    async def test_dynamic_functions_have_valid_signatures(self):
        """ADK calls inspect.signature() to build Gemini schemas — must not fail."""
        from gemini_agent import _build_tools

        mcp = OctaneMcpClient()
        populate_registry_from_mcp((await mcp.list_tools()).get("tools", []))

        tools = _build_tools(mcp, [], bearer_token=None, mcp_called_flag=[False])
        errors: list[str] = []

        for fn in tools:
            try:
                sig = inspect.signature(fn)
            except Exception as exc:
                errors.append(
                    f"  '{fn.__name__}' — inspect.signature() failed: {exc}"
                )
                continue

            for pname, param in sig.parameters.items():
                if param.annotation is inspect.Parameter.empty:
                    errors.append(
                        f"  '{fn.__name__}.{pname}' — missing type annotation "
                        "(ADK needs this to generate a Gemini FunctionDeclaration)"
                    )

        assert not errors, (
            "Dynamic tool function signature issues:\n"
            + "\n".join(errors)
            + "\n\nFix: _make_dynamic_tool_fn must set __signature__ and __annotations__."
        )

    async def test_dynamic_functions_have_docstrings(self):
        """Gemini uses docstrings as tool descriptions — they must not be empty."""
        from gemini_agent import _build_tools

        mcp = OctaneMcpClient()
        populate_registry_from_mcp((await mcp.list_tools()).get("tools", []))

        tools = _build_tools(mcp, [], bearer_token=None, mcp_called_flag=[False])
        missing_docs = [fn.__name__ for fn in tools if not (fn.__doc__ or "").strip()]

        assert not missing_docs, (
            "These tools have no docstring — Gemini won't know what they do:\n"
            + "\n".join(f"  • {n}" for n in missing_docs)
            + "\n\nFix: populate_registry_from_mcp must store non-empty descriptions."
        )


# ── 5. Gemini agent ──────────────────────────────────────────────────────────

class TestGeminiAgent:

    async def test_agent_initialises(self):
        """GeminiAgent() must construct without raising."""
        from gemini_agent import GeminiAgent

        try:
            agent = GeminiAgent()
        except Exception as exc:
            _fail(
                f"GeminiAgent() raised {type(exc).__name__}: {exc}\n\n"
                "Possible causes:\n"
                "  • GEMINI_API_KEY not set or invalid\n"
                "  • google-adk not installed (pip install google-adk)\n"
                "  • InMemorySessionService unavailable in this ADK version"
            )

    async def test_refresh_tools_wires_mcp_tools(self):
        """After refresh_tools, the agent runner must know about MCP tools."""
        from gemini_agent import GeminiAgent

        mcp = OctaneMcpClient()
        populate_registry_from_mcp((await mcp.list_tools()).get("tools", []))

        agent = GeminiAgent()
        try:
            tool_names = await agent.refresh_tools(mcp)
        except Exception as exc:
            _fail(
                f"agent.refresh_tools() raised {type(exc).__name__}: {exc}\n"
                "Check MCP connectivity and GEMINI_API_KEY."
            )

        mcp_tools = [n for n in tool_names if n not in _LOCAL_ONLY_TOOLS]
        assert mcp_tools, (
            f"refresh_tools() returned only local tools: {tool_names}\n\n"
            "MCP tools are not reaching the Gemini agent runner.\n"
            "Likely cause: TOOL_REGISTRY was empty when refresh_tools was called "
            "(in-place update bug) or _build_tools is not reading TOOL_REGISTRY."
        )

        assert "get_entity_types" in tool_names, (
            f"'get_entity_types' not in agent tool list: {tool_names}\n"
            "This discovery tool must be present for the agent to function correctly."
        )

    async def test_system_prompt_contains_tool_catalogue(self):
        """_build_system_prompt() must list MCP tools, not the 'no tools' fallback."""
        from gemini_agent import _build_system_prompt

        mcp = OctaneMcpClient()
        populate_registry_from_mcp((await mcp.list_tools()).get("tools", []))

        prompt = _build_system_prompt()

        assert "No live MCP tools are loaded" not in prompt, (
            "System prompt still shows the 'no tools' fallback text.\n"
            "TOOL_REGISTRY is empty when _build_system_prompt() runs.\n"
            "Ensure populate_registry_from_mcp() is called before building the prompt."
        )
        assert "get_entity_types" in prompt, (
            "System prompt does not mention 'get_entity_types'.\n"
            f"Prompt excerpt (first 400 chars):\n{prompt[:400]}\n"
            "Tools may not be appearing in the DISCOVERY section."
        )
        assert "DISCOVERY" in prompt, (
            "System prompt is missing the DISCOVERY tool section.\n"
            "Check _build_system_prompt() categorisation keywords."
        )


# ── 6. End-to-end round-trip ─────────────────────────────────────────────────

class TestEndToEnd:
    """Full path: user message → Gemini agent → MCP tool call → response."""

    async def test_get_entity_types_triggers_tool_call(self):
        """
        Asking for entity types must cause the agent to call get_entity_types
        on the real MCP server and return a non-empty list.
        """
        from gemini_agent import GeminiAgent

        mcp = OctaneMcpClient()
        populate_registry_from_mcp((await mcp.list_tools()).get("tools", []))

        agent = GeminiAgent()
        await agent.refresh_tools(mcp)

        summary, artifacts, mcp_called = await agent.run(
            "Call get_entity_types and list all available entity types",
            mcp,
            context_id="preflight-e2e-entity-types",
            bearer_token=config.API_KEY,
        )

        assert mcp_called, (
            "Agent did NOT call any MCP tool.\n"
            f"Agent response: {summary!r}\n\n"
            "Expected: agent calls get_entity_types on the MCP server.\n"
            "Possible causes:\n"
            "  • get_entity_types not in the agent's tool list\n"
            "  • System prompt is too restrictive\n"
            "  • Gemini chose to answer from knowledge instead of calling the tool"
        )

        assert summary and len(summary) > 30, (
            f"Agent returned an empty or trivially short response: {summary!r}\n"
            "Expected a list of entity type names."
        )

        assert "(no response" not in summary.lower(), (
            f"Agent returned a no-response placeholder: {summary!r}\n"
            "The Gemini model call may have failed or timed out."
        )

    async def test_filtered_entity_query_uses_discovery_chain(self):
        """
        A filtered query should trigger: get_entity_types → get_filter_metadata
        (or get_entity_field_metadata) → get_entities, not a flat refusal.
        """
        from gemini_agent import GeminiAgent

        mcp = OctaneMcpClient()
        populate_registry_from_mcp((await mcp.list_tools()).get("tools", []))

        agent = GeminiAgent()
        await agent.refresh_tools(mcp)

        summary, artifacts, mcp_called = await agent.run(
            "List all defects that are not in Done phase",
            mcp,
            context_id="preflight-e2e-filtered-query",
            bearer_token=config.API_KEY,
        )

        assert mcp_called, (
            "Agent did NOT call any MCP tool for a filtered entity query.\n"
            f"Agent response: {summary!r}\n\n"
            "The agent should discover entity types and filter syntax before querying.\n"
            "Check the system prompt reasoning-workflow section."
        )

        refusal_phrases = [
            "cannot fulfill",
            "do not have access",
            "not able to",
            "unable to",
            "no tools",
        ]
        lower = summary.lower()
        matched = [p for p in refusal_phrases if p in lower]
        assert not matched, (
            f"Agent returned a refusal instead of querying Octane.\n"
            f"Matched refusal phrases: {matched}\n"
            f"Response: {summary!r}\n\n"
            "The agent must use MCP tools rather than refusing."
        )
