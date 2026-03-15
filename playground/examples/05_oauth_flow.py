"""
Example 05 — OAuth2 Authorization Flow
========================================
Shows how to obtain a bearer token from the mock OAuth server
and use it to authenticate requests to the wrapper.

Prerequisites:
    The mock OAuth server must be running:
        python mock_oauth_server.py  (from the wrapper repo)
"""
import asyncio
import sys
import os

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from a2a_client import DEFAULT_URL

OAUTH_URL = os.getenv("OAUTH_SERVER_URL", "http://localhost:9001")
CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "playground-client")
CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "playground-secret")


async def get_token() -> str:
    """Fetch an access token via client_credentials grant."""
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"{OAUTH_URL}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": "a2a:read a2a:write",
            },
        )
        r.raise_for_status()
        return r.json()["access_token"]


async def main() -> None:
    print("Step 1: Obtain access token via client_credentials grant...")
    token = await get_token()
    print(f"  Token obtained: {token[:40]}...\n")

    print("Step 2: Call the wrapper with the bearer token...")
    async with httpx.AsyncClient(
        base_url=DEFAULT_URL,
        headers={"Authorization": f"Bearer {token}"},
    ) as http:
        r = await http.get("/.well-known/agent-card.json")
        r.raise_for_status()
        print(f"  AgentCard name: {r.json().get('name', 'n/a')}")
        print("  Authentication succeeded!")


if __name__ == "__main__":
    asyncio.run(main())
