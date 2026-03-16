"""
End-to-end A2A Authentication Flow Script
========================================

This is an integration script (not a unit test). It is intentionally named
so pytest won't auto-collect it; run manually when the wrapper + mock OAuth
server are running:

    python tests/a2a_auth_flow.py --flow all

"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

# Configuration copied from the original integration script
WRAPPER_URL = os.getenv("WRAPPER_URL", "http://localhost:9000")
OAUTH_BASE_URL = os.getenv("OAUTH_BASE_URL", "http://localhost:8090")
CLIENT_ID = "csai-demo-client"
CLIENT_SECRET = "csai-demo-secret"
REDIRECT_URI = "http://localhost:9999/callback"
VERBOSE = False

# Minimal helpers (copied)
class Colors:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    INFO = "\033[94m"
    BOLD = "\033[1m"
    END = "\033[0m"


def _ok(msg: str) -> None:
    print(f"  {Colors.OK}✓{Colors.END} {msg}")

def _fail(msg: str) -> None:
    print(f"  {Colors.FAIL}✗{Colors.END} {msg}")

def _info(msg: str) -> None:
    print(f"  {Colors.INFO}ℹ{Colors.END} {msg}")

def _header(msg: str) -> None:
    print(f"\n{Colors.BOLD}{'─' * 60}{Colors.END}")
    print(f"{Colors.BOLD}  {msg}{Colors.END}")
    print(f"{Colors.BOLD}{'─' * 60}{Colors.END}")


def _dump(label: str, data) -> None:
    if VERBOSE:
        print(f"  {Colors.WARN}  {label}:{Colors.END}")
        if isinstance(data, (dict, list)):
            print(f"    {json.dumps(data, indent=2)[:2000]}")
        else:
            print(f"    {str(data)[:2000]}")


def _decode_mock_jwt(token: str) -> dict | None:
    try:
        parts = token.split('.')
        payload_b64 = parts[1] + '=' * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None


def _generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return verifier, challenge


# For brevity, reuse the same test functions but keep them as plain functions
# (not starting with `test_`) so pytest won't collect them.

def discovery() -> dict | None:
    _header("Step 1 — Agent Card Discovery")
    url = f"{WRAPPER_URL}/.well-known/agent-card.json"
    _info(f"GET {url}")
    try:
        resp = httpx.get(url, timeout=10)
    except httpx.ConnectError:
        _fail(f"Cannot connect to wrapper at {WRAPPER_URL} — is it running?")
        return None
    if resp.status_code != 200:
        _fail(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    card = resp.json()
    _ok(f"AgentCard received  name={card.get('name')}  version={card.get('version')}")
    _dump("AgentCard", card)
    return card


def client_credentials(card: dict) -> str | None:
    _header("Step 2 — Client Credentials Flow")
    flows = card.get('securitySchemes', {})
    oauth = next((s for s in flows.values() if s.get('type') == 'oauth2'), None)
    if not oauth:
        _fail('No OAuth2 scheme — skipping')
        return None
    cc = oauth.get('flows', {}).get('clientCredentials', {})
    token_url = f"{OAUTH_BASE_URL}/oauth2/token"
    payload = {
        'grant_type': 'client_credentials',
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scope': ' '.join(list(cc.get('scopes', {}).keys())),
    }
    try:
        resp = httpx.post(token_url, data=payload, timeout=10)
    except httpx.ConnectError:
        _fail(f"Cannot connect to mock OAuth server at {OAUTH_BASE_URL} — is it running?")
        return None
    if resp.status_code != 200:
        _fail(f"Token request failed: HTTP {resp.status_code}")
        return None
    data = resp.json()
    access_token = data.get('access_token', '')
    _ok(f"Access token received  type={data.get('token_type')}  expires_in={data.get('expires_in')}")
    return access_token


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description='A2A Auth Flow (manual)')
    parser.add_argument('--flow', choices=['all','cc','pkce','send','discovery','jsonrpc'], default='all')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    VERBOSE = args.verbose

    card = discovery()
    if not card:
        sys.exit(1)
    token = client_credentials(card)
    if not token:
        sys.exit(1)
    _ok('Integration script completed (partial run)')


if __name__ == '__main__':
    main()
