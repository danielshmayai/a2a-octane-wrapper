# CLAUDE.md — A2A Octane Wrapper

## Project Overview
A2A (Agent-to-Agent) wrapper around OpenText SDP's MCP server for ALM Octane.
Backend: Python + FastAPI | AI: Gemini (google-genai/google-adk) | Protocol: A2A + MCP

## Key Commands
- `python main.py`             — Start the A2A wrapper (port 9000)
- `python mock_oauth_server.py` — Start mock OAuth server (port 8090)
- `pytest`                      — Run test suite
- `pytest --html=reports/report.html` — Tests with HTML report

## Architecture
- FastAPI server exposing A2A protocol endpoints
- MCP client connecting to OpenText SDP (Octane) MCP server
- Gemini agent for AI-powered tool routing and response generation
- OAuth2 (OTDS) authentication support
- Tool discovery via MCP polling

## Configuration
- All config via environment variables (see `config.py` and `.env.example`)
- Octane MCP: sharedSpaceId=1001, workSpaceId per `.env`
- Gemini model: configurable via GEMINI_MODEL env var

## Coding Standards
- Python with type hints
- Pydantic models for all data structures (see `a2a_models.py`)
- Async/await throughout (FastAPI + httpx + MCP client)
- Tests in `tests/` directory, using pytest-asyncio
- All MCP tool calls wrapped in try-except with error handling
- Token resolution: 3-level strategy (no A2A key → admin key → passthrough)

## Critical Patterns
- Dual-mode: Gemini agent (with API key) or keyword-based router (fallback)
- MCP: Official SDK with Streamable HTTP transport, connection pooling via httpx.AsyncClient
- Tool discovery: Auto-fetch from MCP server at startup + periodic polling
- A2A protocol: Both HTTP+JSON (POST /message:send) and JSON-RPC 2.0 (POST /)
- Octane MCP: sharedSpaceId=1001, create_comment uses `text` param (NOT `comment`)

## When Compacting
Preserve: list of modified files, current test status, MCP tool parameter
discoveries, A2A protocol issues, Gemini agent bugs, and any auth/token
resolution gotchas found during this session.

## Reference Infrastructure
For Claude API patterns, MCP integration best practices, model routing,
security, deployment, and architecture guidance, refer to:
`C:/Daniel/AI/claude/claude-app-infrastructure/`

Key references:
- MCP patterns: `claude-app-infrastructure/skills/mcp-orchestrator/SKILL.md`
- Security: `claude-app-infrastructure/skills/security/SKILL.md`
- Testing/evals: `claude-app-infrastructure/skills/testing/SKILL.md`
- Deployment: `claude-app-infrastructure/skills/deployment/SKILL.md`
- Auth (OAuth/PKCE): `claude-app-infrastructure/skills/auth/SKILL.md`
- Performance: `claude-app-infrastructure/skills/performance/SKILL.md`
