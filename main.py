"""
A2A ↔ Octane MCP Agent Wrapper
===============================

A lightweight FastAPI application that bridges the Google A2A protocol
(HTTP+JSON binding) with the internal Octane MCP Server.

Architecture:

    Gemini Enterprise ──A2A──▶  this wrapper  ──JSON-RPC POST──▶  Octane /mcp

Endpoints:
    GET  /.well-known/agent-card.json   → AgentCard discovery
    POST /message:send                  → A2A SendMessage (primary)
    GET  /health                        → Liveness check
"""

from __future__ import annotations

import logging
import uuid
import asyncio

import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import config
from pydantic import BaseModel
from a2a_models import (
    AgentCard,
    AgentCapabilities,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Artifact,
    Message,
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

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("a2a-wrapper")

# ── App & shared MCP client ─────────────────────────────────────────

app = FastAPI(
    title="Octane A2A Agent Wrapper",
    version=config.AGENT_VERSION,
    description="Bridges Google A2A protocol to the Octane MCP Server.",
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
        logger.info("Discovered %d tools from Octane MCP server", len(mcp_tools))
    except Exception as exc:
        logger.warning(
            "Could not auto-discover MCP tools (Octane unreachable?); "
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


# ── Runtime configuration ────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    octane_url: str | None = None
    api_key: str | None = None
    shared_space_id: int | None = None
    workspace_id: int | None = None
    gemini_enabled: bool | None = None


def _masked_key(key: str) -> str:
    if not key:
        return ""
    return ("*" * max(len(key) - 4, 0)) + key[-4:]


@app.get("/config")
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
    })


@app.post("/config")
async def update_config(body: ConfigUpdate):
    """Update Octane URL and/or API key at runtime and reinitialise the MCP client."""
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
    })


# ── Agent Card (discovery) ───────────────────────────────────────────

def _build_agent_card(base_url: str) -> AgentCard:
    """Construct the AgentCard from the tool registry and config."""
    skills = [
        AgentSkill(
            id=tool_name,
            name=tool_name.replace("_", " ").title(),
            description=tool_def["description"],
            tags=["octane", "alm", tool_name],
            examples=tool_def.get("example_prompts", []),
        )
        for tool_name, tool_def in TOOL_REGISTRY.items()
    ]

    return AgentCard(
        name="ALM Octane Agent",
        description=(
            "An agent that provides read access to OpenText Octane ALM data "
            "(defects, user stories, features) via the Octane MCP Server."
        ),
        version=config.AGENT_VERSION,
        supportedInterfaces=[
            AgentInterface(
                url=f"{base_url}/message:send",
                protocolBinding="HTTP+JSON",
                protocolVersion="1.0",
            )
        ],
        provider=AgentProvider(
            organization="OpenText",
            url="https://www.opentext.com",
        ),
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        securitySchemes={
            "bearer": SecurityScheme(
                httpAuthSecurityScheme={"scheme": "Bearer"}
            )
        },
        defaultInputModes=["text/plain", "application/json"],
        defaultOutputModes=["application/json", "text/plain"],
        skills=skills,
    )


@app.post("/discover-tools")
async def discover_tools():
    """Manual trigger to discover MCP tools and refresh the router/agent.

    Useful when you add a tool to Octane and want the wrapper to pick it up
    immediately without restarting.
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
async def agent_card(request: Request):
    """
    AgentCard discovery endpoint.
    Gemini / AgentSpace GETs this to learn what the agent can do.
    """
    base_url = str(request.base_url).rstrip("/")
    card = _build_agent_card(base_url)
    return JSONResponse(
        content=card.model_dump(exclude_none=True),
        media_type="application/json",
    )


# ── A2A SendMessage ─────────────────────────────────────────────────

@app.post("/message:send")
async def send_message(req: SendMessageRequest):
    """
    Primary A2A endpoint.

    When GEMINI_API_KEY is configured the request is handled by the
    Gemini function-calling agent (real agentic loop).
    Otherwise falls back to the lightweight keyword router.
    """
    user_msg = req.message
    context_id = user_msg.contextId or str(uuid.uuid4())
    task_id = user_msg.taskId or str(uuid.uuid4())
    user_text = " ".join(p.text for p in user_msg.parts if p.text) or ""
    logger.info("A2A request  context=%s  text=%r", context_id, user_text)

    if agent is not None:
        return await _handle_with_agent(task_id, context_id, user_text)
    return await _handle_with_keywords(task_id, context_id, user_text, user_msg)


async def _handle_with_agent(
    task_id: str, context_id: str, user_text: str
) -> dict:
    """Run the Gemini agentic loop and wrap the result as an A2A Task."""
    try:
        summary, artifacts = await agent.run(user_text, mcp, context_id)
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
    )
    logger.info("Agent response  task=%s  artifacts=%d", task.id, len(artifacts or []))
    return TaskResponse(task=task).model_dump(exclude_none=True)


async def _handle_with_keywords(
    task_id: str, context_id: str, user_text: str, user_msg
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
        artifact = await execute_tool(tool_name, arguments, mcp)
    except OctaneMcpError as exc:
        logger.error("Octane MCP error: %s", exc)
        return _error_task(
            task_id, context_id, TaskState.FAILED,
            f"Octane returned an error: {exc.message} (code {exc.code})",
        )
    except httpx.TimeoutException:
        logger.error("Timeout calling Octane MCP server")
        return _error_task(
            task_id, context_id, TaskState.FAILED,
            "Request to Octane MCP server timed out.",
        )
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json()
        except Exception:
            detail = exc.response.text
        logger.error("HTTP error from Octane: %s  body=%s", exc, detail)
        return _error_task(
            task_id, context_id, TaskState.FAILED,
            f"Octane HTTP error: {exc.response.status_code} – {detail}",
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
    Proxy to Octane's tools/list – lets you verify the exact tool names
    the Octane MCP server exposes so they can be matched in TOOL_REGISTRY.
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
