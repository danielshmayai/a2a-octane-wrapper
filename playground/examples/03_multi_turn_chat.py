"""
Example 03 — Multi-Turn Conversation
======================================
Demonstrates how context_id preserves conversation history
across multiple requests in the same session.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from a2a_client import A2AClient


CONVERSATION = [
    "Get defect 2110",
    "What is the status of that defect?",
    "Who is the assignee?",
    "Update the description to 'Reproduced on v3.2.1'",
]


async def main() -> None:
    async with A2AClient() as client:
        print(f"Session ID: {client.context_id}\n")
        print("=" * 60)

        for turn, message in enumerate(CONVERSATION, 1):
            print(f"\n[Turn {turn}] User: {message}")
            response = await client.send(message)
            print(f"[Turn {turn}] Agent: {response}")
            print("-" * 60)

        print("\nDone — all turns used the same context_id (session memory preserved).")


if __name__ == "__main__":
    asyncio.run(main())
