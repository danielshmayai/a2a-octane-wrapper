---
globs: gemini_agent.py
---

# Gemini Agent Rules

## Architecture
- Uses google-adk (Agent Development Kit) with LlmAgent + Runner + InMemorySessionService
- Per-session history keyed by `contextId` — enables follow-up questions
- Typed async Python functions (one per MCP tool) — ADK infers Gemini schemas from them

## System prompt
- Located at top of `gemini_agent.py` as `_SYSTEM_PROMPT`
- Instructs agent to: use tools before answering, present data clearly (not raw JSON),
  handle errors gracefully, maintain conversation context across turns

## Text generation patterns
- `_maybe_inject_generated_text()` — pre-generates comment text via separate Gemini call
- `_generate_joke()` — local tool, generates via Gemini API with timeout
- Both use `GEMINI_REQUEST_TIMEOUT_SECONDS` timeout with `asyncio.wait_for()`

## Error handling
- Tool execution errors fed back as text to the agent (not exceptions)
- Agent explains errors in natural language
- Pre-generation timeouts fall back to original message

## When modifying the agent
- Keep functions typed — ADK needs type hints to generate Gemini tool schemas
- Don't add blocking calls — everything must be async
- Test both Gemini mode and keyword fallback mode (tool_router.py)
- Session cleanup: InMemorySessionService grows unbounded — consider for production
