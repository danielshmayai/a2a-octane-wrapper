# Testing Guide

This project has three layers of tests: **unit tests** (fast, no servers), **E2E tests** (DeepEval + Gemini judge), and an **integration test script** (requires live servers).

---

## 1. Unit Tests — `tests/`

These run automatically on every `git commit` via the pre-commit hook.

### What they cover
| File | Tests |
|------|-------|
| `tests/test_main_handlers.py` | A2A task response metadata (`auth_injected`, `mcp_called`), agent error handling |
| `tests/test_tools_and_docs.py` | Tool registry completeness, README and UI references to key fields |

### Run locally

```bash
# From the repo root
pytest tests/test_*.py -v
```

No environment variables or running servers are required.

---

## 2. E2E Tests — `tests/e2e/`

End-to-end tests that validate the Internal Agent's MCP tool-calling logic using **DeepEval** and **Gemini 2.0 Flash** as an LLM judge. The MCP server is fully mocked — no real Opentext SDP connection needed.

### Architecture

```
User text  →  keyword router (tool_router.py)  →  execute_tool()  →  mock MCP client
                                                        ↓
                                                  A2A Task response
                                                        ↓
                                              DeepEval metrics evaluate
```

### Three test tiers

| Tier | Class | Tests | API Cost | What it validates |
|------|-------|-------|----------|-------------------|
| 1 | `TestToolCorrectness` | 6 | **Zero** | Plain pytest assertions — correct MCP tool called with correct arguments (intent resolution + argument extraction) |
| 2 | `TestToolCorrectnessDeepEval` | 3 | ~$0.001 | DeepEval `ToolCorrectnessMetric` with Gemini judge — formally compares actual `ToolCall` objects (name + input_parameters) against expected ones |
| 3 | `TestAnswerRelevancy` | 3 | ~$0.001 | DeepEval `AnswerRelevancyMetric` with Gemini judge — scores whether the agent's final response is relevant to the user's question (0-1 scale, threshold 0.5) |

### What each test covers

**Tier 1 — Tool Correctness (free, no API key)**

| Test | Input | Validates |
|------|-------|-----------|
| `test_get_defect_routes_correctly` | "Get defect 1314" | `get_defect` called with `entityId=1314`, task COMPLETED |
| `test_get_story_routes_correctly` | "Get story 55" | `get_story` called with `entityId=55`, task COMPLETED |
| `test_get_comments_routes_correctly` | "Show comments on defect 1314" | `get_comments` called with `entityId=1314`, `entityType=defect` |
| `test_fetch_my_work_items_routes_correctly` | "What are my work items?" | `fetch_My_Work_Items` called (no args), task COMPLETED |
| `test_create_comment_routes_correctly` | "add a comment saying Verified on defect 1314" | `create_comment` called with `entityId=1314`, `entityType=defect` |
| `test_wrong_tool_not_called` | "Get defect 1314" | `get_story` is NOT called (negative test) |

**Tier 2 — DeepEval ToolCorrectnessMetric (requires GEMINI_API_KEY)**

| Test | What DeepEval checks |
|------|----------------------|
| `test_defect_tool_correctness` | Tool name + `input_parameters` exact match against expected `get_defect({entityId: 1314})` |
| `test_work_items_tool_correctness` | Tool name match for `fetch_My_Work_Items` |
| `test_create_comment_tool_correctness` | Tool name + full arg match: `entityId`, `entityType`, `text` |

**Tier 3 — AnswerRelevancyMetric (requires GEMINI_API_KEY)**

| Test | Gemini judges |
|------|---------------|
| `test_defect_response_is_relevant` | Response to "Get defect 1314" is relevant to defect retrieval |
| `test_work_items_response_is_relevant` | Response to "What are my work items?" is relevant to work items |
| `test_comments_response_is_relevant` | Response to "Show comments on defect 1314" is relevant to comments |

### Configuration

The E2E tests use `tests/e2e/conftest.py` which:

1. **Loads `.env`** from the repo root automatically (`GEMINI_API_KEY`).
2. **Disables Confident AI** — sets `DEEPEVAL_RESULTS_FOLDER` and `DEEPEVAL_TELEMETRY_OPT_OUT` so no cloud login is required. Results are saved as local JSON in `./deepeval_results/`.
3. **Wraps Gemini 2.0 Flash** as a `DeepEvalBaseLLM` subclass (`GeminiFlashModel`) so DeepEval metrics use Gemini instead of the default OpenAI GPT.
4. **Exposes a `gemini_judge` fixture** (session-scoped) that Tier 2 and Tier 3 tests inject as a parameter.

### Prerequisites

```bash
# Install dependencies (first time only)
pip install -r requirements.txt
```

Ensure `GEMINI_API_KEY` is set in your `.env` file (required for Tier 2 and 3 only).

### Run

```powershell
# All E2E tests (12 tests)
pytest tests/e2e/ -v

# Tier 1 only — free, no API key needed
pytest tests/e2e/ -k "TestToolCorrectness and not DeepEval" -v

# Tier 2 + 3 only — Gemini-judged
pytest tests/e2e/ -k "DeepEval or Relevancy" -v
```

### Run scripts

Convenience scripts that run unit + E2E together:

```powershell
# PowerShell
.\run_tests.ps1              # all tests (unit + E2E)
.\run_tests.ps1 unit         # unit tests only
.\run_tests.ps1 e2e          # E2E tests only
.\run_tests.ps1 e2e-free     # Tier 1 only (free)
```

```bash
# Bash
./run_tests.sh               # all tests
./run_tests.sh unit          # unit tests only
./run_tests.sh e2e           # E2E tests only
./run_tests.sh e2e-free      # Tier 1 only (free)
```

---

## 3. Integration Test — `test_a2a_auth_flow.py`

End-to-end test of the full OAuth2 authentication flow. Requires both servers running.

### Prerequisites

**Step 1 — Install dependencies** (first time only):

```bash
.venv-1\Scripts\pip install -r requirements.txt
```

**Step 2 — Configure** `.env` (copy from `.env.example`):

```bash
copy .env.example .env
# Edit .env and fill in your OCTANE_BASE_URL, API_KEY, etc.
```

**Step 3 — Start the servers**

Use the restart script (starts both in the background):

```powershell
.\restart.ps1
```

Or start them manually in separate terminals:

```bash
# Terminal 1 — A2A wrapper (port 9000)
.venv-1\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 9000 --reload

# Terminal 2 — Mock OAuth server (port 8090)
.venv-1\Scripts\python.exe mock_oauth_server.py
```

**Step 4 — Run the integration test**

```bash
# Run all flows
.venv-1\Scripts\python.exe test_a2a_auth_flow.py

# Run a specific flow only
.venv-1\Scripts\python.exe test_a2a_auth_flow.py --flow cc        # Client Credentials only
.venv-1\Scripts\python.exe test_a2a_auth_flow.py --flow pkce      # Authorization Code + PKCE only
.venv-1\Scripts\python.exe test_a2a_auth_flow.py --flow send      # /message:send only
.venv-1\Scripts\python.exe test_a2a_auth_flow.py --flow discovery # AgentCard discovery only

# Show full request/response payloads
.venv-1\Scripts\python.exe test_a2a_auth_flow.py --verbose
```

### What it tests

| Step | Description |
|------|-------------|
| 1 — AgentCard Discovery | `GET /.well-known/agent-card.json` → validates `csai_oauth` security scheme, PKCE config, and scopes |
| 2 — Client Credentials | `POST /oauth2/token` with `client_id` + `client_secret` → verifies token issuance and JWT claims |
| 3 — Auth Code + PKCE | `GET /oauth2/auth` → `POST /oauth2/token` with S256 code verifier → verifies PKCE enforcement (wrong verifier is rejected with 400) |
| 4 — `/message:send` | Calls the A2A endpoint with the Bearer token → verifies `auth_injected: true` in task metadata |
| Bonus — OIDC Discovery | `GET /.well-known/openid-configuration` on the mock server |

### Browser-based flow visualizer

Open `http://localhost:9000/auth-test` for an animated step-by-step visualization of the same flows with live request/response payloads.

---

## 4. Mock OAuth Server — `mock_oauth_server.py`

Simulates the OTDS OAuth2 authorization server locally. **Not used in production.**

| Endpoint | Description |
|----------|-------------|
| `GET  /oauth2/auth` | Authorization endpoint — auto-approves and redirects with auth code |
| `POST /oauth2/token` | Token endpoint — supports `client_credentials` and `authorization_code` grants |
| `GET  /.well-known/openid-configuration` | OIDC discovery |
| `GET  /oauth2/introspect?token=...` | Decode and inspect a mock token |
| `GET  /health` | Liveness check (also used as `redirect_uri` in PKCE tests) |

**Demo credentials** (hardcoded):

```
client_id:     csai-demo-client
client_secret: csai-demo-secret
scopes:        otds:groups  otds:roles  search
```

Custom port:

```bash
.venv-1\Scripts\python.exe mock_oauth_server.py --port 8091
# Update OAUTH_BASE_URL in .env or pass --oauth http://localhost:8091 to the test script
```

---

## 5. Pre-commit Hook

The pre-commit hook runs `pytest` (unit tests only) before every commit.
The E2E and integration tests are **excluded** from automatic runs because they require API keys / live servers.

To run the hook manually:

```bash
pre-commit run --all-files
```

To skip the hook in an emergency (not recommended):

```bash
git commit --no-verify -m "your message"
```

---

## Test Summary

| Layer | Location | Requires | Run with |
|-------|----------|----------|----------|
| Unit tests | `tests/test_*.py` | Nothing | `pytest tests/test_*.py` |
| E2E — Tier 1 (free) | `tests/e2e/` | Nothing | `pytest tests/e2e/ -k "not DeepEval"` |
| E2E — Tier 2+3 (Gemini) | `tests/e2e/` | `GEMINI_API_KEY` in `.env` | `pytest tests/e2e/` |
| Integration (OAuth) | `test_a2a_auth_flow.py` | Live servers | `python test_a2a_auth_flow.py` |
