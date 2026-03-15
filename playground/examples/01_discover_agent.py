"""
Example 01 — Discover Agent Card
=================================
Fetches the AgentCard from the wrapper and pretty-prints it.
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from a2a_client import A2AClient


async def main() -> None:
    async with A2AClient() as client:
        print(f"Connecting to: {client.base_url}")
        card = await client.agent_card()
        print("\n--- Agent Card ---")
        print(json.dumps(card, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
