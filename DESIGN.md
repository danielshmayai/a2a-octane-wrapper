# A2A Opentext SDP MCP Wrapper Design Document

---

## Table of Contents

1. [Functional Design](#functional-design)
2. [Google AgentSpace Integration](#google-agentspace-integration)
3. [Technical Design](#technical-design)

---

## Functional Design

### Purpose

This service allows AI agents and chat interfaces that speak the **Google A2A protocol** to query and manage **Opentext SDP** data through natural language, powered by a Gemini LLM agent. It translates between the A2A protocol, the Gemini function-calling layer, and the Opentext SDP MCP transport.

### Actors

| Actor | Role |
|---|---|
| **A2A Client** | Any A2A-compatible agent or UI (Google AgentSpace, the built-in chat UI) that sends natural-language messages |
| **A2A Wrapper** | This service  receives A2A messages, runs them through the Gemini agent, executes Opentext SDP tool calls, and returns A2A Task responses |
| **Gemini Agent** | LLM function-calling agent inside the wrapper that decides which Opentext SDP tools to call and synthesizes the final answer |
| **Opentext SDP MCP Server** | Backend ALM system exposing domain tools over the Model Context Protocol |
| **Opentext SDP** | The underlying ALM data store (defects, stories, features, comments, etc.) |

### Functional Flows

#### Flow 1  Gemini Agent: Get a defect

```
User: "Get defect 2110"
  
  
Gemini agent receives user message
Gemini selects tool: get_defect(entityId=2110)
  
   MCP call: tools/call  name=get_defect  arguments={entityId:2110, sharedSpaceId:..., workSpaceId:...}
  
  
Opentext SDP returns defect JSON
  
  
Gemini synthesizes natural-language summary
  
  
A2A Task (COMPLETED)  status.message = Gemini summary, artifacts = raw data
```

#### Flow 2  Gemini Agent: Fetch my work items

```
User: "What are my work items?"
  
  
Gemini selects tool: fetch_My_Work_Items()
  
   MCP call: tools/call  name=fetch_My_Work_Items
  
  
Opentext SDP returns list of assigned items
  
  
Gemini summarizes: lists each item type, ID, name, phase
A2A Task (COMPLETED)
```

#### Flow 3  Gemini Agent: Multi-step (get defect then add comment)

```
User: "Get defect 2110 and add a comment saying 'Reproduced on build 5.3'"
  
  
Gemini selects two tools in sequence:
  Round 1  get_defect(entityId=2110)
  Round 2  create_comment(entityId=2110, entityType="defect", text="Reproduced on build 5.3")
  
  
Gemini confirms both actions in final summary
A2A Task (COMPLETED)
```

#### Flow 4  Auto-generated comment text

```
User: "Add a funny comment to defect 2110"
  
  
_maybe_inject_generated_text() detects "funny" trigger word
  
   Separate Gemini call: generate a witty short comment based on conversation history
    Returns e.g. "This bug is so slippery it should have its own LinkedIn profile."
  
  
Rewritten message passed to agentic loop:
  "Add a funny comment to defect 2110. Use exactly this text: '...'"
  
  
Gemini calls create_comment with the generated text
A2A Task (COMPLETED)
```

#### Flow 5  Keyword fallback (no Gemini API key)

```
User: "Get defect 2110"
  
  
Keyword scorer resolves intent  get_defect
Regex extracts entityId = 2110
  
   MCP call: tools/call  name=get_defect
  
  
Raw Opentext SDP result wrapped in A2A Artifact
A2A Task (COMPLETED)  status.message = "Successfully executed get_defect"
```

#### Flow 6  Error handling

```
Gemini calls tool  Opentext SDP returns HTTP 400 or JSON-RPC error
  
  
Error string fed back to Gemini as FunctionResponse
Gemini explains the error to the user in natural language
A2A Task (COMPLETED or FAILED depending on severity)
```

### Supported Tools

| Tool | Description | Required arguments |
|---|---|---|
| `get_defect` | Fetch a single defect by ID | `entityId` |
| `get_story` | Fetch a single user story by ID | `entityId` |
| `get_feature` | Fetch a single feature by ID | `entityId` |
| `get_comments` | Get all comments for an entity | `entityId`, `entityType` |
| `create_comment` | Post a new comment on a work item | `entityId`, `entityType`, `text` |
| `update_comment` | Edit an existing comment | `commentId`, `entityId`, `entityType`, `text` |
| `fetch_My_Work_Items` | List the current user's assigned items | _(none beyond injected context)_ |

`sharedSpaceId` and `workSpaceId` are injected automatically from config into every MCP call  Gemini never needs to supply them.

### Keyword Fallback Intent Rules (no Gemini API key)

| Priority | Keywords | Tool |
|---|---|---|
| 1 | `my work`, `my items`, `my defects`, `my backlog`, `assigned to me` | `fetch_My_Work_Items` |
| 2 | `update comment`, `edit comment`, `modify comment` | `update_comment` |
| 3 | `add comment`, `create comment`, `post comment`, `comment saying` | `create_comment` |
| 4 | `comments`, `discussion`, `thread`, `feedback` | `get_comments` |
| 5 | `defect`, `bug` | `get_defect` |
| 6 | `story`, `user story` | `get_story` |
| 7 | `feature` | `get_feature` |

---

## Google AgentSpace Integration

### What is AgentSpace?

**Google AgentSpace** (`vertexaisearch.cloud.google.com`) is Google's enterprise AI assistant platform. It supports **external A2A agents** that users can invoke with `@AgentName` directly from the chat interface. When a user types `@` in the chat input, a popover appears listing all available agents — built-in ones (like "Deep Research") and any registered external agents (like the OT ADM Agent).

The screenshot below shows what this looks like in practice:

```
┌─────────────────────────────────────────────────┐
│  Google Agentspace   Hello, Scott               │
│  ─────────────────────────────────────────────  │
│                                                 │
│           Agents                                │
│  ┌──────────────────────────────────────────┐   │
│  │ ● Content Aviator Agent                  │   │
│  │   AI-powered document analysis an…       │   │
│  │ ● Content Aviator Agent v2               │   │
│  │   AI-powered document analysis an…       │   │
│  │ ● Deep Research                          │   │
│  │   Get in-depth answers grounded in …     │   │
│  └──────────────────────────────────────────┘   │
│                                                 │
│  [ @_________________________________________ ] │
│                                                 │
│  Take action  Analyze data  Write code  …       │
└─────────────────────────────────────────────────┘
```

After registering the OT ADM Agent, it appears in this list and users can type `@OT ADM Agent Get defect 2110` to query Opentext SDP directly from AgentSpace.

### A2A Protocol Primer

The A2A protocol defines:

- **AgentCard** — A JSON document at `GET /.well-known/agent-card.json` describing the agent's identity, capabilities, and skills
- **SendMessage** — `POST /message:send` with a `SendMessageRequest` body containing a `Message` (with `parts`, `contextId`, `messageId`)
- **Task** — The response envelope: `{ task: { id, contextId, status: { state, message }, artifacts } }`
- **contextId** — A persistent session identifier. AgentSpace passes the conversation thread ID as `contextId`, enabling multi-turn memory across messages in the same conversation

### Registration Flow

```
AgentSpace (cloud)                A2A Opentext SDP Wrapper (your server)
       │                                       │
       │── GET /.well-known/agent-card.json ──►│
       │◄── AgentCard JSON ────────────────────│
       │                                       │
      │  (user types @OT ADM Agent ...)   │
       │                                       │
       │── POST /message:send ────────────────►│
       │   { message: {                        │
       │       contextId: "conv-abc",          │  Gemini agentic loop
       │       parts: [{text: "Get defect…"}]  │  ──► MCP tool calls
       │     }}                                │  ──► Gemini summary
       │                                       │
       │◄── { task: { status: {               │
       │       message: {parts:[{text:…}]},    │
       │       artifacts: [...]                │
       │     }}} ─────────────────────────────│
       │                                       │
```

## Implementation Notes (2026-03-10)

Recent refactor (migration to official SDKs):

- `mcp_client.py` now uses the official `mcp` Python SDK with the Streamable
   HTTP transport (`streamablehttp_client` + `ClientSession`). This replaces the
   previous hand-rolled JSON-RPC client and opens a short-lived `ClientSession`
   per call to remain stateless with the Opentext SDP `/mcp` endpoint.

- `gemini_agent.py` now uses the `google-adk` (Agent Development Kit) pattern:
   define typed async tool functions (ADK infers Gemini function schemas),
   build an `LlmAgent` and drive it with `Runner` + `InMemorySessionService`.
   The Runner handles multi-step function calling and per-session history.

- `requirements.txt` was updated to include `mcp>=1.5.0` and `google-adk>=1.0.0`.

These changes improve compatibility with upstream SDKs, reduce custom
serialization code, and make the agent easier to reason about and test.

### Auth / MCP metadata and UI

The wrapper now sets explicit metadata on A2A `Task` responses to make the
client-side visualization deterministic and unambiguous:

- `task.metadata.mcp_called` — boolean set to `true` when the wrapper executed
   one or more MCP tool calls against the Opentext SDP server for this request.
- `task.metadata.auth_injected` — boolean set to `true` when the wrapper
   injected or substituted a bearer token (server API key or simulated token)
   for downstream calls.

The built-in chat UI reads these flags to decide how to render the auth trace
(token obtained/injected vs token forwarded to MCP). Previously the UI inferred
MCP activity by checking for artifacts in the response; that heuristic could be
misleading when local-only tools (for example `tell_joke`) produced output.
Using explicit metadata fixes that ambiguity.

Note: some tools are local-only (implemented inside the wrapper) and are not
served by the Opentext MCP server. The codebase now preserves those local tools
when refreshing the tool registry from the MCP server so they continue to be
advertised in the AgentCard and available to the Gemini agent (for example
`tell_joke`). The registry merge happens during `populate_registry_from_mcp()`.

Also: the AgentCard now advertises an OAuth2 security scheme named
`adm_oauth` (Authorization Code + PKCE and client-credentials token URL). This
is used by A2A clients to understand the auth flows the wrapper supports.

### Network Requirements

AgentSpace is a cloud service — your wrapper **must be reachable via public HTTPS**:

| Scenario | Solution |
|---|---|
| Local development | `ngrok http 9000` — creates a public HTTPS tunnel |
| Staging / production | Cloud Run, App Engine, EC2/VM behind nginx with TLS |
| Enterprise on-prem | API Gateway or DMZ reverse proxy with a valid certificate |
| HTTP 4xx/5xx from Opentext SDP | HTTPX | Error string fed back to Gemini | `Task(FAILED)` + body |
### Step-by-Step: Register the Opentext SDP Agent in AgentSpace

**Step 1 — Make the wrapper publicly reachable**

```bash
# Development shortcut:
ngrok http 9000
# Note the HTTPS URL, e.g. https://abc123.ngrok-free.app
```

The wrapper must respond to `GET /health` and `GET /.well-known/agent-card.json` over HTTPS.

**Step 2 — Verify the AgentCard**

```bash
curl https://<your-public-url>/.well-known/agent-card.json
```

Should return the AgentCard JSON with `name`, `url`, `skills`, `capabilities`, etc.

Also confirm liveness:

```bash
curl https://<your-public-url>/health
# → {"status": "ok"}
```

**Step 3 — Open AgentSpace and type `@`**

1. Open AgentSpace at `https://vertexaisearch.cloud.google.com`
2. In the chat input box, **type `@`** — the Agents popover appears (as in the screenshot above)
3. If "Opentext SDP Agent" is not yet in the list, look for **"Connect an agent"** or **"Add external agent"** in the popover or the left sidebar Agents panel
3. If "OT ADM Agent" is not yet in the list, look for **"Connect an agent"** or **"Add external agent"** in the popover or the left sidebar Agents panel

**Step 4 — Enter the agent URL**

1. Enter your wrapper's **public base URL** (without any path):
   ```
   https://<your-public-url>
   ```
2. AgentSpace automatically fetches `/.well-known/agent-card.json` and displays:
   - Agent name: **OT ADM Agent**
   - Description from the AgentCard
   - List of skills: Get Defect, Get Story, Get Feature, Get Comments, Create Comment, Update Comment, Fetch My Work Items

**Step 5 — Save and confirm**

Click **Save** — the agent now appears in the `@` mention popover alongside other agents.

**Step 6 — Invoke the OT ADM Agent from AgentSpace chat**

Type `@` in the chat input, select **OT ADM Agent**, and continue with your request:

```
@OT ADM Agent Get defect 2110
@OT ADM Agent What are my work items?
@OT ADM Agent Add a comment to defect 2110 saying "Fixed in 5.3"
@OT ADM Agent Show comments on story 55
@OT ADM Agent Get feature 200 and summarize it
```

AgentSpace forwards the message to `/message:send` using the A2A protocol. The Gemini agent fetches data from Opentext SDP and returns a natural-language reply displayed inline in AgentSpace.

**Multi-turn context in AgentSpace:** AgentSpace passes the conversation thread ID as `contextId`, so follow-up questions within the same conversation retain full context — just as they do in the built-in chat UI.

---

## Technical Design

### Technology Stack

| Component | Technology |
|---|---|
| Runtime | Python 3.11+ |
| Web framework | FastAPI 0.115 |
| ASGI server | Uvicorn (with `watchfiles` hot-reload in dev) |
| HTTP client | HTTPX (async) |
| Data validation | Pydantic v2 |
| LLM / Agent | Google Gemini via `google-genai` SDK v1+ |
| Configuration | `python-dotenv` + environment variables |
| Frontend | Vanilla HTML/JS (static, no framework) |

### Architecture

```

                   FastAPI App (main.py)                  
                                                         
  GET  /.well-known/agent-card.json                      
  POST /message:send           
  GET  /tools                                           
  GET  /health                                          
  GET  /  (static UI)                                   

                                                
                        
                                                                  
             GEMINI_API_KEY set?                             No API key
                                                                  
                                     
                 gemini_agent                            tool_router    
                 GeminiAgent                            resolve_intent  
                 .run()                                 extract_args    
                 Agentic loop                         
                                              
                          FunctionCall                          
                                                                
                                     
                 tool_router                           mcp_client    
                 execute_tool    call_tool     
                                     
                                                                
                        
                                           JSON-RPC 2.0  POST /mcp
                                          
                                 Opentext SDP MCP Server
```

### Module Responsibilities

#### `main.py`
- Bootstraps the FastAPI application, mounts static files
- On startup, initializes `GeminiAgent` if `GEMINI_API_KEY` is set
- Routes `/message:send` to `_handle_with_agent()` or `_handle_with_keywords()`
- `_handle_with_agent()`  calls `agent.run(user_text, mcp, context_id)`, wraps the summary as the A2A Task status message, attaches Opentext SDP Artifacts
- `_handle_with_keywords()`  legacy path: keyword intent  argument extraction  MCP call  Task response

#### `gemini_agent.py`
Gemini function-calling agentic loop with per-session conversation memory and auto-generated comment text:

| Component | Purpose |
|---|---|
| `_SYSTEM_PROMPT` | Instructs Gemini to summarize Opentext SDP data naturally, use tools first, and freely draft comment text |
| `_GENERATE_TEXT_TRIGGERS` | Regex that detects "invent / funny / make up / anything" patterns |
| `_TOOL_DECLARATIONS` | 7 `types.FunctionDeclaration` objects  one per Opentext SDP MCP tool |
| `GeminiAgent.__init__` | Configures `genai.Client`, `GenerateContentConfig`, and `_histories` dict |
| `GeminiAgent.run(user_text, mcp, context_id)` | Pre-generates text if needed, loads history, drives agentic loop, saves updated history |
| `_maybe_inject_generated_text()` | Detects open-ended text requests, calls Gemini with a neutral prompt to produce concrete comment text, splices it back into the user message |
| `_get_history / _save_history` | Per-session history (keyed by `context_id`); capped at `MAX_HISTORY_TURNS=40` |
| `_call_octane()` | Executes `execute_tool()`; converts errors to strings for Gemini |
| `_artifact_to_dict()` | Flattens A2A Artifact parts into a JSON dict for Gemini FunctionResponse |

Loop cap: `MAX_TOOL_ROUNDS=10` per user turn.

#### `a2a_models.py`
Pydantic models implementing the A2A HTTP+JSON binding:

| Model | Purpose |
|---|---|
| `Part` | Single content unit (text or structured data) |
| `Message` | Collection of Parts with role, contextId, messageId |
| `SendMessageRequest` | Inbound POST body |
| `Task` / `TaskStatus` / `TaskResponse` | Outbound response envelope |
| `Artifact` | Tool result payload attached to a Task |
| `AgentCard` / `AgentSkill` | Discovery metadata at `/.well-known/agent-card.json` |
| `TaskState` | Enum: COMPLETED, FAILED, REJECTED, etc. |

#### `mcp_client.py`
- Async HTTP client for Opentext SDP MCP endpoint (JSON-RPC 2.0 POST `/mcp`)
- Injects `sharedSpaceId`, `workSpaceId` from config into every call
- Required headers: `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `Authorization: Bearer <API_KEY>`
- On HTTP error, logs the full response body before raising
- Parses both standard JSON-RPC errors and Opentext SDP's inline `isError` content errors

#### `tool_router.py`
- `TOOL_REGISTRY`  7 tool definitions with default arguments and required field lists
- `resolve_intent(text)`  priority-ordered keyword scorer (keyword fallback only)
- `extract_arguments(tool, message)`  NL extraction of `entityId`, `entityType`, `commentId`, comment `text`
- `execute_tool(tool, args, mcp)`  shared by both paths; calls `mcp.call_tool()`, converts MCP content blocks to A2A `Part` objects, returns an `Artifact`

#### `config.py`
Single source of truth for all configuration. Loads from `.env` via `python-dotenv`.

### Data Flow  `/message:send` (Gemini agent path)

```
POST /message:send
  
    SendMessageRequest (Pydantic validation)
  
Extract user text + context_id from message
  
  
agent.run(user_text, mcp, context_id)
  
   _maybe_inject_generated_text()  pre-generate text if "invent/funny/anything" detected
  
   Load prior session history from _histories[context_id]
  
   [Round 1] Gemini  FunctionCall(name, args)
         
          execute_tool(name, args)  Artifact   stored
         
          FunctionResponse sent back to Gemini
  
   [Round N] repeat until Gemini returns plain text
  
   Save updated history back to _histories[context_id]
  
   Gemini error   Task(FAILED)
  
  
summary (str) + artifacts (list[Artifact])
  
  
Task(COMPLETED)
  status.message.parts[0].text = summary
  artifacts = [all collected Artifacts]
```

### JSON-RPC Request Format

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": "<auto-incrementing integer>",
  "params": {
    "name": "get_defect",
    "arguments": {
      "entityId": 2110,
      "sharedSpaceId": 1001,
      "workSpaceId": 1002
    }
  }
}
```

### Error Handling Matrix

| Error type | Source | Gemini agent path | Keyword fallback path |
|---|---|---|---|
| Gemini API failure | Gemini | `Task(FAILED)` with error detail | N/A |
| Unknown intent | Wrapper | N/A (Gemini decides) | `Task(REJECTED)` |
| Argument parse failure | Wrapper | N/A (Gemini provides args) | `Task(FAILED)` |
| HTTP 4xx/5xx from Opentext SDP | HTTPX | Error string fed back to Gemini | `Task(FAILED)` + body |
| Timeout | HTTPX | Error string fed back to Gemini | `Task(FAILED)` |
| JSON-RPC error | Opentext SDP | Error string fed back to Gemini | `Task(FAILED)` |
| Opentext SDP inline `isError` | Opentext SDP | Error string fed back to Gemini | `Task(FAILED)` |
| Max tool rounds exceeded | Wrapper | Final text from last response | N/A |

### Security

- Opentext SDP authentication: **Bearer token** (`Authorization: Bearer <API_KEY>`)
- Gemini API key: passed at agent initialization via `genai.Client(api_key=...)`
- Inbound A2A requests: no authentication enforced (suitable for internal/trusted networks)
- All secrets loaded from environment variables, never hardcoded

### Known Limitations

- **No streaming**  A2A streaming and SSE push notifications are not implemented
- **Sync Gemini SDK**  `google-genai` v1 has no native async client; calls run via `asyncio.to_thread`
- **In-memory history only**  conversation history is stored in the `GeminiAgent` instance and lost on server restart; Redis or similar would be needed for production persistence
- **Single workspace**  `sharedSpaceId` and `workSpaceId` are global defaults, not per-user or per-request
- **Keyword fallback is limited**  complex or ambiguous prompts may not resolve correctly without a Gemini API key
- **No inbound authentication**  the wrapper does not verify A2A caller identity; suitable for internal/trusted network deployment
