# A2A Opentext SDP MCP Agent Wrapper

A lightweight Python service that bridges the **Google Agent-to-Agent (A2A) protocol** with the **Opentext SDP MCP Server**, powered by a **Gemini function-calling agent** that understands natural language, drives multi-step Opentext SDP tool calls, and maintains **per-session conversation memory**.

---

## Ways to Use This Agent

Once the wrapper is running (`python main.py`), you can interact with the OT ADM Agent in four different ways — no enterprise account required for options 1–3:

| # | Option | Access | Best for |
|---|---|---|---|
| 1 | **Built-in Chat UI** | Browser → `http://localhost:9000` | Quick testing, demos |
| 2 | **REST API (curl / code)** | HTTP POST to `/message:send` | Scripts, CI, custom integrations |
| 3 | **Any A2A-compatible client** | Point at `http://localhost:9000` | Other A2A platforms |
| 4 | **Google Gemini Enterprise** | Cloud Console registration (admin) | Enterprise users, `@mention` flow |

### Option 1 — Built-in Chat UI

Open a browser and navigate to `http://localhost:9000`. A full chat interface loads immediately with suggestion chips, collapsible raw data, and multi-turn memory — no configuration needed.

### Option 2 — REST API (curl / any HTTP client)

Call the A2A endpoint directly from any tool or script:

```bash
curl -X POST http://localhost:9000/message:send \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "messageId": "msg-1",
      "contextId": "my-session-001",
      "role": "ROLE_USER",
      "parts": [{"text": "Get defect 2110"}]
    },
    "configuration": {"blocking": true}
  }'
```

Reuse the same `contextId` across requests to maintain conversation history. Works from Postman, Python `httpx`/`requests`, or any language.

**Python example:**

```python
import httpx, uuid

session_id = str(uuid.uuid4())

def ask(text: str) -> str:
    r = httpx.post("http://localhost:9000/message:send", json={
        "message": {
            "messageId": str(uuid.uuid4()),
            "contextId": session_id,
            "role": "ROLE_USER",
            "parts": [{"text": text}]
        },
        "configuration": {"blocking": True}
    })
    task = r.json()
    # Extract text from the first artifact
    return task["status"]["message"]["parts"][0]["text"]

print(ask("Get defect 2110"))
print(ask("Who is the owner?"))   # follow-up — retains context
```

### Option 3 — Any A2A-compatible client or platform

The wrapper is a standard A2A server. Any client or platform that supports the A2A protocol can discover and use it:

1. Point the client at your wrapper's base URL: `http://localhost:9000` (or the public HTTPS URL).
2. The client fetches the AgentCard from `GET /.well-known/agent-card.json` for discovery.
3. It sends messages to `POST /message:send`.

This works with any A2A-compliant orchestrator — Google's ADK, LangGraph, CrewAI, or a custom implementation.

### Option 4 — Google Gemini Enterprise (`@mention` flow)

Requires a Gemini Enterprise subscription (Standard / Plus / Frontline) and admin access to the Google Cloud Console. See [Section 10](#10-connecting-to-google-agentspace) for the full step-by-step registration guide.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Prerequisites](#3-prerequisites)
4. [Setup](#4-setup)
5. [Running the Server](#5-running-the-server)
6. [API Endpoints](#6-api-endpoints)
7. [Supported Tools](#7-supported-tools)
8. [Multi-turn Conversation](#8-multi-turn-conversation)
9. [Chat UI](#9-chat-ui)
10. [Connecting to Google Gemini Enterprise](#10-connecting-to-google-agentspace)
11. [Project Structure](#11-project-structure)
12. [Troubleshooting](#12-troubleshooting)

> **Tip — VS Code users:** Open the rendered preview with **Ctrl+Shift+V** so all Table of Contents links are clickable.

---

<a id="1-overview"></a>
## 1. Overview

This service acts as a bridge between three systems:

```
Google Gemini Enterprise / A2A Client
        │
        │  HTTP+JSON  (A2A protocol)
        copy env.example .env

        # Linux / macOS
┌─────────────────────────────────┐
│   A2A Opentext SDP Wrapper  :9000     │
│   + Gemini Agent                │
│   + per-session chat history    │
└─────────────────────────────────┘
        │
        │  JSON-RPC 2.0  (MCP protocol)
        ▼
┌──────────────────────┐
│  Opentext SDP MCP Server   │
│  /mcp endpoint       │
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│  Opentext SDP │
└──────────────────────┘
```

When `GEMINI_API_KEY` is set, the wrapper uses a **Gemini function-calling agent** that:

1. Understands the user's free-text request via an LLM
2. Decides which Opentext SDP tool(s) to call
3. Executes them against the Opentext SDP MCP server
4. Returns a natural-language summary with raw Opentext SDP data as collapsible artifacts
5. **Retains full conversation history per session** — follow-up questions like "who owns it?" or "show me the next one" work seamlessly

Without a Gemini API key it falls back to a lightweight **keyword-based router**.

---

<a id="2-architecture"></a>
## 2. Architecture

```
                 ┌──────────────────────────────────────┐
                 │         FastAPI App (main.py)         │
                 │                                       │
                 │  GET  /.well-known/agent-card.json    │
                 │  POST /message:send                   │
                 │  GET  /tools                          │
                 │  GET  /health                         │
                 │  GET  /  (static chat UI)             │
                 └────────────────┬─────────────────────┘
                                  │
              ┌───────────────────┴──────────────────┐
              │                                      │
     GEMINI_API_KEY set?                      No API key
              │                                      │
    ┌─────────▼──────────┐              ┌────────────▼──────────┐
    │   gemini_agent.py  │              │   tool_router.py       │
    │   GeminiAgent.run()│              │   resolve_intent()     │
    │   Agentic loop     │              │   extract_args()       │
    └─────────┬──────────┘              └────────────┬──────────┘
              │  FunctionCall                        │
    ┌─────────▼──────────────────────────────────────▼──────────┐
    │                    tool_router.py                          │
    │                    execute_tool()  ──► mcp_client.py      │
    └────────────────────────────────────────────────────────────┘
                                  │  JSON-RPC 2.0  POST /mcp
                                  ▼
                        ┌─────────────────────┐
                        │  Octane MCP Server  │
                        └─────────────────────┘
```

| File | Role |
|---|---|
| `main.py` | FastAPI bootstrap, A2A endpoints, routes to Gemini agent or keyword fallback |
| `gemini_agent.py` | Gemini function-calling agentic loop, per-session history, auto-text generation |
| `a2a_models.py` | Pydantic models: `Message`, `Task`, `Artifact`, `AgentCard`, `AgentSkill` |
| `mcp_client.py` | JSON-RPC 2.0 async client for the Opentext SDP MCP `/mcp` endpoint |
| `tool_router.py` | `TOOL_REGISTRY`, `resolve_intent()`, `extract_arguments()`, `execute_tool()` |
| `config.py` | Single source of truth for all env-var configuration |

---

<a id="3-prerequisites"></a>
## 3. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Required by `google-genai` SDK |
| Opentext SDP MCP Server | Must be running and network-reachable |
| Opentext SDP API key | Bearer token for authenticating MCP requests |
| Google Gemini API key | **Optional** — activates the full agentic experience. Without it the wrapper uses keyword routing. |
| Public HTTPS URL | **Required only for Google Agentspace integration** — ngrok works for development |

---

<a id="4-setup"></a>
## 4. Setup

### 4.1 Clone and create a virtual environment

```bash
git clone <repo-url>
cd a2a-octane-wrapper

# Windows
python -m venv .venv
.venv\Scripts\activate

# Linux / macOS
python -m venv .venv
source .venv/bin/activate
```

### 4.2 Install dependencies

```bash
pip install -r requirements.txt
```

Dependencies installed:

| Package | Purpose |
|---|---|
| `fastapi` | Web framework for the A2A endpoints |
| `uvicorn[standard]` | ASGI server |
| `httpx` | Async HTTP client for MCP calls |
| `pydantic` | A2A protocol data models |
| `python-dotenv` | `.env` file loading |
| `google-genai` | Gemini function-calling SDK |
| `aiofiles` | Async static file serving |
| `mcp` | Official MCP SDK; streamable HTTP transport for Opentext SDP `/mcp` |
| `google-adk` | Google Agent Development Kit (ADK) — LlmAgent + Runner for agent orchestration |

### 4.3 Configure environment variables

Create a `.env` file in the project root:

```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

Edit `.env` with your values:

```env
# ── Octane MCP Server Connection ──────────────────────────────────────────────
OCTANE_BASE_URL=https://your-octane-server.example.com
API_KEY=your_octane_api_key

# ── Octane Context (injected automatically into every MCP tool call) ──────────
DEFAULT_SHARED_SPACE_ID=1001
DEFAULT_WORKSPACE_ID=1002

# ── Gemini Agent (optional — enables LLM-powered intent + conversation memory) ─
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.0-flash

# ── Wrapper Settings ───────────────────────────────────────────────────────────
A2A_HOST=0.0.0.0
A2A_PORT=9000
MCP_REQUEST_TIMEOUT_SECONDS=30
```

#### Full variable reference

| Variable | Default | Required | Description |
|---|---|---|---|
| `OCTANE_BASE_URL` | `http://localhost:8080` | ✅ | Base URL of the Octane MCP server |
| `API_KEY` | _(empty)_ | ✅ | Bearer token for Octane authentication |
| `DEFAULT_SHARED_SPACE_ID` | `1001` | ✅ | Octane shared space ID — injected into every request |
| `DEFAULT_WORKSPACE_ID` | `1002` | ✅ | Octane workspace ID — injected into every request |
| `GEMINI_API_KEY` | _(empty)_ | ⚡ Recommended | Google Gemini API key — activates the LLM agent |
| `GEMINI_MODEL` | `gemini-2.0-flash` | — | Gemini model to use |
| `A2A_HOST` | `0.0.0.0` | — | Host to bind the wrapper to |
| `A2A_PORT` | `9000` | — | Port to listen on |
| `MCP_REQUEST_TIMEOUT_SECONDS` | `30` | — | Timeout (seconds) for upstream Octane calls |

> **Where to find the Opentext SDP IDs:** In your Opentext SDP browser URL the path contains `/ui/entity-navigation?p=<sharedSpaceId>/<workspaceId>`. Use those numbers.

---

<a id="5-running-the-server"></a>
## 5. Running the Server

```bash
python main.py
```

The server starts at `http://localhost:9000`.

Verify it is working:

```bash
# Liveness check
curl http://localhost:9000/health
# → {"status": "ok"}

# Inspect the AgentCard
curl http://localhost:9000/.well-known/agent-card.json

# List all tools the Octane MCP server exposes
curl http://localhost:9000/tools
```

---

<a id="6-api-endpoints"></a>
## 6. API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Built-in chat UI |
| `GET` | `/.well-known/agent-card.json` | A2A AgentCard discovery endpoint |
| `POST` | `/message:send` | Primary A2A endpoint — send a message, receive a Task |
| `GET` | `/tools` | Lists all MCP tools exposed by the Octane server |
| `GET` | `/health` | Liveness check — returns `{"status": "ok"}` |

### Example: Calling `/message:send` directly

```bash
curl -X POST http://localhost:9000/message:send \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "messageId": "msg-1",
      "contextId": "my-session-123",
      "role": "ROLE_USER",
      "parts": [{"text": "Get defect 2110"}]
    },
    "configuration": {"blocking": true}
  }'
```

The `contextId` field is your **session ID** — reuse it across requests to maintain conversation history.

#### Task metadata (new)

Responses now include explicit metadata on the returned `task` object describing
how the wrapper handled authentication and MCP forwarding. Check `task.metadata`:

- `mcp_called` (boolean): true if the wrapper forwarded the request to the
  Octane MCP server (one or more MCP tool calls were executed).
- `auth_injected` (boolean): true if the wrapper injected or substituted a
  bearer token (server API key or simulated token) into the request.

The built-in Chat UI uses these flags to render the auth/forwarding visualization
accurately (it no longer guesses from artifact counts).

---

<a id="7-supported-tools"></a>
## 7. Supported Tools

| Tool | Description | Example prompts |
|---|---|---|
| `get_defect` | Retrieve a defect by numeric ID | `Get defect 2110`, `Show me bug #9001` |
| `get_story` | Retrieve a user story by numeric ID | `Get story 1234`, `Show user story 55` |
| `get_feature` | Retrieve a feature by numeric ID | `Get feature 77`, `Show feature 200` |
| `get_comments` | Get all comments for an entity | `Show comments on defect 2110` |
| `create_comment` | Post a new comment on a work item | `Add a comment to defect 2110 saying "Reproduced"` |
| `update_comment` | Edit an existing comment | `Update comment 99 on defect 2110 with "Fixed in build 5.3"` |
| `fetch_My_Work_Items` | List the current user's assigned items | `What are my work items?`, `Show my backlog` |

> `sharedSpaceId` and `workSpaceId` are injected automatically from `.env` — you never need to supply them in prompts.

---

<a id="8-multi-turn-conversation"></a>
## 8. Multi-turn Conversation

The Gemini agent maintains **per-session conversation history**. Each session generates a stable `contextId` sent with every message, giving Gemini full context for follow-up questions.

```
User:  Get defect 2110
Agent: Defect 2110 — "Login page crashes on empty password"
       Phase: In Progress  |  Severity: High  |  Assigned: Alice

User:  Who is the owner?
Agent: The defect is assigned to Alice (alice@example.com).

User:  Add a comment saying "Reproduced on build 5.3"
Agent: Done — comment posted to defect 2110.

User:  Add a funny comment
Agent: Posted: "This bug is so slippery it should have its own LinkedIn profile."
```

History is retained for up to **40 content blocks** per session. Refreshing the browser starts a new session.

---

<a id="9-chat-ui"></a>
## 9. Chat UI

A browser-based chat interface is served at `http://localhost:9000`.

Features:
- OpenText logo in the header
- Suggestion chips for common queries (defects, stories, work items)
- Collapsible raw Octane data under each response
- Markdown-style bold and bullet rendering

---

<a id="10-connecting-to-google-agentspace"></a>
## 10. Connecting to Google Gemini Enterprise (formerly Agentspace)

> ⚠️ **Product renamed:** Google Agentspace is now called **Gemini Enterprise**.
> Both old URLs (`vertexaisearch.cloud.google.com` and `agentspace.google.com`) return **404**.
> Registration is done through **[console.cloud.google.com/gemini-enterprise](https://console.cloud.google.com/gemini-enterprise/)**.

**Gemini Enterprise** is Google's enterprise AI assistant platform. It supports **custom A2A agents** that users can invoke by typing `@` in the chat input.

> **Access requirements:**
> - Requires the **Discovery Engine Admin** IAM role in your Google Cloud project
> - Requires an existing **Gemini Enterprise app** (Standard / Plus / Frontline edition)
> - Requires a **Google Workspace** account — personal Gmail does not work
> - This feature is currently in **Preview** (pre-GA)

> **Official documentation:** [Register and manage A2A agents](https://docs.cloud.google.com/gemini/enterprise/docs/register-and-manage-an-a2a-agent)

---

### What you will achieve

After registration, the OT ADM Agent appears in the `@` popover alongside built-in Google agents:

```
┌────────────────────────────────────────────────┐
│  Google Gemini Enterprise    Hello, [User]      │
│  ─────────────────────────────────────────────  │
│                                                 │
│            Agents                               │
│  ┌───────────────────────────────────────────┐  │
│  │ ● Content Aviator Agent                   │  │
│  │   AI-powered document analysis an…        │  │
│  │ ● Deep Research                           │  │
│  │   Get in-depth answers grounded in…       │  │
│  │ ● OT ADM Agent                ◄── yours     │  │  ← appears after registration
│  │   Query and manage Opentext SDP work…       │  │
│  └───────────────────────────────────────────┘  │
│                                                 │
│  [ @_________________________________________ ] │
│  Take action   Analyze data   Write code  …     │
└────────────────────────────────────────────────┘
```

> **Important:** Custom A2A agents **cannot** be added from the Gemini Enterprise web UI directly. Registration is done by an **administrator** in the **Google Cloud Console**. Once registered there, the agent becomes available to all users in the Gemini Enterprise workspace.

---

### Step 1 — Make the wrapper publicly reachable

Gemini Enterprise is a cloud service. It **must reach your wrapper over HTTPS** — `localhost` will not work.

**Option A — Development / testing (ngrok):**

```bash
# One-time setup: create free account at https://dashboard.ngrok.com/signup
# Get token at https://dashboard.ngrok.com/get-started/your-authtoken
ngrok config add-authtoken <YOUR_AUTHTOKEN>

# Start the wrapper
python main.py

# In a second terminal, open the tunnel
ngrok http 9000
# → Forwarding: https://abc123.ngrok-free.app -> http://localhost:9000
```

> The ngrok URL changes on every restart. You will need to re-register the agent in the Cloud Console each time it changes. A paid ngrok plan gives you a stable static domain.

**Option B — Cloudflare Tunnel (no sign-up required):**

```bash
# Download cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cloudflared tunnel --url http://localhost:9000
# → https://random-name.trycloudflare.com
```

**Option C — Production deployment:**

| Platform | Notes |
|---|---|
| **Google Cloud Run** | `gcloud run deploy a2a-octane --source . --port 9000` — auto-managed TLS |
| **Google App Engine** | `gcloud app deploy` — auto-managed TLS |
| **Any Linux VM** | nginx reverse proxy + Let's Encrypt (`certbot`) |

---

### Step 2 — Verify the AgentCard is reachable

```bash
PUBLIC_URL=https://abc123.ngrok-free.app  # replace with your actual URL

curl $PUBLIC_URL/.well-known/agent-card.json   # must return valid JSON
curl $PUBLIC_URL/health                         # must return {"status":"ok"}
```

Copy the full JSON output from the first command — you will paste it in Step 3.

---

### Step 3 — Open the Google Cloud Console

1. Go to **[console.cloud.google.com/gemini-enterprise](https://console.cloud.google.com/gemini-enterprise/)**.
2. Make sure you are in the **correct Google Cloud Project** (the one linked to your Gemini Enterprise subscription).
3. Confirm you have the **Discovery Engine Admin** IAM role. Without it you will not see the Agents menu.
   - Cloud Console → IAM & Admin → IAM → find your account → confirm `roles/discoveryengine.admin`

---

### Step 4 — Navigate to Agents and add a Custom A2A agent

1. On the Gemini Enterprise page you will see a list of your **Apps**. Click the name of the App you want to add the agent to.
2. In the left-hand navigation, click **Agents**.
3. Click **+ Add Agents** at the top of the screen.
4. Under **Choose an agent type**, find **Custom agent via A2A** and click **Add**.

---

### Step 5 — Paste the AgentCard JSON

The form field is labelled **"Agent card JSON"** — paste the **JSON content** (not a URL).

Fetch it first if you don't have it already:

```bash
curl https://abc123.ngrok-free.app/.well-known/agent-card.json
```

Paste the full JSON output into the **Agent card JSON** field. Example of what should be pasted:

```json
{
  "name": "OT ADM Agent",
  "description": "Query and manage Opentext SDP work items — defects, stories, features, comments, and personal work lists — using natural language.",
  "version": "1.0.0",
  "url": "https://abc123.ngrok-free.app/message:send",
  "capabilities": {},
  "skills": [
    { "id": "get_defect",          "name": "Get Defect",           "description": "Retrieve a defect by ID" },
    { "id": "get_story",           "name": "Get Story",            "description": "Retrieve a story by ID" },
    { "id": "get_feature",         "name": "Get Feature",          "description": "Retrieve a feature by ID" },
    { "id": "get_comments",        "name": "Get Comments",         "description": "Get all comments for an entity" },
    { "id": "create_comment",      "name": "Create Comment",       "description": "Post a new comment" },
    { "id": "update_comment",      "name": "Update Comment",       "description": "Edit an existing comment" },
    { "id": "fetch_My_Work_Items", "name": "Fetch My Work Items",  "description": "List the current user's work items" }
  ],
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"]
}
```

Click **Preview agent details** to validate, then click **Next**.

---

### Step 6 — Authentication (OAuth)

You will be asked to configure authentication. This step is **optional** — it is only required if the agent needs to access **Google Cloud resources** (e.g. BigQuery) on behalf of users.

**For the OT ADM Agent, which connects to Opentext SDP (not Google Cloud):**

→ Click **Skip & Finish**.

**If you do need OAuth** (e.g. a future agent that accesses GCP resources):

The Client ID, Client Secret, Authorization URI, and Token URI must come from a **Google Cloud OAuth credential** created in APIs & Services → Credentials (not from your ngrok server):

1. In Cloud Console → **APIs & Services → Credentials → Create credentials → OAuth client ID**
2. Application type: **Web application**
3. Authorized redirect URIs — add both:
   - `https://vertexaisearch.cloud.google.com/oauth-redirect`
   - `https://vertexaisearch.cloud.google.com/static/oauth/oauth.html`
4. Click **Create** → download the JSON → use those values in the form.

---

### Step 7 — Confirm registration and use the agent

After clicking **Finish** (or **Skip & Finish**):

 - The **OT ADM Agent** now appears in the Gemini Enterprise web app's `@` popover.
 - Open **[business.gemini.google](https://business.gemini.google)**, type `@` in the chat input, and select **OT ADM Agent**.

```
@OT ADM Agent Get defect 2110
@OT ADM Agent What are my work items?
@OT ADM Agent Show comments on story 55
@OT ADM Agent Add a comment to defect 2110 saying "Under investigation"
@OT ADM Agent Get feature 200 and summarize it
```

Gemini Enterprise forwards the message to your wrapper's `/message:send` endpoint via the A2A protocol. The Gemini agent fetches the data from Opentext SDP and returns a natural-language reply inline in the chat.

> **Multi-turn context:** Gemini Enterprise passes the conversation thread ID as `contextId`, so follow-up questions within the same thread retain full history — exactly like the built-in chat UI.

---

## CI & Test Reports

This project includes a GitHub Actions workflow (`.github/workflows/ci.yml`) that runs the test suite on push and pull requests.

What the workflow does:

- Installs project dependencies from `requirements.txt`.
- Runs `pytest`, producing `reports/junit.xml` and a self-contained HTML report at `reports/report.html`.
- Uploads the `reports/` directory as a workflow artifact named `test-reports`.

How to view CI reports on GitHub:

1. Push your branch to GitHub.
2. Open the repository's **Actions** tab and select the latest CI run.
3. After the run finishes, expand the **Artifacts** section and download `test-reports` — it contains `junit.xml` and `report.html`.

Run the same commands locally to reproduce the CI behavior:

```bash
# activate venv, install deps
pip install -r requirements.txt

# run tests and generate reports
mkdir -p reports
pytest --junitxml=reports/junit.xml --html=reports/report.html --self-contained-html -q

# open the HTML report in your browser
python -m webbrowser reports/report.html
```

If you'd like a pre-commit hook that runs tests before committing, I can add a simple `pre-commit` configuration that runs `pytest` (recommended for small teams). Ask and I will add it.


### Networking & prerequisites checklist

| Check | How to verify |
|---|---|
| Discovery Engine Admin role | Cloud Console → IAM & Admin → IAM → find your account |
| Discovery Engine API enabled | Cloud Console → APIs & Services → search "Discovery Engine API" |
| Wrapper is running locally | `curl http://localhost:9000/health` → `{"status":"ok"}` |
| Public HTTPS URL is reachable | Open `https://<url>/health` in a browser — must load |
| AgentCard returns valid JSON | `curl https://<url>/.well-known/agent-card.json` |
| TLS certificate is valid | No browser cert warning — Gemini Enterprise requires CA-signed HTTPS |
| Gemini Enterprise app exists | Cloud Console → Gemini Enterprise — at least one app listed |

---

<a id="11-project-structure"></a>
## 11. Project Structure

```
a2a-octane-wrapper/
├── main.py                 # FastAPI app — A2A endpoints, agent/fallback routing
├── gemini_agent.py         # Gemini agentic loop + per-session conversation history
├── a2a_models.py           # Pydantic models for the A2A protocol
├── mcp_client.py           # Async HTTP JSON-RPC 2.0 client for Octane MCP
├── tool_router.py          # Tool registry, keyword fallback router, argument extraction
├── config.py               # All configuration loaded from environment variables
├── requirements.txt        # Python dependencies
├── agent-card-static.json  # Static AgentCard reference (for external registration tests)
├── static/
│   ├── index.html          # Built-in chat UI (vanilla HTML / JS)
│   └── OTEX_BIG.svg        # OpenText logo
├── .env                    # Local secrets — NOT committed to source control
└── .env.example            # Template — copy to .env and fill in values
└── env.example             # Template — copy to .env and fill in values
```

---

<a id="12-troubleshooting"></a>
## 12. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `400 — Both application/json and text/event-stream required` | Missing `Accept` header | Fixed in `mcp_client.py` — no action needed |
| `Octane HTTP error: 400` | Wrong workspace IDs or tool name | Check `/tools`; verify `DEFAULT_SHARED_SPACE_ID` / `DEFAULT_WORKSPACE_ID` in `.env` |
| `Agent error: GEMINI_API_KEY is not set` | Missing API key | Add `GEMINI_API_KEY=...` to `.env` |
| Agent falls back to keyword router | `GEMINI_API_KEY` not set or invalid | Set a valid `GEMINI_API_KEY` in `.env` |
| Gemini Enterprise cannot reach the agent | Wrapper not publicly accessible | Use ngrok or deploy to a public URL with valid HTTPS |
| AgentCard not found in Gemini Enterprise | Wrong URL entered or subscription not active | Confirm `GET <url>/.well-known/agent-card.json` returns valid JSON; ensure you have a Gemini Enterprise subscription |
| Agent not in `@` mention list | Not yet registered, or no Gemini Enterprise subscription | Follow Steps 3–5 in [Section 10](#10-connecting-to-google-agentspace); check subscription at [business.gemini.google](https://business.gemini.google) |
| Agent loses context between conversations | Different `contextId` per conversation | Expected — each conversation thread is a separate session |
| `Could not determine a supported tool` | Keyword fallback only (no Gemini key) | Set `GEMINI_API_KEY` or rephrase using tool keywords |
| Timeout errors | Octane server slow or unreachable | Increase `MCP_REQUEST_TIMEOUT_SECONDS`; check `OCTANE_BASE_URL` |
| Gemini Enterprise shows TLS / cert error | Self-signed or expired certificate | Use a valid CA-signed certificate (Let's Encrypt, Google-managed, etc.) |
| ngrok tunnel expires | Free ngrok tunnels reset on restart | Restart ngrok and re-enter the new URL in Gemini Enterprise, or use a paid static domain |
