"""
Example 04 — Batch Queries (Concurrent Sessions)
==================================================
Sends multiple independent queries in parallel, each with its own session.
"""
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from a2a_client import A2AClient, DEFAULT_URL

QUERIES = [
    "List all open defects",
    "Get the 5 most recent incidents",
    "Tell me a joke",
    "What tools do you have available?",
]


async def run_query(index: int, query: str) -> tuple[int, str, str]:
    """Run a single query in its own session and return (index, query, response)."""
    async with A2AClient() as client:
        response = await client.send(query, new_session=True)
        return index, query, response


async def main() -> None:
    print(f"Running {len(QUERIES)} queries concurrently against {DEFAULT_URL}\n")
    start = time.perf_counter()

    tasks = [run_query(i, q) for i, q in enumerate(QUERIES)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = time.perf_counter() - start
    print(f"Completed in {elapsed:.2f}s\n{'=' * 60}")

    for result in results:
        if isinstance(result, Exception):
            print(f"ERROR: {result}")
        else:
            idx, query, response = result
            print(f"\n[{idx + 1}] Query : {query}")
            print(f"     Answer: {response[:200]}{'...' if len(response) > 200 else ''}")


if __name__ == "__main__":
    asyncio.run(main())
