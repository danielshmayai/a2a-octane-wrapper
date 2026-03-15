# A2A Octane Playground

A hands-on playground for experimenting with the [A2A Octane Wrapper](https://github.com/danielshmayai/a2a-octane-wrapper) — a service that bridges the **Google Agent-to-Agent (A2A) protocol** with the **Opentext SDP MCP Server**.

## Quick Start

### Prerequisites

- Python 3.11+
- A running instance of the A2A Octane Wrapper (`python main.py` in the wrapper repo)
- `httpx` installed: `pip install httpx`

### 1. Clone & install

```bash
git clone https://github.com/danielshmayai/a2a-octane-playground.git
cd a2a-octane-playground
pip install -r requirements.txt
```

### 2. Configure

Copy the example env file and fill in your wrapper URL:

```bash
cp .env.example .env
# Edit .env: set A2A_WRAPPER_URL=http://localhost:9000
```

### 3. Run examples

```bash
# Discover the agent card
python examples/01_discover_agent.py

# Send a single message
python examples/02_send_message.py "List open incidents"

# Multi-turn conversation
python examples/03_multi_turn_chat.py

# Batch queries
python examples/04_batch_queries.py
```

## Examples Overview

| File | What it does |
|------|-------------|
| `01_discover_agent.py` | Fetches and displays the AgentCard |
| `02_send_message.py` | Sends a single message and prints the response |
| `03_multi_turn_chat.py` | Demonstrates multi-turn conversation with context |
| `04_batch_queries.py` | Runs multiple queries and collects results |
| `05_oauth_flow.py` | Shows the OAuth2 authorization flow |
| `06_streaming_chat.py` | Interactive CLI chat loop |

## Project Structure

```
playground/
├── README.md
├── requirements.txt
├── .env.example
├── a2a_client.py          # Reusable A2A client wrapper
├── examples/
│   ├── 01_discover_agent.py
│   ├── 02_send_message.py
│   ├── 03_multi_turn_chat.py
│   ├── 04_batch_queries.py
│   ├── 05_oauth_flow.py
│   └── 06_streaming_chat.py
└── notebooks/
    └── exploration.ipynb
```

## A2A Protocol Basics

The wrapper exposes two main endpoints:

```
GET  /.well-known/agent-card.json  → Discover agent capabilities
POST /message:send                 → Send a message and get a response
```

### Message format

```json
{
  "message": {
    "messageId": "<uuid>",
    "contextId": "<session-id>",
    "role": "ROLE_USER",
    "parts": [{"text": "Your question here"}]
  },
  "configuration": {"blocking": true}
}
```

Reuse the same `contextId` to maintain conversation history across requests.
