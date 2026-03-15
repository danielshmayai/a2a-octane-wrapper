"""
Pydantic models for the Google A2A (Agent-to-Agent) protocol.

Implements the HTTP+JSON binding subset needed for this PoC:
  - AgentCard (served at /.well-known/agent-card.json)
  - SendMessage request  (POST /message:send)
  - Task / Artifact response envelope

Reference: https://a2a-protocol.org/latest/specification/
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────

class Role(str, Enum):
    USER = "ROLE_USER"
    AGENT = "ROLE_AGENT"


class TaskState(str, Enum):
    SUBMITTED = "TASK_STATE_SUBMITTED"
    WORKING = "TASK_STATE_WORKING"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    REJECTED = "TASK_STATE_REJECTED"
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"


# ── Message Parts ────────────────────────────────────────────────────

class Part(BaseModel):
    """A single content part inside a Message or Artifact."""
    text: str | None = None
    data: Any | None = None
    mediaType: str | None = None
    metadata: dict[str, Any] | None = None


class Message(BaseModel):
    messageId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: Role
    parts: list[Part]
    contextId: str | None = None
    taskId: str | None = None
    metadata: dict[str, Any] | None = None


# ── Send-Message Request ─────────────────────────────────────────────

class SendMessageConfiguration(BaseModel):
    acceptedOutputModes: list[str] | None = None
    blocking: bool = True
    historyLength: int | None = None


class SendMessageRequest(BaseModel):
    """Inbound payload for POST /message:send."""
    message: Message
    configuration: SendMessageConfiguration | None = None
    metadata: dict[str, Any] | None = None


# ── Task Response ────────────────────────────────────────────────────

class TaskStatus(BaseModel):
    state: TaskState
    message: Message | None = None
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class Artifact(BaseModel):
    artifactId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str | None = None
    description: str | None = None
    parts: list[Part]
    metadata: dict[str, Any] | None = None


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    contextId: str | None = None
    status: TaskStatus
    artifacts: list[Artifact] | None = None
    history: list[Message] | None = None
    metadata: dict[str, Any] | None = None


class TaskResponse(BaseModel):
    """Top-level A2A response envelope (HTTP+JSON binding)."""
    task: Task


# ── Agent Card (Discovery) ───────────────────────────────────────────

class AgentSkill(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str]
    examples: list[str] | None = None
    inputModes: list[str] | None = None
    outputModes: list[str] | None = None


class AgentCapabilities(BaseModel):
    streaming: bool = False
    pushNotifications: bool = False


class AgentProvider(BaseModel):
    organization: str
    url: str | None = None


class ClientCredentialsFlow(BaseModel):
    """OAuth2 client credentials flow."""
    tokenUrl: str
    scopes: dict[str, str] = {}


class AuthorizationCodeFlow(BaseModel):
    """OAuth2 authorization code flow (with optional PKCE)."""
    authorizationUrl: str
    tokenUrl: str
    scopes: dict[str, str] = {}
    pkce: bool | None = None
    pkceMethod: str | None = None


class OAuthFlows(BaseModel):
    """Container for OAuth2 flows."""
    clientCredentials: ClientCredentialsFlow | None = None
    authorizationCode: AuthorizationCodeFlow | None = None


class SecurityScheme(BaseModel):
    """OpenAPI 3.0-style security scheme.

    For Bearer (HTTP): set type='http', scheme='bearer'.
    For OAuth2: set type='oauth2' and populate flows.
    """
    type: str
    scheme: str | None = None   # only for type='http'
    flows: OAuthFlows | None = None  # only for type='oauth2'


class AgentInterface(BaseModel):
    url: str
    protocolBinding: str = "HTTP+JSON"
    protocolVersion: str = "1.0"


class AgentCard(BaseModel):
    name: str
    description: str
    version: str
    # A2A 0.3.0 top-level URL and transport fields
    url: str | None = None
    preferredTransport: str | None = None
    protocolVersion: str | None = None
    supportsAuthenticatedExtendedCard: bool | None = None
    # Legacy interface list (A2A < 0.3.0)
    supportedInterfaces: list[AgentInterface] | None = None
    provider: AgentProvider | None = None
    capabilities: AgentCapabilities = AgentCapabilities()
    securitySchemes: dict[str, SecurityScheme] | None = None
    security: list[dict[str, list]] | None = None
    defaultInputModes: list[str] = ["text/plain"]
    defaultOutputModes: list[str] = ["text/plain"]
    skills: list[AgentSkill] = []
