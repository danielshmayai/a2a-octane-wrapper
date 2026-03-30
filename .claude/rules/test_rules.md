---
globs: tests/**
---

# Testing Rules

## Test structure
- `tests/conftest.py` — auto-mocks MCP client to prevent network calls during unit tests
- `tests/test_main_handlers.py` — unit tests for A2A endpoints (both JSON-RPC and REST)
- `tests/test_tools_and_docs.py` — documentation and build verification
- `tests/e2e/` — end-to-end tests with live MCP server
- `tests/a2a_auth_flow.py` — OAuth2 integration tests

## Running tests
- Unit tests: `pytest` (runs tests/ only, per pytest.ini `testpaths = tests`)
- E2E tests: `pytest tests/e2e/ -v` (requires live MCP server)
- With HTML report: `pytest --html=reports/report.html`
- Async mode: `asyncio_mode = strict` (required for proper async isolation)
- Timeout: 10 seconds per test (prevents hangs)

## When writing tests
- Mock MCP client for unit tests (conftest.py auto-use fixture)
- Test both Gemini agent path AND keyword fallback path
- Verify JSON-RPC envelope format: `jsonrpc="2.0"`, `id` echoed, proper `error`/`result` fields
- Check metadata flags: `mcp_called`, `auth_injected` in task.metadata
- Test role-based behavior: admin token vs user token vs no token

## CI/CD
- GitHub Actions: Python 3.11 + 3.12 matrix
- Runs on push to main/master and all PRs
- Produces JUnit XML + HTML report artifacts
