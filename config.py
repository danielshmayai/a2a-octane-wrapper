"""
Configuration module.

Loads Opentext SDP MCP server connection details and A2A wrapper settings
from environment variables (with .env file support).
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Opentext SDP MCP Server Connection ---
OCTANE_BASE_URL: str = os.getenv("OCTANE_BASE_URL", "http://localhost:8080").rstrip("/")
OCTANE_MCP_ENDPOINT: str = f"{OCTANE_BASE_URL}/mcp"
API_KEY: str = os.getenv("API_KEY", "")

# --- Opentext SDP Context (injected into every MCP tool call) ---
DEFAULT_SHARED_SPACE_ID: int = int(os.getenv("DEFAULT_SHARED_SPACE_ID", "1001"))
DEFAULT_WORKSPACE_ID: int = int(os.getenv("DEFAULT_WORKSPACE_ID", "1002"))

# --- Gemini Agent ---
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_ENABLED: bool = True   # toggled at runtime via /config

# --- A2A Wrapper Settings ---
A2A_HOST: str = os.getenv("A2A_HOST", "0.0.0.0")
A2A_PORT: int = int(os.getenv("A2A_PORT", "9000"))
AGENT_VERSION: str = "0.1.0"
# Inbound bearer token — leave empty to disable auth (dev/local use only)
A2A_API_KEY: str = os.getenv("A2A_API_KEY", "")

# --- Agent Identity & OAuth2 ---
# Public URL of this agent (used in the AgentCard)
AGENT_URL: str = os.getenv("AGENT_URL", "https://csai-a2a-agent.dev.ca.opentext.com")
AGENT_NAME: str = os.getenv("AGENT_NAME", "CSAI Agent")
# OTDS OAuth2 endpoints
OAUTH2_AUTH_URL: str = os.getenv(
    "OAUTH2_AUTH_URL", "https://otdsauth.dev.ca.opentext.com/oauth2/auth"
)
OAUTH2_TOKEN_URL: str = os.getenv(
    "OAUTH2_TOKEN_URL", "https://otdsauth.dev.ca.opentext.com/oauth2/token"
)

# --- MCP discovery polling (seconds). Set to 0 to disable periodic polling.
# Default: once per day (86400 seconds). Set env var to override.
MCP_TOOL_POLL_INTERVAL_SECONDS: int = int(os.getenv("MCP_TOOL_POLL_INTERVAL_SECONDS", "86400"))
# --- Timeouts ---
MCP_REQUEST_TIMEOUT_SECONDS: int = int(os.getenv("MCP_REQUEST_TIMEOUT_SECONDS", "30"))
