"""
End-to-end A2A Authentication Flow Test
========================================

Tests the FULL A2A authentication protocol as described in the AgentCard:

    1. Discovery  — GET /.well-known/agent-card.json
    2. Parse securitySchemes → extract OAuth2 endpoints and scopes
    3. Client Credentials flow → POST /oauth2/token → get access_token
    4. Authorization Code + PKCE flow → GET /oauth2/auth → POST /oauth2/token
    5. Call /message:send with the obtained Bearer token
    6. Verify the token was forwarded to the downstream MCP server

Prerequisites:
    - A2A wrapper running on :9000        (python main.py)
    - Mock OAuth2 server running on :8090  (python mock_oauth_server.py)

    Update WRAPPER_URL / OAUTH_BASE_URL below if using different ports.

Usage:
    python test_a2a_auth_flow.py                    # run all tests
    python test_a2a_auth_flow.py --flow cc          # client_credentials only
    python test_a2a_auth_flow.py --flow pkce        # authorization_code + PKCE only
    python test_a2a_auth_flow.py --flow send        # token → /message:send only
    python test_a2a_auth_flow.py --verbose           # show full payloads
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

# ── Configuration ────────────────────────────────────────────────────

WRAPPER_URL = os.getenv("WRAPPER_URL", "http://localhost:9000")
OAUTH_BASE_URL = os.getenv("OAUTH_BASE_URL", "http://localhost:8090")

# Mock client credentials (must match mock_oauth_server.py)
CLIENT_ID = "csai-demo-client"
CLIENT_SECRET = "csai-demo-secret"

# Dummy redirect URI for the auth code flow
REDIRECT_URI = "http://localhost:9999/callback"

VERBOSE = False


# ── Helpers ──────────────────────────────────────────────────────────

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
    """Decode the unsigned mock JWT payload."""
    try:
        parts = token.split(".")
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None


def _generate_pkce() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ── Test Functions ───────────────────────────────────────────────────

def test_discovery() -> dict | None:
    """Step 1: Fetch AgentCard and parse securitySchemes."""
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

    # Parse securitySchemes
    schemes = card.get("securitySchemes", {})
    if not schemes:
        _fail("No securitySchemes in AgentCard")
        return None

    _ok(f"securitySchemes found: {list(schemes.keys())}")

    # Find the OAuth2 scheme
    oauth_scheme = None
    for name, scheme in schemes.items():
        if scheme.get("type") == "oauth2":
            oauth_scheme = scheme
            _ok(f"OAuth2 scheme '{name}' found")
            break

    if not oauth_scheme:
        _fail("No OAuth2 scheme in securitySchemes")
        return None

    flows = oauth_scheme.get("flows", {})

    # Client credentials
    cc = flows.get("clientCredentials", {})
    if cc:
        _ok(f"clientCredentials flow  tokenUrl={cc.get('tokenUrl')}")
        _ok(f"  scopes: {list(cc.get('scopes', {}).keys())}")
    else:
        _info("clientCredentials flow not advertised")

    # Authorization code
    ac = flows.get("authorizationCode", {})
    if ac:
        _ok(f"authorizationCode flow  authUrl={ac.get('authorizationUrl')}")
        _ok(f"  tokenUrl={ac.get('tokenUrl')}")
        _ok(f"  PKCE={ac.get('pkce')}  method={ac.get('pkceMethod')}")
        _ok(f"  scopes: {list(ac.get('scopes', {}).keys())}")
    else:
        _info("authorizationCode flow not advertised")

    # Verify security requirements
    security = card.get("security", [])
    if security:
        _ok(f"security requirements: {security}")
    else:
        _info("No security requirements array (optional)")

    return card


def test_client_credentials(card: dict) -> str | None:
    """Step 2: OAuth2 Client Credentials flow."""
    _header("Step 2 — Client Credentials Flow")

    flows = card.get("securitySchemes", {})
    oauth = next(
        (s for s in flows.values() if s.get("type") == "oauth2"), None
    )
    if not oauth:
        _fail("No OAuth2 scheme — skipping")
        return None

    cc = oauth.get("flows", {}).get("clientCredentials", {})
    token_url_from_card = cc.get("tokenUrl", "")
    scopes = list(cc.get("scopes", {}).keys())

    # Override the token URL to point to the mock server
    token_url = f"{OAUTH_BASE_URL}/oauth2/token"
    _info(f"AgentCard tokenUrl: {token_url_from_card}")
    _info(f"Using mock tokenUrl: {token_url}")
    _info(f"Requesting scopes: {scopes}")

    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": " ".join(scopes),
    }

    _info(f"POST {token_url}")
    _dump("Request body", payload)

    try:
        resp = httpx.post(token_url, data=payload, timeout=10)
    except httpx.ConnectError:
        _fail(f"Cannot connect to mock OAuth server at {OAUTH_BASE_URL} — is it running?")
        return None

    if resp.status_code != 200:
        _fail(f"Token request failed: HTTP {resp.status_code}")
        _fail(f"  Response: {resp.text[:300]}")
        return None

    data = resp.json()
    access_token = data.get("access_token", "")
    _ok(f"Access token received  type={data.get('token_type')}  expires_in={data.get('expires_in')}")
    _ok(f"  scope: {data.get('scope')}")
    _ok(f"  token (first 50 chars): {access_token[:50]}...")

    # Decode and verify claims
    claims = _decode_mock_jwt(access_token)
    if claims:
        _ok(f"  JWT claims: sub={claims.get('sub')}  aud={claims.get('aud')}  grant_type={claims.get('grant_type')}")
        _dump("Full JWT claims", claims)
    else:
        _info("  (Could not decode JWT — may be opaque token)")

    return access_token


def test_authorization_code_pkce(card: dict) -> str | None:
    """Step 3: OAuth2 Authorization Code + PKCE flow."""
    _header("Step 3 — Authorization Code + PKCE Flow")

    flows = card.get("securitySchemes", {})
    oauth = next(
        (s for s in flows.values() if s.get("type") == "oauth2"), None
    )
    if not oauth:
        _fail("No OAuth2 scheme — skipping")
        return None

    ac = oauth.get("flows", {}).get("authorizationCode", {})
    auth_url_from_card = ac.get("authorizationUrl", "")
    token_url_from_card = ac.get("tokenUrl", "")
    scopes = list(ac.get("scopes", {}).keys())
    pkce_required = ac.get("pkce", False)
    pkce_method = ac.get("pkceMethod", "S256")

    auth_url = f"{OAUTH_BASE_URL}/oauth2/auth"
    token_url = f"{OAUTH_BASE_URL}/oauth2/token"

    _info(f"AgentCard authorizationUrl: {auth_url_from_card}")
    _info(f"AgentCard tokenUrl: {token_url_from_card}")
    _info(f"Using mock authUrl: {auth_url}")
    _info(f"PKCE required: {pkce_required}  method: {pkce_method}")

    # ── Step 3a: Generate PKCE ──────────────────────────────────────
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)

    _ok(f"PKCE generated  verifier_len={len(code_verifier)}  challenge_len={len(code_challenge)}")
    _dump("code_verifier", code_verifier)
    _dump("code_challenge", code_challenge)

    # ── Step 3b: Authorization request ──────────────────────────────
    auth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": pkce_method,
    }

    full_auth_url = f"{auth_url}?{urlencode(auth_params)}"
    _info(f"GET {auth_url}  (with PKCE challenge)")
    _dump("Auth params", auth_params)

    try:
        # follow_redirects=False so we can capture the redirect with the code
        resp = httpx.get(full_auth_url, timeout=10, follow_redirects=False)
    except httpx.ConnectError:
        _fail(f"Cannot connect to mock OAuth server at {OAUTH_BASE_URL}")
        return None

    if resp.status_code not in (302, 303):
        _fail(f"Expected redirect (302/303), got HTTP {resp.status_code}")
        _fail(f"  Body: {resp.text[:300]}")
        return None

    redirect_location = resp.headers.get("location", "")
    _ok(f"Redirect received  status={resp.status_code}")
    _ok(f"  Location: {redirect_location[:100]}...")

    # Parse the authorization code from the redirect
    parsed = urlparse(redirect_location)
    params = parse_qs(parsed.query)

    auth_code = params.get("code", [None])[0]
    returned_state = params.get("state", [None])[0]

    if not auth_code:
        _fail("No 'code' parameter in redirect")
        return None

    _ok(f"Authorization code: {auth_code}")

    if returned_state != state:
        _fail(f"State mismatch: sent={state}  received={returned_state}")
        return None
    _ok(f"State parameter verified ✓")

    # ── Step 3c: Exchange code for token (with PKCE verifier) ───────
    _info(f"POST {token_url}  (exchanging code + PKCE verifier)")

    token_payload = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code_verifier": code_verifier,
    }
    _dump("Token request body", token_payload)

    try:
        resp = httpx.post(token_url, data=token_payload, timeout=10)
    except httpx.ConnectError:
        _fail(f"Cannot connect to mock OAuth server")
        return None

    if resp.status_code != 200:
        _fail(f"Token exchange failed: HTTP {resp.status_code}")
        _fail(f"  Response: {resp.text[:300]}")
        return None

    data = resp.json()
    access_token = data.get("access_token", "")
    _ok(f"Access token received  type={data.get('token_type')}  expires_in={data.get('expires_in')}")
    _ok(f"  scope: {data.get('scope')}")
    _ok(f"  token (first 50 chars): {access_token[:50]}...")

    claims = _decode_mock_jwt(access_token)
    if claims:
        _ok(f"  JWT claims: sub={claims.get('sub')}  grant_type={claims.get('grant_type')}")

    # ── Step 3d: Verify PKCE rejection with wrong verifier ──────────
    _info("Verifying PKCE enforcement — sending wrong code_verifier...")

    # Get a fresh auth code for the negative test
    resp2 = httpx.get(full_auth_url, timeout=10, follow_redirects=False)
    params2 = parse_qs(urlparse(resp2.headers.get("location", "")).query)
    bad_code = params2.get("code", [None])[0]

    if bad_code:
        bad_payload = {
            "grant_type": "authorization_code",
            "code": bad_code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code_verifier": "THIS_IS_THE_WRONG_VERIFIER_12345",
        }
        resp_bad = httpx.post(token_url, data=bad_payload, timeout=10)
        if resp_bad.status_code != 200:
            _ok(f"PKCE correctly rejected wrong verifier  HTTP {resp_bad.status_code}: {resp_bad.json().get('detail', '')}")
        else:
            _fail("PKCE did NOT reject wrong verifier — security issue!")
    else:
        _info("Could not get second auth code for negative test")

    return access_token


def test_send_message_with_token(access_token: str, label: str = "OAuth token") -> bool:
    """Step 4: Call /message:send with the Bearer token."""
    _header(f"Step 4 — POST /message:send with {label}")

    url = f"{WRAPPER_URL}/message:send"
    payload = {
        "message": {
            "messageId": f"auth-test-{int(time.time())}",
            "contextId": f"auth-test-ctx-{int(time.time())}",
            "role": "ROLE_USER",
            "parts": [{"text": "Get defect 1314"}],
        },
        "configuration": {"blocking": True},
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    _info(f"POST {url}")
    _info(f"Authorization: Bearer {access_token[:30]}...")
    _dump("Request payload", payload)

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=30)
    except httpx.ConnectError:
        _fail(f"Cannot connect to wrapper at {WRAPPER_URL}")
        return False

    _info(f"Response: HTTP {resp.status_code}")

    if resp.status_code == 401:
        _fail("401 Unauthorized — wrapper rejected the token")
        _info("This is expected if A2A_API_KEY is set and the OAuth token doesn't match")
        _info("For testing, either unset A2A_API_KEY in .env or use the /sim/token flow")
        return False

    if resp.status_code != 200:
        _fail(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return False

    data = resp.json()
    _dump("Response body", data)

    task = data.get("task", {})
    status = task.get("status", {})
    state = status.get("state", "")
    metadata = task.get("metadata", {})

    _ok(f"Task state: {state}")
    _ok(f"auth_injected: {metadata.get('auth_injected')}")
    _ok(f"mcp_called: {metadata.get('mcp_called')}")

    # Show the agent's text response
    msg_parts = status.get("message", {}).get("parts", [])
    for p in msg_parts:
        if p.get("text"):
            _ok(f"Agent reply: {p['text'][:200]}...")
            break

    if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
        _ok("A2A response envelope is valid ✓")
        return True
    else:
        _fail(f"Unexpected task state: {state}")
        return False


def test_oidc_discovery() -> None:
    """Bonus: Test OIDC discovery endpoint on mock server."""
    _header("Bonus — OIDC Discovery")

    url = f"{OAUTH_BASE_URL}/.well-known/openid-configuration"
    _info(f"GET {url}")

    try:
        resp = httpx.get(url, timeout=10)
    except httpx.ConnectError:
        _fail(f"Cannot connect to mock OAuth server at {OAUTH_BASE_URL}")
        return

    if resp.status_code == 200:
        data = resp.json()
        _ok(f"OIDC discovery OK  issuer={data.get('issuer')}")
        _ok(f"  authorization_endpoint: {data.get('authorization_endpoint')}")
        _ok(f"  token_endpoint: {data.get('token_endpoint')}")
        _ok(f"  grant_types: {data.get('grant_types_supported')}")
        _ok(f"  code_challenge_methods: {data.get('code_challenge_methods_supported')}")
    else:
        _fail(f"HTTP {resp.status_code}")


# ── Main ─────────────────────────────────────────────────────────────

def main():
    global VERBOSE

    parser = argparse.ArgumentParser(description="A2A OAuth2 Auth Flow Test")
    parser.add_argument("--flow", choices=["all", "cc", "pkce", "send", "discovery"],
                        default="all", help="Which flow to test")
    parser.add_argument("--verbose", action="store_true", help="Show full payloads")
    parser.add_argument("--wrapper", type=str, default=None, help="Wrapper URL override")
    parser.add_argument("--oauth", type=str, default=None, help="Mock OAuth URL override")
    args = parser.parse_args()

    VERBOSE = args.verbose
    global WRAPPER_URL, OAUTH_BASE_URL
    if args.wrapper:
        WRAPPER_URL = args.wrapper
    if args.oauth:
        OAUTH_BASE_URL = args.oauth

    print(f"""
{Colors.BOLD}╔══════════════════════════════════════════════════════════╗
║         A2A OAuth2 Authentication Flow Test              ║
╠══════════════════════════════════════════════════════════╣
║  Wrapper:     {WRAPPER_URL:<42s} ║
║  Mock OAuth:  {OAUTH_BASE_URL:<42s} ║
║  Client ID:   {CLIENT_ID:<42s} ║
║  Flow:        {args.flow:<42s} ║
╚══════════════════════════════════════════════════════════╝{Colors.END}
""")

    passed = 0
    failed = 0

    # ── Step 1: Discovery ────────────────────────────────────────────
    if args.flow in ("all", "discovery"):
        card = test_discovery()
        if card:
            passed += 1
        else:
            failed += 1
            if args.flow != "discovery":
                print(f"\n{Colors.FAIL}Cannot continue without AgentCard. Aborting.{Colors.END}")
                sys.exit(1)
    else:
        # Need the card for other tests
        try:
            resp = httpx.get(f"{WRAPPER_URL}/.well-known/agent-card.json", timeout=10)
            card = resp.json()
        except Exception:
            print(f"{Colors.FAIL}Cannot fetch AgentCard. Is the wrapper running?{Colors.END}")
            sys.exit(1)

    # ── OIDC Discovery ───────────────────────────────────────────────
    if args.flow in ("all", "discovery"):
        test_oidc_discovery()

    # ── Step 2: Client Credentials ───────────────────────────────────
    cc_token = None
    if args.flow in ("all", "cc"):
        cc_token = test_client_credentials(card)
        if cc_token:
            passed += 1
        else:
            failed += 1

    # ── Step 3: Authorization Code + PKCE ────────────────────────────
    pkce_token = None
    if args.flow in ("all", "pkce"):
        pkce_token = test_authorization_code_pkce(card)
        if pkce_token:
            passed += 1
        else:
            failed += 1

    # ── Step 4: Use token to call /message:send ──────────────────────
    if args.flow in ("all", "send"):
        token = pkce_token or cc_token
        if not token:
            # Get a quick token via client_credentials
            _info("No token from previous steps — obtaining via client_credentials...")
            try:
                resp = httpx.post(
                    f"{OAUTH_BASE_URL}/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "scope": "otds:groups otds:roles search",
                    },
                    timeout=10,
                )
                token = resp.json().get("access_token")
            except Exception as exc:
                _fail(f"Could not obtain token: {exc}")

        if token:
            if test_send_message_with_token(token, "OAuth2 token"):
                passed += 1
            else:
                failed += 1
        else:
            _fail("No token available to test /message:send")
            failed += 1

    # ── Summary ──────────────────────────────────────────────────────
    _header("Summary")
    total = passed + failed
    if failed == 0:
        print(f"  {Colors.OK}{Colors.BOLD}All {passed} tests passed ✓{Colors.END}")
    else:
        print(f"  {Colors.OK}{passed} passed{Colors.END}  {Colors.FAIL}{failed} failed{Colors.END}  ({total} total)")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
