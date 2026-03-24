# A2A Opentext SDP MCP Wrapper — Design Document

---

## Table of Contents

1. [Functional Design](#functional-design)
2. [Google AgentSpace Integration](#google-agentspace-integration)
3. [Technical Design](#technical-design)

---

## Functional Design

### Purpose

This service allows AI agents and chat interfaces that speak the **Google A2A protocol** to query and manage **Opentext SDP** data through natural language, powered by a Gemini LLM agent. It translates between the A2A protocol, the google-adk function-calling layer, and the Opentext SDP MCP transport.

### Actors

| Actor | Role |
| --- | --- |
| **A2A Client** | Any A2A-compatible agent or UI (Google AgentSpace, the built-in chat UI) that sends natural-language messages |
| **A2A Wrapper** | This service — receives A2A messages, runs them through the Gemini ADK agent, executes Opentext SDP tool calls, and returns A2A Task responses |
| **Gemini ADK Agent** | `LlmAgent` + `Runner` inside the wrapper that decides which tools to call and synthesizes the final answer; per-session history managed by `InMemorySessionService` |
| **Opentext SDP MCP Server** | Backend ALM system exposing domain tools over the Model Context Protocol (Streamable HTTP transport) |
| **Opentext SDP** | The underlying ALM data store (defects, stories, features, comments, etc.) |

### Functional Flows

#### Flow 1 — Gemini Agent: Get a defect

```text
User: "Get defect 1314"


Gemini ADK agent receives user message
Gemini selects tool: get_defect(entityId=1314)

   MCP Streamable HTTP: tools/call  name=get_defect  arguments={entityId:1314, sharedSpaceId:..., workSpaceId:...}


Opentext SDP returns defect JSON


Gemini synthesizes natural-language summary


A2A Task (COMPLETED)  status.message = Gemini summary, artifacts = raw data
task.metadata = {mcp_called: true, auth_injected: true/false}
```

#### Flow 2 — Gemini Agent: Fetch my work items

```text
User: "What are my work items?"


Gemini selects tool: fetch_My_Work_Items()

   MCP Streamable HTTP: tools/call  name=fetch_My_Work_Items


Opentext SDP returns list of assigned items


Gemini summarizes: lists each item type, ID, name, phase
A2A Task (COMPLETED)
```

#### Flow 3 — Gemini Agent: Multi-step (get defect then add comment)

```text
User: "Get defect 1314 and add a comment saying 'Reproduced on build 5.3'"


Gemini ADK Runner drives multi-step function-calling loop automatically:
   Round 1 → get_defect(entityId=1314)
   Round 2 → create_comment(entityId=1314, entityType="defect", text="Reproduced on build 5.3")


Gemini confirms both actions in final summary
A2A Task (COMPLETED)
```

#### Flow 4 — Auto-generated comment text

```text
User: "Add a funny comment to defect 1314"


_maybe_inject_generated_text() detects "funny" trigger word

   Separate Gemini call: generate a witty short comment based on context
    Returns e.g. "This bug is so slippery it should have its own LinkedIn profile."


Rewritten message passed to agentic loop:
   "Add a funny comment to defect 1314. Use exactly this text: '...'"


Gemini calls create_comment with the generated text
A2A Task (COMPLETED)
```

#### Flow 5 — Local-only tool: Tell a joke

```text
User: "Tell me a joke about defects"


Gemini selects tool: tell_joke(topic="defects")

   No MCP call — joke generated locally via direct Gemini call


A2A Task (COMPLETED)
task.metadata = {mcp_called: false, auth_injected: true/false}
```

#### Flow 6 — Keyword fallback (no Gemini API key)

```text
User: "Get defect 1314"


Keyword scorer resolves intent → get_defect
Regex extracts entityId = 1314

   MCP Streamable HTTP: tools/call  name=get_defect


Raw Opentext SDP result wrapped in A2A Artifact
A2A Task (COMPLETED)  status.message = "Successfully executed get_defect"
task.metadata = {mcp_called: true, auth_injected: true/false}
```

#### Flow 7 — Error handling

```text
Gemini calls tool → Opentext SDP returns HTTP 400 or MCP isError


Error string fed back to Gemini as tool function return value
Gemini explains the error to the user in natural language
A2A Task (COMPLETED or FAILED depending on severity)
```

### Supported Tools

| Tool | Description | Required arguments | Local only |
| --- | --- | --- | --- |
| `get_defect` | Fetch a single defect by ID | `entityId` | No |
| `get_story` | Fetch a single user story by ID | `entityId` | No |
| `get_feature` | Fetch a single feature by ID | `entityId` | No |
| `get_comments` | Get all comments for an entity | `entityId`, `entityType` | No |
| `create_comment` | Post a new comment on a work item | `entityId`, `entityType`, `text` | No |
| `update_comment` | Edit an existing comment | `commentId`, `entityId`, `entityType`, `text` | No |
| `fetch_My_Work_Items` | List the current user's assigned items | _(none beyond injected context)_ | No |
| `tell_joke` | Generate and return a developer joke | `topic` _(optional)_ | **Yes** |

`sharedSpaceId` and `workSpaceId` are injected automatically from config into every MCP call — Gemini never supplies them.

Local-only tools (`tell_joke`) are implemented inside the wrapper and never sent to the MCP server. They are preserved across MCP tool-discovery refreshes.

### Keyword Fallback Intent Rules (no Gemini API key)

| Priority | Keywords | Tool |
| --- | --- | --- |
| 1 | `my work`, `my items`, `my defects`, `fetch my`, `assigned to me` | `fetch_My_Work_Items` |
| 2 | `update comment`, `edit comment`, `change comment`, `modify comment` | `update_comment` |
| 3 | `add comment`, `create comment`, `post comment`, `comment saying` | `create_comment` |
| 4 | `comments`, `discussion`, `thread`, `feedback`, `notes` | `get_comments` |
| 5 | `defect`, `bug` | `get_defect` |
| 6 | `story`, `user story` | `get_story` |
| 7 | `feature` | `get_feature` |

---

## Google AgentSpace Integration

### What is AgentSpace?

**Google AgentSpace** (`vertexaisearch.cloud.google.com`) is Google's enterprise AI assistant platform. It supports **external A2A agents** that users can invoke with `@AgentName` directly from the chat interface. When a user types `@` in the chat input, a popover appears listing all available agents — built-in ones (like "Deep Research") and any registered external agents (like the OT ADM Agent).

```text
┌─────────────────────────────────────────────────┐
│  Google Agentspace   Hello, Scott               │
│  ─────────────────────────────────────────────  │
│                                                 │
│           Agents                                │
│  ┌──────────────────────────────────────────┐   │
│  │ ● Content Aviator Agent                  │   │
│  │   AI-powered document analysis an…       │   │
│  │ ● Deep Research                          │   │
│  │   Get in-depth answers grounded in …     │   │
│  │ ● OT ADM Agent                           │   │
│  │   Query and manage Opentext SDP…         │   │
│  └──────────────────────────────────────────┘   │
│                                                 │
│  [ @_________________________________________ ] │
│                                                 │
│  Take action  Analyze data  Write code  …       │
└─────────────────────────────────────────────────┘
```

After registering the OT ADM Agent, users can type `@OT ADM Agent Get defect 1314` to query Opentext SDP directly from AgentSpace.

### A2A Protocol Primer

The A2A protocol defines:

- **AgentCard** — A JSON document at `GET /.well-known/agent-card.json` describing the agent's identity, capabilities, skills, and security schemes
- **SendMessage (HTTP+JSON)** — `POST /message:send` with a `SendMessageRequest` body
- **SendMessage (JSON-RPC 2.0)** — `POST /` with a JSON-RPC envelope, method `message/send` (Gemini Enterprise default binding)
- **Task** — The response envelope: `{ task: { id, contextId, status: { state, message }, artifacts, metadata } }`
- **contextId** — A persistent session identifier; AgentSpace passes the conversation thread ID enabling multi-turn memory

### Registration Flow

```text
AgentSpace (cloud)                A2A Opentext SDP Wrapper (your server)
       │                                       │
       │── GET /.well-known/agent-card.json ──►│
       │◄── AgentCard JSON ────────────────────│
       │   { name, url, preferredTransport,    │
       │     protocolVersion, securitySchemes, │
       │     skills, ... }                     │
       │                                       │
       │  (user types @OT ADM Agent ...)        │
       │                                       │
       │── POST / (JSON-RPC 2.0) ─────────────►│
       │   { jsonrpc: "2.0",                   │  ADK agentic loop
       │     method: "message/send",           │  ──► MCP Streamable HTTP
       │     params: {                         │  ──► Gemini summary
       │       message: {                      │
       │         contextId: "conv-abc",        │
       │         parts: [{text: "Get defect…"}]│
       │       }}}                             │
       │                                       │
       │◄── { jsonrpc: "2.0", id: ...,        │
       │      result: { task: {               │
       │        status: { message: {...} },    │
       │        artifacts: [...],             │
       │        metadata: { mcp_called, ... } │
       │      }}} ────────────────────────────│
       │                                       │
```

### Network Requirements

AgentSpace is a cloud service — your wrapper **must be reachable via public HTTPS**:

| Scenario | Solution |
| --- | --- |
| Local development | `ngrok http 9000` — creates a public HTTPS tunnel |
| Staging / production | Cloud Run, App Engine, EC2/VM behind nginx with TLS |
| Enterprise on-prem | API Gateway or DMZ reverse proxy with a valid certificate |

### Step-by-Step: Register the Opentext SDP Agent in AgentSpace

#### Step 1 — Make the wrapper publicly reachable

```bash
ngrok http 9000
# Note the HTTPS URL, e.g. https://abc123.ngrok-free.app
```

#### Step 2 — Verify the AgentCard

```bash
curl https://<your-public-url>/.well-known/agent-card.json
curl https://<your-public-url>/health
# → {"status": "ok", "version": "0.1.0"}
```

#### Step 3 — Open AgentSpace and type `@`

1. Open AgentSpace at `https://vertexaisearch.cloud.google.com`
2. Type `@` — the Agents popover appears
3. If "OT ADM Agent" is not yet in the list, look for **"Connect an agent"** or **"Add external agent"**

#### Step 4 — Enter the agent URL

Enter your wrapper's public base URL (without any path). AgentSpace fetches `/.well-known/agent-card.json` automatically.

#### Step 5 — Invoke the OT ADM Agent

```text
@OT ADM Agent Get defect 1314
@OT ADM Agent What are my work items?
@OT ADM Agent Add a comment to defect 1314 saying "Fixed in 5.3"
@OT ADM Agent Show comments on story 55
@OT ADM Agent Tell me a joke about bugs
```

**Multi-turn context:** AgentSpace passes the conversation thread ID as `contextId`. Follow-up questions within the same conversation retain full context via `InMemorySessionService`.

---

## Technical Design

### Technology Stack

| Component | Technology |
| --- | --- |
| Runtime | Python 3.11+ |
| Web framework | FastAPI 0.115 |
| ASGI server | Uvicorn (with `watchfiles` hot-reload in dev) |
| HTTP client | HTTPX (async, HTTP/2) |
| Data validation | Pydantic v2 |
| LLM / Agent | Google ADK (`google-adk`) — `LlmAgent`, `Runner`, `InMemorySessionService` |
| LLM API | `google-genai` SDK v1+ (used for direct Gemini calls: text pre-generation, jokes) |
| MCP transport | `mcp` Python SDK ≥ 1.5.0 — Streamable HTTP transport (`ClientSession`) |
| Configuration | `python-dotenv` + environment variables |
| Frontend | Vanilla HTML/JS (static, no framework) |

### Architecture

```text
                   FastAPI App (main.py)
  ─────────────────────────────────────────────────────────────
  GET  /.well-known/agent-card.json   →  AgentCard discovery
  POST /                              →  A2A JSON-RPC 2.0 binding
  POST /message:send                  →  A2A HTTP+JSON binding
  GET  /health                        →  Liveness check
  GET  /tools                         →  Proxy to MCP tools/list
  GET  /config  POST /config          →  Runtime config (auth-gated)
  POST /discover-tools                →  Manual MCP re-discovery (auth-gated)
  GET  /sim/token                     →  Token sim for Chat UI (auth-gated)
  GET  /auth-test                     →  OAuth2 flow test UI
  GET  /readme  GET /raw-readme       →  Rendered README viewer
  GET  /                              →  Built-in Chat UI
  ─────────────────────────────────────────────────────────────

                GEMINI_API_KEY set?
               Yes ↓                    No ↓
          ┌─────────────┐         ┌─────────────────┐
          │ GeminiAgent │         │   tool_router   │
          │ (ADK Runner)│         │ resolve_intent  │
          │  run_async  │         │ extract_args    │
          └──────┬──────┘         └────────┬────────┘
                 │ typed async fn calls      │
                 └───────────┬───────────────┘
                             │ execute_tool()
                      ┌──────▼──────┐
                      │  mcp_client │
                      │ call_tool() │
                      │ list_tools()│
                      └──────┬──────┘
                             │ Streamable HTTP + Bearer token
                    ┌────────▼────────┐
                    │ Opentext SDP    │
                    │ MCP Server      │
                    │ POST /mcp       │
                    └─────────────────┘
```

### Module Responsibilities

#### `main.py`

Bootstraps the FastAPI application and owns all HTTP endpoints.

- **Startup** — calls `mcp.list_tools()` to populate `TOOL_REGISTRY` via `populate_registry_from_mcp()`; initialises `GeminiAgent` if `GEMINI_API_KEY` is set; optionally starts a periodic background task (`MCP_TOOL_POLL_INTERVAL_SECONDS`) that refreshes the tool registry and agent.
- **Auth** — `_verify_token()` dependency gates admin endpoints against `A2A_API_KEY`; `_extract_bearer_token()` extracts inbound Bearer tokens for downstream passthrough.
- **Token resolution** — `send_message` and `_jsonrpc_message_send` determine which token to forward to the MCP server:
  - No `A2A_API_KEY` set → always use server's `API_KEY`
  - `A2A_API_KEY` set and inbound token matches it → substitute with server's `API_KEY` (Chat UI / demo mode)
  - `A2A_API_KEY` set and inbound token differs → pass through as-is (real OAuth token from AgentSpace)
- **`_handle_with_agent()`** — calls `agent.run(user_text, mcp, context_id, bearer_token)`, wraps the `(summary, artifacts, mcp_called)` 3-tuple into an A2A `Task` with `metadata.mcp_called` and `metadata.auth_injected`.
- **`_handle_with_keywords()`** — legacy keyword path; returns `Task` with `metadata.mcp_called=True`.
- **`_build_agent_card()`** — constructs `AgentCard` from `TOOL_REGISTRY` dynamically; advertises the `csai_oauth` security scheme (Authorization Code + PKCE, Client Credentials) with OTDS token URLs.

#### `gemini_agent.py`

ADK-powered agent with per-session conversation history.

| Component | Purpose |
| --- | --- |
| `_SYSTEM_PROMPT` | Instructs Gemini on how to use tools, resolve entity references from history, draft comment text, and call `tell_joke` |
| `_GENERATE_TEXT_TRIGGERS` | Regex detecting open-ended text requests (`funny`, `invent`, `make up`, etc.) |
| `_build_tools(mcp, artifacts, bearer_token, mcp_called_flag)` | Factory that returns 8 typed async functions (one per tool). ADK infers Gemini `FunctionDeclaration` schemas from Python type annotations. Functions close over `mcp`, `artifacts`, `bearer_token`, and `mcp_called_flag`. |
| `_invoke(tool_name, arguments, ...)` | Shared helper: calls `execute_tool()`, appends the `Artifact`, sets `mcp_called_flag[0] = True`, returns result text for Gemini |
| `GeminiAgent.__init__` | Creates `InMemorySessionService`; allocates shared `_run_artifacts` list and `_run_mcp_called` flag (never reassigned — closures hold references) |
| `GeminiAgent._rebuild_runner(mcp, bearer_token)` | Constructs a fresh `LlmAgent` + `Runner` bound to the current `mcp` and `bearer_token`; called at the start of every `run()` turn so the bearer token is always current |
| `GeminiAgent.refresh_tools(mcp)` | Rebuilds the runner; verifies MCP connectivity via `list_tools()`; called at startup and after `/config` changes |
| `GeminiAgent.run(user_text, mcp, context_id, bearer_token)` | One full agentic turn: resets per-run state, injects generated text if needed, creates session if absent, streams events from `runner.run_async()`, returns `(summary, artifacts, mcp_called)` |
| `_maybe_inject_generated_text()` | Detects open-ended text requests; calls Gemini directly (wrapped in `asyncio.wait_for` + `asyncio.to_thread`) to pre-generate comment text; splices it into the user message |
| `_generate_joke(topic)` | Local joke generation via direct Gemini call; used by the `tell_joke` tool function |

The `InMemorySessionService` manages per-session conversation history automatically (keyed by `session_id = context_id`). Sessions are explicitly created if they don't exist (required by newer ADK versions that removed auto-creation from `Runner`).

There is no manual `MAX_TOOL_ROUNDS` loop — the ADK `Runner` drives the multi-step function-calling exchange until Gemini returns a final text response.

#### `a2a_models.py`

Pydantic v2 models implementing both A2A protocol bindings.

| Model | Purpose |
| --- | --- |
| `Role` | Enum: `ROLE_USER`, `ROLE_AGENT` |
| `TaskState` | Enum: `COMPLETED`, `FAILED`, `REJECTED`, `SUBMITTED`, `WORKING`, `CANCELED`, `INPUT_REQUIRED`, `AUTH_REQUIRED` |
| `Part` | Single content unit: `text`, `data`, `mediaType`, `metadata` |
| `Message` | List of `Part`s with `role`, `contextId`, `taskId`, `messageId` |
| `SendMessageConfiguration` | Optional `acceptedOutputModes`, `blocking`, `historyLength` |
| `SendMessageRequest` | Inbound POST body: `message`, `configuration`, `metadata` |
| `TaskStatus` | `state`, `message`, `timestamp` |
| `Artifact` | Tool result payload: `artifactId`, `name`, `description`, `parts`, `metadata` |
| `Task` | Response entity: `id`, `contextId`, `status`, `artifacts`, `history`, `metadata` |
| `TaskResponse` | Top-level HTTP+JSON response envelope: `{ task }` |
| `JsonRpcError` | JSON-RPC 2.0 error: `code`, `message`, `data` |
| `JsonRpcResponse` | JSON-RPC 2.0 response envelope: `jsonrpc`, `id`, `result`, `error` |
| `AgentSkill` | Skill definition: `id`, `name`, `description`, `tags`, `examples` |
| `AgentCapabilities` | `streaming`, `pushNotifications` |
| `AgentProvider` | `organization`, `url` |
| `ClientCredentialsFlow` | OAuth2 client-credentials: `tokenUrl`, `scopes` |
| `AuthorizationCodeFlow` | OAuth2 auth-code + PKCE: `authorizationUrl`, `tokenUrl`, `scopes`, `pkce`, `pkceMethod` |
| `OAuthFlows` | Container: `clientCredentials`, `authorizationCode` |
| `SecurityScheme` | OpenAPI 3.0 security scheme: `type`, `scheme`, `flows` |
| `AgentCard` | Discovery metadata: `name`, `url`, `preferredTransport`, `protocolVersion`, `supportsAuthenticatedExtendedCard`, `securitySchemes`, `security`, `skills`, `capabilities` |

#### `mcp_client.py`

Async MCP client using the official `mcp` Python SDK with Streamable HTTP transport.

| Feature | Detail |
| --- | --- |
| Transport | `streamable_http_client` (preferred, MCP SDK ≥ 1.6) or `streamablehttp_client` (older SDK) — detected at import time |
| Session pattern | Short-lived `ClientSession` per call: `initialize()` → `call_tool()` / `list_tools()` → close |
| `terminate_on_close=False` | Keeps the underlying HTTP connection alive for reuse |
| HTTP client | Persistent `httpx.AsyncClient` with HTTP/2 enabled (falls back to `create_mcp_http_client` then `None`) |
| Parameter compatibility | Tries `http_client=`, then `client=`, then no keyword — handles API changes across SDK versions |
| Auth | `Authorization: Bearer <API_KEY>` injected as header; per-call `bearer_token` override supported |
| Context injection | `sharedSpaceId` and `workSpaceId` merged into `arguments` on every `call_tool()` |
| Error handling | Checks `result.isError`; raises `OctaneMcpError(code, message, data)` on server-side errors |
| Timing | Logs `MCP call_tool duration=` and `MCP list_tools duration=` for every call |

#### `tool_router.py`

| Component | Purpose |
| --- | --- |
| `TOOL_REGISTRY` | Dict of tool definitions (description, example_prompts, default_arguments, required, `_local_only` flag) |
| `_LOCAL_ONLY_TOOLS` | Frozenset `{"tell_joke"}` — preserved across MCP discovery refreshes |
| `_EXCLUDED_MCP_PARAMS` | Frozenset `{"sharedSpaceId", "workSpaceId"}` — stripped from MCP-discovered schemas |
| `populate_registry_from_mcp(tools)` | Replaces `TOOL_REGISTRY` with live MCP tool definitions; re-merges local-only tools; no-ops if list is empty |
| `resolve_intent(text)` | Priority-ordered keyword scorer; also handles JSON payload with explicit `"tool"` key |
| `extract_arguments(tool, message)` | NL extraction of `entityId`, `entityType`, `commentId`, comment `text`; structured `data` parts take priority |
| `execute_tool(tool, args, mcp, bearer_token)` | Shared by both paths; calls `mcp.call_tool()`, converts MCP content blocks to A2A `Part`/`Artifact` |

#### `config.py`

| Variable | Default | Purpose |
| --- | --- | --- |
| `OCTANE_BASE_URL` | `http://localhost:8080` | Opentext SDP base URL |
| `OCTANE_MCP_ENDPOINT` | `{OCTANE_BASE_URL}/mcp` | Derived MCP endpoint |
| `API_KEY` | `""` | Opentext SDP bearer token |
| `DEFAULT_SHARED_SPACE_ID` | `1001` | Injected into every MCP call |
| `DEFAULT_WORKSPACE_ID` | `1002` | Injected into every MCP call |
| `GEMINI_API_KEY` | `""` | Google AI API key |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model name |
| `GEMINI_ENABLED` | `True` | Runtime toggle (via `/config`) |
| `A2A_HOST` | `0.0.0.0` | Bind address |
| `A2A_PORT` | `9000` | Listen port |
| `AGENT_VERSION` | `0.1.0` | Reported in AgentCard and `/health` |
| `A2A_API_KEY` | `""` | Inbound bearer token for admin endpoints; empty disables auth |
| `AGENT_URL` | `http://localhost:9000` | Public URL advertised in AgentCard |
| `AGENT_NAME` | `ADM Agent` | Agent name in AgentCard |
| `OAUTH2_AUTH_URL` | OTDS dev endpoint | OAuth2 authorization URL |
| `OAUTH2_TOKEN_URL` | OTDS dev endpoint | OAuth2 token URL |
| `MCP_TOOL_POLL_INTERVAL_SECONDS` | `86400` | Periodic MCP re-discovery interval; `0` disables |
| `MCP_REQUEST_TIMEOUT_SECONDS` | `10` | HTTPX timeout for MCP calls |
| `GEMINI_REQUEST_TIMEOUT_SECONDS` | `10` | `asyncio.wait_for` timeout on direct Gemini calls |

### Data Flow — `/message:send` (Gemini agent path)

```text
POST /message:send  (HTTP+JSON)
  or
POST /  method="message/send"  (JSON-RPC 2.0)

   ↓ Pydantic validation: SendMessageRequest
   ↓ Extract user_text, context_id, task_id
   ↓ Resolve octane_token (passthrough or substitute)

agent.run(user_text, mcp, context_id, bearer_token)

   _rebuild_runner(mcp, bearer_token)
     → fresh LlmAgent + Runner with new tool closures

   _run_artifacts.clear()  /  _run_mcp_called[0] = False

   _maybe_inject_generated_text(user_text)
     if trigger regex matches:
       → asyncio.wait_for(asyncio.to_thread(genai.generate_content))
       → splice generated text into message

   Ensure session exists in InMemorySessionService

   runner.run_async(user_id, session_id, new_message) → event stream:
     [Event] tool call → _invoke() → execute_tool() → mcp.call_tool()
                         append Artifact, set mcp_called_flag[0]=True
                         return text result to ADK
     [Event] ... repeat (ADK drives the loop, no MAX_TOOL_ROUNDS)
     [Event is_final_response] → extract summary text

   return (summary, list(artifacts), mcp_called_flag[0])

   ↓

Task(COMPLETED)
  status.message.parts[0].text = summary
  artifacts = [all collected Artifacts]
  metadata = {mcp_called: bool, auth_injected: bool}
```

### Data Flow — `/message:send` (Keyword fallback path)

```text
POST /message:send  (no GEMINI_API_KEY)

   ↓ Extract user_text, context_id, task_id
   ↓ Resolve octane_token

resolve_intent(user_text) → tool_name (or None)

If None:
   Check message.parts for structured {tool: ...} data
If still None:
   Task(REJECTED) + supported tool list

extract_arguments(tool_name, message) → arguments dict

execute_tool(tool_name, arguments, mcp, bearer_token) → Artifact

Task(COMPLETED)
  status.message = "Successfully executed {tool_name}"
  artifacts = [Artifact]
  metadata = {mcp_called: true, auth_injected: bool}
```

### MCP Streamable HTTP Session Pattern

```text
OctaneMcpClient.call_tool(tool_name, arguments)

   Merge sharedSpaceId, workSpaceId into arguments
   Build per-call headers (override Authorization if bearer_token given)

   async with streamable_http_client(url, http_client=..., terminate_on_close=False) as (read, write, _):
       async with ClientSession(read, write) as session:
           await session.initialize()          # MCP initialize handshake
           result = await session.call_tool(tool_name, arguments)

   Check result.isError → raise OctaneMcpError
   Return {"content": [{type, text}, ...]}
```

The persistent `httpx.AsyncClient` (HTTP/2 enabled) is shared across multiple `ClientSession` lifetimes, enabling connection reuse and keep-alive without re-negotiating TLS for every call.

### AgentCard Schema

```json
{
  "name": "ADM Agent",
  "description": "...",
  "version": "0.1.0",
  "url": "https://<agent-host>",
  "preferredTransport": "JSONRPC",
  "protocolVersion": "0.3.0",
  "supportsAuthenticatedExtendedCard": true,
  "provider": { "organization": "OpenText", "url": "https://opentext.com" },
  "capabilities": { "streaming": false, "pushNotifications": false },
  "securitySchemes": {
    "csai_oauth": {
      "type": "oauth2",
      "flows": {
        "clientCredentials": {
          "tokenUrl": "<OAUTH2_TOKEN_URL>",
          "scopes": { "otds:groups": "...", "otds:roles": "...", "search": "..." }
        },
        "authorizationCode": {
          "authorizationUrl": "<OAUTH2_AUTH_URL>",
          "tokenUrl": "<OAUTH2_TOKEN_URL>",
          "scopes": { "otds:groups": "...", "otds:roles": "...", "search": "..." },
          "pkce": true,
          "pkceMethod": "S256"
        }
      }
    }
  },
  "security": [{ "csai_oauth": ["otds:groups", "otds:roles", "search"] }],
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [ ... one entry per TOOL_REGISTRY entry ... ]
}
```

### Task Response Schema

```json
{
  "task": {
    "id": "<uuid>",
    "contextId": "<session-id>",
    "status": {
      "state": "TASK_STATE_COMPLETED",
      "message": {
        "messageId": "<uuid>",
        "role": "ROLE_AGENT",
        "parts": [{ "text": "Defect 1314: ..." }]
      },
      "timestamp": "2026-03-24T..."
    },
    "artifacts": [
      {
        "artifactId": "<uuid>",
        "name": "get_defect_result",
        "description": "Result from Opentext SDP tool: get_defect",
        "parts": [
          { "data": { ... }, "mediaType": "application/json" }
        ]
      }
    ],
    "metadata": {
      "mcp_called": true,
      "auth_injected": true
    }
  }
}
```

`metadata.mcp_called` is `true` when one or more MCP tool calls were made. `metadata.auth_injected` is `true` when a bearer token was forwarded to the MCP server. The Chat UI uses these flags to render the auth/MCP trace correctly — it does not infer activity from the presence of artifacts.

### Error Handling Matrix

| Error type | Source | Gemini agent path | Keyword fallback path |
| --- | --- | --- | --- |
| Gemini API failure | Gemini | `Task(FAILED)` with error detail | N/A |
| ADK session error | ADK | `Task(FAILED)` with error detail | N/A |
| Unknown intent | Wrapper | N/A (Gemini decides) | `Task(REJECTED)` |
| Argument parse failure | Wrapper | N/A (Gemini provides args) | `Task(FAILED)` |
| HTTP 4xx/5xx from Opentext SDP | HTTPX | Error string returned to Gemini | `Task(FAILED)` + body |
| Timeout (MCP call) | HTTPX | Error string returned to Gemini | `Task(FAILED)` |
| Timeout (Gemini call) | asyncio | Falls back to original user_text | N/A |
| MCP `isError` result | Opentext SDP | Error string returned to Gemini | `Task(FAILED)` |
| JSON-RPC parse error | Wrapper | N/A (HTTP binding) | `{"jsonrpc":"2.0","error":{"code":-32700}}` |
| JSON-RPC invalid request | Wrapper | N/A (HTTP binding) | `{"jsonrpc":"2.0","error":{"code":-32600}}` |
| JSON-RPC unimplemented method | Wrapper | N/A (HTTP binding) | `{"jsonrpc":"2.0","error":{"code":-32601}}` |

### Security

- **Opentext SDP authentication**: Bearer token (`Authorization: Bearer <API_KEY>`), passed through from inbound request or substituted from `config.API_KEY`
- **Inbound admin auth**: `A2A_API_KEY` guards `/config`, `/sim/token`, `/discover-tools`; if unset, those endpoints are open (suitable for local/trusted networks)
- **Inbound A2A requests**: `/message:send` and `POST /` accept any caller without authentication (by design — Gemini Enterprise handles caller auth upstream)
- **Gemini API key**: passed to `genai.Client(api_key=...)` at call time; never logged
- **All secrets** loaded from environment variables / `.env` file, never hardcoded
- **AgentCard OAuth2**: advertises OTDS Authorization Code (PKCE/S256) and Client Credentials flows for A2A clients that need to obtain tokens before calling

### Known Limitations

- **No streaming** — A2A streaming and SSE push notifications are not implemented (`capabilities.streaming = false`)
- **Sync Gemini SDK** — `google-genai` v1 has no native async client; direct calls (text pre-generation, jokes) run via `asyncio.to_thread` with `asyncio.wait_for` timeout
- **In-memory session history** — `InMemorySessionService` stores conversation history in-process; lost on server restart; a persistent session store (Redis, database) would be needed for production
- **Single workspace** — `sharedSpaceId` and `workSpaceId` are global defaults, not per-user; override via `/config` or environment variables
- **Keyword fallback is limited** — complex or ambiguous prompts may not resolve correctly without a Gemini API key
- **No task persistence** — `tasks/get` and `tasks/cancel` JSON-RPC methods return `method not found`; tasks are fire-and-forget
- **Bearer token per-turn rebuild** — the ADK `Runner` is rebuilt on every `run()` call so tool closures capture the current `bearer_token`; this is correct but adds minor overhead
