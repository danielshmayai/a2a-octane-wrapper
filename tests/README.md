# Testing Guide

This project has two layers of tests: **unit tests** (fast, no servers needed) and an **integration test script** (requires live servers).

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
.venv-1\Scripts\python.exe -m pytest
```

Or with verbose output:

```bash
.venv-1\Scripts\python.exe -m pytest -v
```

No environment variables or running servers are required.

---

## 2. Integration Test — `test_a2a_auth_flow.py`

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

## 3. Mock OAuth Server — `mock_oauth_server.py`

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

## 4. Pre-commit Hook

The pre-commit hook runs `pytest` (unit tests only) before every commit.
The integration test script is **excluded** from automatic runs because it requires live servers.

To run the hook manually:

```bash
pre-commit run --all-files
```

To skip the hook in an emergency (not recommended):

```bash
git commit --no-verify -m "your message"
```
