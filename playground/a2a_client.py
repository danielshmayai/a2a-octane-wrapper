"""
Reusable A2A client for the Octane Wrapper playground.

Usage:
    from a2a_client import A2AClient

    client = A2AClient()                          # reads A2A_WRAPPER_URL from .env
    response = await client.send("List incidents")
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

DEFAULT_URL = os.getenv("A2A_WRAPPER_URL", "http://localhost:9000")
DEFAULT_TOKEN = os.getenv("A2A_BEARER_TOKEN", "")


class A2AClient:
    """Thin async client for the A2A Octane Wrapper."""

    def __init__(self, base_url: str = DEFAULT_URL, token: str = DEFAULT_TOKEN):
        self.base_url = base_url.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=60)
        self.context_id: str = str(uuid.uuid4())

    async def agent_card(self) -> dict[str, Any]:
        """Fetch the agent discovery card."""
        r = await self._http.get("/.well-known/agent-card.json")
        r.raise_for_status()
        return r.json()

    async def send(
        self,
        text: str,
        *,
        context_id: str | None = None,
        new_session: bool = False,
    ) -> str:
        """
        Send a message and return the text response.

        Args:
            text: Natural-language query or instruction.
            context_id: Reuse a specific session ID.
            new_session: Start a fresh conversation (generates new context_id).
        """
        if new_session:
            self.context_id = str(uuid.uuid4())
        cid = context_id or self.context_id

        payload = {
            "message": {
                "messageId": str(uuid.uuid4()),
                "contextId": cid,
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            },
            "configuration": {"blocking": True},
        }
        r = await self._http.post("/message:send", json=payload)
        r.raise_for_status()
        data = r.json()
        return self._extract_text(data)

    def _extract_text(self, data: dict[str, Any]) -> str:
        """Pull the assistant text out of the A2A response."""
        # TaskResponse → task.artifacts[0].parts[0].text
        try:
            artifacts = data["task"]["artifacts"]
            parts = artifacts[0]["parts"]
            return parts[0].get("text", str(parts[0]))
        except (KeyError, IndexError):
            return str(data)

    async def health(self) -> dict[str, Any]:
        r = await self._http.get("/health")
        r.raise_for_status()
        return r.json()

    async def __aenter__(self) -> "A2AClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._http.aclose()
