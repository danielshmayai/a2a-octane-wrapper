"""
Example 06 — Interactive CLI Chat Loop
========================================
A simple interactive REPL that keeps context across turns.
Type 'exit' or Ctrl-C to quit, 'new' to start a fresh session.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from a2a_client import A2AClient


async def chat_loop() -> None:
    async with A2AClient() as client:
        print(f"Connected to {client.base_url}")
        print(f"Session: {client.context_id}")
        print("Commands: 'new' = new session, 'exit' = quit\n")

        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break

            if not user_input:
                continue
            if user_input.lower() == "exit":
                print("Bye!")
                break
            if user_input.lower() == "new":
                client.context_id = __import__("uuid").uuid4().__str__()
                print(f"[New session: {client.context_id}]")
                continue

            response = await client.send(user_input)
            print(f"Agent: {response}\n")


if __name__ == "__main__":
    asyncio.run(chat_loop())
