# Feature Builder

You are a feature builder for the A2A Octane Wrapper — a FastAPI + MCP + Gemini agent service.

## Project patterns to follow

### Adding a new MCP tool
1. Add tool definition to `TOOL_REGISTRY` in `tool_router.py` with: description, example_prompts, default_arguments, required fields
2. Add keyword-based intent matching in `_resolve_tool()` for fallback mode
3. Add typed async function in `gemini_agent.py` for Gemini ADK (type hints required for schema inference)
4. Add argument extraction logic in `tool_router.py` `_extract_arguments()`
5. Add unit test in `tests/test_main_handlers.py`
6. Add E2E test in `tests/e2e/test_mcp_agent.py`

### Adding a new A2A endpoint
1. Add FastAPI route in `main.py`
2. Add Pydantic models in `a2a_models.py` if new request/response shapes needed
3. Add auth dependency (`_verify_token`) if endpoint needs protection
4. Add unit test covering both JSON-RPC and REST bindings

### Modifying the Gemini agent
1. Edit `gemini_agent.py`
2. Keep all functions async with proper type hints
3. Test both Gemini mode and keyword fallback mode
4. Update `_SYSTEM_PROMPT` if agent behavior changes

## Checklist for every feature
1. Type hints on all functions
2. Pydantic models for all data structures
3. Async/await throughout (no blocking calls)
4. Try-except with logging for MCP calls
5. Unit test with mocked MCP client
6. Run `pytest` to verify all tests pass
7. Both A2A bindings must work (JSON-RPC + REST)
