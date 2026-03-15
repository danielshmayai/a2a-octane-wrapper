"""
Example 02 — Send a Single Message
====================================
Usage:
    python 02_send_message.py "Get defect 2110"
    python 02_send_message.py "List all open incidents"
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from a2a_client import A2AClient


async def main(text: str) -> None:
    async with A2AClient() as client:
        print(f"Sending: {text!r}\n")
        response = await client.send(text)
        print("Response:")
        print(response)


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Tell me a joke"
    asyncio.run(main(query))
