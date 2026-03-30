"""
A2A ↔ Opentext SDP MCP Agent Wrapper
===============================

A lightweight FastAPI application that bridges the Google A2A protocol
(both HTTP+JSON and JSON-RPC 2.0 bindings) with the internal Opentext
SDP MCP Server.

Architecture:

    Gemini Enterprise ──A2A──▶  this wrapper  ──JSON-RPC POST──▶  Opentext SDP /mcp

Endpoints:
    POST /                              → A2A JSON-RPC 2.0 binding (Gemini Enterprise default)
    GET  /.well-known/agent-card.json   → AgentCard discovery
    POST /message:send                  → A2A HTTP+JSON binding (REST)
    GET  /health                        → Liveness check
"""

from __future__ import annotations

import logging
import uuid
import asyncio

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, FileResponse
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import config
from pydantic import BaseModel
from a2a_models import (
    AgentCard,
    AgentCapabilities,
    AgentProvider,
    AgentSkill,
    Artifact,
    AuthorizationCodeFlow,
    ClientCredentialsFlow,
    JsonRpcError,
    JsonRpcResponse,
    Message,
    OAuthFlows,
    Part,
    Role,
    SecurityScheme,
    SendMessageRequest,
    Task,
    TaskResponse,
    TaskState,
    TaskStatus,
)
from gemini_agent import GeminiAgent
from mcp_client import OctaneMcpClient, OctaneMcpError
from tool_router import (
    TOOL_REGISTRY,
    execute_tool,
    extract_arguments,
    populate_registry_from_mcp,
    resolve_intent,
)

# ── Auth ─────────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)

def _verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Validate inbound Bearer token for admin endpoints when A2A_API_KEY is configured."""
    if not config.A2A_API_KEY:
        return  # auth disabled — A2A_API_KEY not set
    if credentials is None or credentials.credentials != config.A2A_API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def _extract_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Extract the Bearer token from the inbound request (optional).

    When Gemini/AgentSpace calls this A2A endpoint it injects
    Authorization: Bearer <TOKEN> which is passed through to Octane.
    For Chat UI / demo use without an explicit token the server falls back
    to config.API_KEY, so the header is not required.
    """
    return credentials.credentials if credentials else ""

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("a2a-wrapper")

# ── App & shared MCP client ─────────────────────────────────────────

app = FastAPI(
    title="Opentext SDP A2A Agent Wrapper",
    version=config.AGENT_VERSION,
    description="Bridges Google A2A protocol to the Opentext SDP MCP Server.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

mcp = OctaneMcpClient()
agent: GeminiAgent | None = None


@app.on_event("startup")
async def _startup() -> None:
    global agent
    # ── Auto-discover tools from MCP server ──────────────────────────
    try:
        raw = await mcp.list_tools()
        mcp_tools = raw.get("tools", [])
        populate_registry_from_mcp(mcp_tools)
        logger.info("Discovered %d tools from Opentext SDP MCP server", len(mcp_tools))
    except Exception as exc:
        logger.warning(
            "Could not auto-discover MCP tools (Opentext SDP unreachable?); "
            "using built-in registry. Error: %s", exc
        )
    # ── Initialise Gemini agent ───────────────────────────────────────
    if config.GEMINI_API_KEY:
        try:
            agent = GeminiAgent()
            await agent.refresh_tools(mcp)   # update Gemini with live tool list
            logger.info(
                "Gemini agent initialised  model=%s", config.GEMINI_MODEL
            )
        except Exception as exc:
            logger.error("Failed to initialise Gemini agent: %s", exc)
    else:
        logger.warning(
            "GEMINI_API_KEY not configured — using keyword-based fallback router"
        )

    # Start periodic MCP discovery task if enabled
    try:
        interval = int(config.MCP_TOOL_POLL_INTERVAL_SECONDS)
    except Exception:
        interval = 0
    if interval > 0:
        # Fire-and-forget background coroutine that refreshes TOOL_REGISTRY
        async def _periodic_discovery():
            global mcp, agent
            while True:
                try:
                    raw = await mcp.list_tools()
                    mcp_tools = raw.get("tools", [])
                    populate_registry_from_mcp(mcp_tools)
                    if agent:
                        await agent.refresh_tools(mcp)
                    logger.info("Periodic discovery: refreshed %d tools", len(mcp_tools))
                except Exception as exc:
                    logger.warning("Periodic discovery failed: %s", exc)
                await asyncio.sleep(interval)

        asyncio.create_task(_periodic_discovery())

# ── Static UI ────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def ui():
    """Serve the chat UI."""
    return FileResponse("static/index.html")


@app.post("/")
async def jsonrpc_endpoint(request: Request):
    """
    A2A JSON-RPC 2.0 binding (POST /).

    Gemini Enterprise calls this endpoint by default.  All JSON-RPC methods
    are dispatched here; currently supported:

        message/send  → same handler as POST /message:send
        tasks/get     → not yet persisted; returns method-not-found
        tasks/cancel  → not yet persisted; returns method-not-found
    """
    # ── Parse JSON-RPC envelope ───────────────────────────────────────
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Parse error"}}
        )

    rpc_id = body.get("id")
    method  = body.get("method", "")
    params  = body.get("params") or {}

    if body.get("jsonrpc") != "2.0" or not method:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rpc_id,
             "error": {"code": -32600, "message": "Invalid Request"}}
        )

    # ── Extract bearer token from Authorization header ────────────────
    bearer_token = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]

    # ── Dispatch ──────────────────────────────────────────────────────
    if method == "message/send":
        return await _jsonrpc_message_send(rpc_id, params, bearer_token)

    # tasks/get and tasks/cancel require server-side task persistence which
    # is not yet implemented; return a meaningful not-found rather than 405.
    if method in ("tasks/get", "tasks/cancel", "tasks/resubscribe"):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rpc_id,
             "error": {"code": -32601,
                        "message": f"Method '{method}' is not yet implemented"}}
        )

    return JSONResponse(
        {"jsonrpc": "2.0", "id": rpc_id,
         "error": {"code": -32601, "message": f"Method not found: {method}"}}
    )


async def _jsonrpc_message_send(
    rpc_id: str | int | None,
    params: dict,
    bearer_token: str,
) -> JSONResponse:
    """Handle the JSON-RPC message/send method and wrap result in a JSON-RPC envelope."""
    try:
        req = SendMessageRequest(**params)
    except Exception as exc:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rpc_id,
             "error": {"code": -32602, "message": f"Invalid params: {exc}"}}
        )

    # Token resolution — same logic as the HTTP+JSON send_message handler
    octane_token = bearer_token
    if config.API_KEY:
        if not config.A2A_API_KEY:
            octane_token = config.API_KEY
            logger.info("JSON-RPC: no A2A_API_KEY set — using server API_KEY for Octane")
        elif bearer_token == config.A2A_API_KEY:
            octane_token = config.API_KEY
            logger.info("JSON-RPC: admin key detected — substituting server API_KEY for Octane")

    user_msg    = req.message
    context_id  = user_msg.contextId or str(uuid.uuid4())
    task_id     = user_msg.taskId    or str(uuid.uuid4())
    user_text   = " ".join(p.text for p in user_msg.parts if p.text) or ""
    logger.info("JSON-RPC message/send  id=%s  context=%s  text=%r", rpc_id, context_id, user_text)

    if agent is not None:
        result = await _handle_with_agent(task_id, context_id, user_text, octane_token)
    else:
        result = await _handle_with_keywords(task_id, context_id, user_text, user_msg, octane_token)

    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


@app.get("/auth-test")
async def auth_test_ui():
    """Serve the animated A2A OAuth2 auth flow test visualizer."""
    return FileResponse("static/auth-flow-test.html")


# ── Runtime configuration ────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    octane_url: str | None = None
    api_key: str | None = None
    shared_space_id: int | None = None
    workspace_id: int | None = None
    gemini_enabled: bool | None = None
    gemini_model: str | None = None


def _masked_key(key: str) -> str:
    if not key:
        return ""
    return ("*" * max(len(key) - 4, 0)) + key[-4:]


@app.get("/config", dependencies=[Depends(_verify_token)])
async def get_config():
    """Return the current runtime configuration (API key is masked)."""
    return JSONResponse({
        "octane_url": config.OCTANE_BASE_URL,
        "api_key_masked": _masked_key(config.API_KEY),
        "api_key_set": bool(config.API_KEY),
        "shared_space_id": config.DEFAULT_SHARED_SPACE_ID,
        "workspace_id": config.DEFAULT_WORKSPACE_ID,
        "gemini_enabled": agent is not None,
        "gemini_api_key_set": bool(config.GEMINI_API_KEY),
        "gemini_model": config.GEMINI_MODEL,
    })


@app.post("/config", dependencies=[Depends(_verify_token)])
async def update_config(body: ConfigUpdate):
    """Update Opentext SDP URL and/or API key at runtime and reinitialise the MCP client."""
    global mcp, agent
    changed = []
    if body.octane_url is not None:
        config.OCTANE_BASE_URL = body.octane_url.rstrip("/")
        config.OCTANE_MCP_ENDPOINT = f"{config.OCTANE_BASE_URL}/mcp"
        changed.append("octane_url")
    if body.api_key is not None:
        config.API_KEY = body.api_key
        changed.append("api_key")
    if body.shared_space_id is not None:
        config.DEFAULT_SHARED_SPACE_ID = body.shared_space_id
        changed.append("shared_space_id")
    if body.workspace_id is not None:
        config.DEFAULT_WORKSPACE_ID = body.workspace_id
        changed.append("workspace_id")
    if body.gemini_model is not None and body.gemini_model.strip():
        config.GEMINI_MODEL = body.gemini_model.strip()
        changed.append("gemini_model")
        # Rebuild agent with new model if it's currently running
        if agent is not None:
            try:
                agent = GeminiAgent()
                await agent.refresh_tools(mcp)
                logger.info("Gemini agent rebuilt with model=%s", config.GEMINI_MODEL)
            except Exception as exc:
                logger.warning("Failed to rebuild agent after model change: %s", exc)
                agent = None
    if body.gemini_enabled is not None:
        if body.gemini_enabled:
            if not config.GEMINI_API_KEY:
                # No API key anywhere — leave agent as None (keyword fallback)
                logger.warning("Gemini toggle ON but no GEMINI_API_KEY — using keyword fallback")
            elif agent is None:
                try:
                    agent = GeminiAgent()
                    await agent.refresh_tools(mcp)
                    logger.info("Gemini agent enabled at runtime")
                except Exception as exc:
                    logger.warning("Failed to init Gemini agent, using keyword fallback: %s", exc)
                    agent = None
        else:
            agent = None
            logger.info("Gemini agent disabled at runtime — using keyword fallback")
        config.GEMINI_ENABLED = body.gemini_enabled
        changed.append("gemini_enabled")
    # Re-create the shared MCP client with updated credentials
    mcp = OctaneMcpClient(
        base_url=config.OCTANE_MCP_ENDPOINT,
        api_key=config.API_KEY,
    )
    # Re-discover tools with the new server / credentials
    try:
        raw = await mcp.list_tools()
        mcp_tools = raw.get("tools", [])
        populate_registry_from_mcp(mcp_tools)
        if agent:
            await agent.refresh_tools(mcp)
        logger.info("Tool re-discovery after config change: %d tools", len(mcp_tools))
    except Exception as exc:
        logger.warning("Tool re-discovery failed after config change: %s", exc)
    logger.info("Runtime config updated: %s", changed)
    return JSONResponse({
        "status": "ok",
        "changed": changed,
        "octane_url": config.OCTANE_BASE_URL,
        "api_key_masked": _masked_key(config.API_KEY),
        "shared_space_id": config.DEFAULT_SHARED_SPACE_ID,
        "workspace_id": config.DEFAULT_WORKSPACE_ID,
        "gemini_enabled": agent is not None,
        "gemini_api_key_set": bool(config.GEMINI_API_KEY),
        "gemini_model": config.GEMINI_MODEL,
    })


# ── Agent Card (discovery) ───────────────────────────────────────────

def _build_agent_card() -> AgentCard:
    """Construct the AgentCard from the tool registry and config."""
    skills = [
        AgentSkill(
            id=tool_name,
            name=tool_name.replace("_", " ").title(),
            description=tool_def["description"],
            tags=["opentext-sdp", tool_name],
            examples=tool_def.get("example_prompts", []),
        )
        for tool_name, tool_def in TOOL_REGISTRY.items()
    ]

    _oauth_scopes = {
        "otds:groups": "Access to groups",
        "otds:roles": "Access to roles",
        "search": "Access to search",
    }

    return AgentCard(
        name=config.AGENT_NAME,
        description=(
            "Query and manage Opentext SDP work items — defects, stories, features, "
            "comments, and personal work lists — using natural language. "
            "Powered by a Gemini function-calling agent. "
            "Invoke with @OT ADM Agent in Google AgentSpace."
        ),
        version=config.AGENT_VERSION,
        url=config.AGENT_URL,
        preferredTransport="JSONRPC",
        protocolVersion="0.3.0",
        supportsAuthenticatedExtendedCard=True,
        provider=AgentProvider(
            organization="OpenText",
            url="https://opentext.com",
        ),
        capabilities=AgentCapabilities(streaming=False),
        securitySchemes={
            "csai_oauth": SecurityScheme(
                type="oauth2",
                flows=OAuthFlows(
                    clientCredentials=ClientCredentialsFlow(
                        tokenUrl=config.OAUTH2_TOKEN_URL,
                        scopes=_oauth_scopes,
                    ),
                    authorizationCode=AuthorizationCodeFlow(
                        authorizationUrl=config.OAUTH2_AUTH_URL,
                        tokenUrl=config.OAUTH2_TOKEN_URL,
                        scopes=_oauth_scopes,
                        pkce=True,
                        pkceMethod="S256",
                    ),
                ),
            )
        },
        security=[{"csai_oauth": ["otds:groups", "otds:roles", "search"]}],
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
        skills=skills,
    )


@app.get("/sim/token", dependencies=[Depends(_verify_token)])
async def sim_token():
    """Return the configured Octane API key for UI simulation use.

    The UI exchanges its A2A_API_KEY (admin key) for the real Octane bearer token
    so the simulation can demonstrate the full passthrough flow without requiring
    the user to manually copy-paste the long Octane API_KEY into the browser.

    Protected by A2A_API_KEY — for PoC/demo use only.
    """
    if not config.API_KEY:
        raise HTTPException(status_code=404, detail="No API_KEY configured on the server")
    return JSONResponse({"token": config.API_KEY})


@app.post("/discover-tools")
async def discover_tools():
    """Manual trigger to discover MCP tools and refresh the router/agent.

    No auth required — this is a read-only cache refresh (calls MCP list_tools and
    updates internal state). Safe to expose publicly; no data is written or returned.
    """
    global mcp, agent
    try:
        raw = await mcp.list_tools()
        mcp_tools = raw.get("tools", [])
        populate_registry_from_mcp(mcp_tools)
        if agent:
            await agent.refresh_tools(mcp)
        return JSONResponse({"status": "ok", "count": len(mcp_tools)})
    except Exception as exc:
        logger.warning("Manual discovery failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/.well-known/agent-card.json")
async def agent_card():
    """
    AgentCard discovery endpoint.
    Gemini / AgentSpace GETs this to learn what the agent can do.
    """
    card = _build_agent_card()
    return JSONResponse(
        content=card.model_dump(exclude_none=True),
        media_type="application/json",
    )


@app.get("/raw-readme")
async def raw_readme():
        """Return the README.md file as plain text (useful for in-app rendering)."""
        try:
                return FileResponse("README.md", media_type="text/plain")
        except Exception as exc:
                logger.exception("Failed to read README.md: %s", exc)
                raise HTTPException(status_code=500, detail=str(exc))


@app.get("/readme")
async def readme():
        """Serve a small HTML viewer that fetches /raw-readme and renders Markdown.

        This avoids GitHub access issues for private repos and provides a rendered
        Markdown view inside the app (opens in a new tab from the UI).
        """
        html = """
<!doctype html>
<html>
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width,initial-scale=1" />
        <title>README.md</title>
        <style>
            body {
                font-family: Segoe UI, Roboto, Arial, Helvetica, sans-serif;
                padding: 24px;
                background: #ffffff;
                color: #111111;
                line-height: 1.6;
            }
            a { color: #0366d6; }
            pre, code { background: #f6f8fa; color: #111111; }
            pre { padding: 12px; border-radius: 6px; overflow: auto; }
            img { max-width: 100%; height: auto; }
            .markdown-body { max-width: 880px; margin: 0 auto; }
        </style>
    </head>
    <body>
        <div class="markdown-body" id="content">Loading README…</div>
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <script>
            fetch('/raw-readme').then(r=>{
                if (!r.ok) throw new Error('Could not fetch README');
                return r.text();
            }).then(md=>{
                // Use marked to render the markdown into the page
                document.getElementById('content').innerHTML = marked.parse(md);
            }).catch(err=>{
                document.getElementById('content').textContent = 'Error loading README: ' + err.message;
            });
        </script>
    </body>
</html>
"""
        return HTMLResponse(content=html, status_code=200)


# ── A2A SendMessage ─────────────────────────────────────────────────

@app.post("/message:send")
async def send_message(
    req: SendMessageRequest,
    bearer_token: str = Depends(_extract_bearer_token),
):
    """
    Primary A2A endpoint.

    Gemini injects Authorization: Bearer <TOKEN> which is extracted and
    forwarded to the Octane MCP server on every downstream call.

    When GEMINI_API_KEY is configured the request is handled by the
    Gemini function-calling agent (real agentic loop).
    Otherwise falls back to the lightweight keyword router.
    """
    user_msg = req.message
    context_id = user_msg.contextId or str(uuid.uuid4())
    task_id = user_msg.taskId or str(uuid.uuid4())
    user_text = " ".join(p.text for p in user_msg.parts if p.text) or ""
    logger.info("A2A request  context=%s  text=%r", context_id, user_text)

    # Resolve which token to forward to the Octane MCP server:
    #   - No A2A_API_KEY configured → no way to distinguish admin from real tokens;
    #     always use the server's configured API_KEY (covers Chat UI / demo use).
    #   - A2A_API_KEY configured and bearer matches it → Chat UI / demo mode;
    #     substitute with server's API_KEY.
    #   - A2A_API_KEY configured and bearer doesn't match → real Gemini OAuth token;
    #     pass through as-is.
    octane_token = bearer_token
    if config.API_KEY:
        if not config.A2A_API_KEY:
            octane_token = config.API_KEY
            logger.info("No A2A_API_KEY set — using server API_KEY for Octane downstream call")
        elif bearer_token == config.A2A_API_KEY:
            octane_token = config.API_KEY
            logger.info("Admin key detected — substituting server API_KEY for Octane downstream call")

    if agent is not None:
        return await _handle_with_agent(task_id, context_id, user_text, octane_token)
    return await _handle_with_keywords(task_id, context_id, user_text, user_msg, octane_token)


async def _handle_with_agent(
    task_id: str, context_id: str, user_text: str, bearer_token: str
) -> dict:
    """Run the Gemini agentic loop and wrap the result as an A2A Task."""
    try:
        summary, artifacts, mcp_called = await agent.run(user_text, mcp, context_id, bearer_token)
    except Exception as exc:
        logger.exception("Gemini agent error")
        return _error_task(
            task_id, context_id, TaskState.FAILED, f"Agent error: {exc}"
        )

    task = Task(
        id=task_id,
        contextId=context_id,
        status=TaskStatus(
            state=TaskState.COMPLETED,
            message=Message(
                role=Role.AGENT,
                parts=[Part(text=summary)],
            ),
        ),
        artifacts=artifacts if artifacts else None,
        metadata={"mcp_called": mcp_called, "auth_injected": bearer_token is not None},
    )
    logger.info("Agent response  task=%s  artifacts=%d  mcp_called=%s  auth_injected=%s", task.id, len(artifacts or []), mcp_called, bearer_token is not None)
    return TaskResponse(task=task).model_dump(exclude_none=True)


async def _handle_with_keywords(
    task_id: str, context_id: str, user_text: str, user_msg, bearer_token: str
) -> dict:
    """Legacy keyword-based routing fallback (no Gemini API key required)."""
    tool_name = resolve_intent(user_text)
    if tool_name is None:
        for part in user_msg.parts:
            if part.data and isinstance(part.data, dict) and "tool" in part.data:
                tool_name = part.data["tool"]
                break

    if tool_name is None or tool_name not in TOOL_REGISTRY:
        return _error_task(
            task_id,
            context_id,
            TaskState.REJECTED,
            f"Could not determine a supported tool from the request. "
            f"Supported tools: {', '.join(TOOL_REGISTRY)}",
        )

    try:
        arguments = extract_arguments(tool_name, user_msg)
    except Exception as exc:
        logger.exception("Argument extraction failed")
        return _error_task(
            task_id, context_id, TaskState.FAILED,
            f"Failed to parse arguments: {exc}",
        )

    try:
        artifact = await execute_tool(tool_name, arguments, mcp, bearer_token=bearer_token)
    except OctaneMcpError as exc:
        logger.error("Opentext SDP MCP error: %s", exc)
        return _error_task(
            task_id, context_id, TaskState.FAILED,
            f"Opentext SDP returned an error: {exc.message} (code {exc.code})",
        )
    except httpx.TimeoutException:
        logger.error("Timeout calling Opentext SDP MCP server")
        return _error_task(
            task_id, context_id, TaskState.FAILED,
            "Request to Opentext SDP MCP server timed out.",
        )
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json()
        except Exception:
            detail = exc.response.text
        logger.error("HTTP error from Opentext SDP: %s  body=%s", exc, detail)
        return _error_task(
            task_id, context_id, TaskState.FAILED,
            f"Opentext SDP HTTP error: {exc.response.status_code} – {detail}",
        )
    except Exception as exc:
        logger.exception("Unexpected error during MCP call")
        return _error_task(
            task_id, context_id, TaskState.FAILED,
            f"Internal error: {exc}",
        )

    task = Task(
        id=task_id,
        contextId=context_id,
        status=TaskStatus(
            state=TaskState.COMPLETED,
            message=Message(
                role=Role.AGENT,
                parts=[Part(text=f"Successfully executed {tool_name}.")],
            ),
        ),
        artifacts=[artifact],
        metadata={"mcp_called": True, "auth_injected": bearer_token is not None},
    )
    logger.info("Keyword response  task=%s  state=%s", task.id, task.status.state)
    return TaskResponse(task=task).model_dump(exclude_none=True)


# ── Health check ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": config.AGENT_VERSION}


@app.get("/tools")
async def list_tools():
    """
    Proxy to Opentext SDP's tools/list – lets you verify the exact tool names
    the Opentext SDP MCP server exposes so they can be matched in TOOL_REGISTRY.
    """
    try:
        result = await mcp.list_tools()
        return JSONResponse(content=result)
    except OctaneMcpError as exc:
        raise HTTPException(status_code=502, detail=exc.message)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Helpers ──────────────────────────────────────────────────────────

def _error_task(
    task_id: str,
    context_id: str,
    state: TaskState,
    detail: str,
) -> dict:
    """Build an A2A Task response representing a failure."""
    task = Task(
        id=task_id,
        contextId=context_id,
        status=TaskStatus(
            state=state,
            message=Message(
                role=Role.AGENT,
                parts=[Part(text=detail)],
            ),
        ),
    )
    return TaskResponse(task=task).model_dump(exclude_none=True)


# ── Entrypoint ───────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.A2A_HOST,
        port=config.A2A_PORT,
        reload=True,
    )
