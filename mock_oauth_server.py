"""
Mock OAuth2 Authorization Server for A2A auth flow testing.

Implements the two flows advertised in the AgentCard's securitySchemes:

  1. Client Credentials  — POST /oauth2/token  (grant_type=client_credentials)
  2. Authorization Code  — GET  /oauth2/auth   (redirects with ?code=...)
                           POST /oauth2/token  (grant_type=authorization_code + PKCE)

Also exposes:
  GET  /.well-known/openid-configuration   — OIDC discovery (optional)
  GET  /oauth2/introspect                  — token introspection (debug helper)

Usage:
    python mock_oauth_server.py              # starts on :8090
    python mock_oauth_server.py --port 7777  # custom port

The tokens are deterministic JWTs (unsigned, base64-encoded JSON) so you can
decode them in the test script or browser to verify claims.

Accepted client credentials (hardcoded for PoC):
    client_id:     csai-demo-client
    client_secret: csai-demo-secret
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import time
import uuid
from urllib.parse import urlencode, urlparse, parse_qs

import uvicorn
from fastapi import FastAPI, Form, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("mock-oauth")

app = FastAPI(title="Mock OAuth2 Server", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Configuration ────────────────────────────────────────────────────

VALID_CLIENT_ID = "csai-demo-client"
VALID_CLIENT_SECRET = "csai-demo-secret"
TOKEN_TTL = 3600  # 1 hour
ISSUER = "http://localhost:8090"

# Advertised scopes
SCOPES = {
    "otds:groups": "Access to groups",
    "otds:roles": "Access to roles",
    "search": "Access to search",
}

# In-memory stores
_auth_codes: dict[str, dict] = {}   # code → {client_id, redirect_uri, scopes, code_challenge, code_challenge_method, expires}
_tokens: dict[str, dict] = {}       # access_token → {claims}


# ── Helpers ──────────────────────────────────────────────────────────

def _make_token(
    client_id: str,
    scopes: list[str],
    grant_type: str,
    subject: str = "demo-user@opentext.com",
) -> str:
    """Create a deterministic mock JWT (unsigned, three base64 segments)."""
    now = int(time.time())
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "iss": ISSUER,
        "sub": subject,
        "aud": client_id,
        "exp": now + TOKEN_TTL,
        "iat": now,
        "jti": str(uuid.uuid4()),
        "scope": " ".join(scopes),
        "grant_type": grant_type,
    }

    def _b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    token = f"{_b64(header)}.{_b64(payload)}."
    _tokens[token] = payload
    return token


def _validate_client(client_id: str, client_secret: str) -> None:
    if client_id != VALID_CLIENT_ID or client_secret != VALID_CLIENT_SECRET:
        raise HTTPException(
            status_code=401,
            detail="invalid_client",
            headers={"WWW-Authenticate": "Basic"},
        )


def _validate_pkce(
    code_verifier: str,
    code_challenge: str,
    method: str = "S256",
) -> bool:
    """Verify PKCE code_verifier against the stored code_challenge."""
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return computed == code_challenge
    elif method == "plain":
        return code_verifier == code_challenge
    return False


# ── OIDC Discovery ───────────────────────────────────────────────────

@app.get("/.well-known/openid-configuration")
async def oidc_discovery(request: Request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth2/auth",
        "token_endpoint": f"{base}/oauth2/token",
        "introspection_endpoint": f"{base}/oauth2/introspect",
        "scopes_supported": list(SCOPES.keys()),
        "response_types_supported": ["code"],
        "grant_types_supported": ["client_credentials", "authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
    })


# ── Authorization Endpoint (GET /oauth2/auth) ───────────────────────

@app.get("/oauth2/auth")
async def authorize(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str = Query(""),
    state: str = Query(""),
    code_challenge: str = Query(None),
    code_challenge_method: str = Query("S256"),
):
    """
    Authorization Code flow — Step 1.

    In a real IdP this would show a login/consent page. The mock
    auto-approves immediately and redirects back with an auth code.
    """
    logger.info(
        "AUTH request  client_id=%s  redirect_uri=%s  scopes=%s  pkce=%s",
        client_id, redirect_uri, scope, bool(code_challenge),
    )

    if response_type != "code":
        raise HTTPException(400, f"unsupported response_type: {response_type}")

    if client_id != VALID_CLIENT_ID:
        raise HTTPException(400, "invalid client_id")

    # Generate auth code
    code = f"authcode-{uuid.uuid4().hex[:16]}"
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scopes": scope.split(),
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "expires": time.time() + 300,  # 5 min
    }

    # Build redirect URL with code and state
    params = {"code": code}
    if state:
        params["state"] = state

    separator = "&" if "?" in redirect_uri else "?"
    redirect_url = f"{redirect_uri}{separator}{urlencode(params)}"

    logger.info("AUTH → redirect  code=%s  to=%s", code, redirect_url)
    return RedirectResponse(url=redirect_url, status_code=302)


# ── Token Endpoint (POST /oauth2/token) ─────────────────────────────

@app.post("/oauth2/token")
async def token(
    grant_type: str = Form(...),
    client_id: str = Form(None),
    client_secret: str = Form(None),
    scope: str = Form(""),
    code: str = Form(None),
    redirect_uri: str = Form(None),
    code_verifier: str = Form(None),
):
    """
    Token endpoint — handles both grant types:
      - client_credentials: client authenticates with id+secret, gets a token
      - authorization_code: exchanges auth code (+ PKCE verifier) for a token
    """
    logger.info("TOKEN request  grant_type=%s  client_id=%s", grant_type, client_id)

    # ── Client Credentials ──────────────────────────────────────────
    if grant_type == "client_credentials":
        if not client_id or not client_secret:
            raise HTTPException(400, "client_id and client_secret required")
        _validate_client(client_id, client_secret)

        requested_scopes = scope.split() if scope else list(SCOPES.keys())
        access_token = _make_token(client_id, requested_scopes, "client_credentials")

        logger.info("TOKEN → client_credentials OK  scopes=%s", requested_scopes)
        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL,
            "scope": " ".join(requested_scopes),
        })

    # ── Authorization Code ──────────────────────────────────────────
    if grant_type == "authorization_code":
        if not code:
            raise HTTPException(400, "code is required")

        stored = _auth_codes.pop(code, None)
        if not stored:
            raise HTTPException(400, "invalid or expired authorization code")

        if time.time() > stored["expires"]:
            raise HTTPException(400, "authorization code expired")

        if client_id and client_id != stored["client_id"]:
            raise HTTPException(400, "client_id mismatch")

        if redirect_uri and redirect_uri != stored["redirect_uri"]:
            raise HTTPException(400, "redirect_uri mismatch")

        # Validate PKCE if code_challenge was provided during /auth
        if stored.get("code_challenge"):
            if not code_verifier:
                raise HTTPException(400, "code_verifier is required (PKCE)")
            if not _validate_pkce(
                code_verifier,
                stored["code_challenge"],
                stored.get("code_challenge_method", "S256"),
            ):
                raise HTTPException(400, "PKCE verification failed — code_verifier does not match code_challenge")
            logger.info("TOKEN → PKCE verified OK  method=%s", stored["code_challenge_method"])

        # For auth code flow with PKCE, client_secret is optional
        # (public clients use PKCE instead of client_secret per RFC 7636)
        if client_secret:
            _validate_client(client_id or stored["client_id"], client_secret)
        elif not stored.get("code_challenge"):
            # No PKCE and no client_secret — reject
            raise HTTPException(400, "client_secret required (no PKCE was used)")

        access_token = _make_token(
            stored["client_id"],
            stored["scopes"],
            "authorization_code",
        )

        logger.info("TOKEN → authorization_code OK  scopes=%s", stored["scopes"])
        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL,
            "scope": " ".join(stored["scopes"]),
        })

    raise HTTPException(400, f"unsupported grant_type: {grant_type}")


# ── Token Introspection (debug helper) ───────────────────────────────

@app.get("/oauth2/introspect")
async def introspect(token_value: str = Query(..., alias="token")):
    """Decode and return the claims of a mock token (debug helper)."""
    claims = _tokens.get(token_value)
    if claims:
        active = time.time() < claims.get("exp", 0)
        return JSONResponse({"active": active, **claims})
    # Try to decode the JWT manually
    try:
        parts = token_value.split(".")
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return JSONResponse({"active": True, "decoded": True, **payload})
    except Exception:
        return JSONResponse({"active": False, "error": "unknown token"})


# ── Health check ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "mock-oauth-server",
        "valid_client_id": VALID_CLIENT_ID,
        "scopes": list(SCOPES.keys()),
    }


# ── Entrypoint ───────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock OAuth2 Server")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    print(f"""
  Mock OAuth2 Server for A2A Auth Testing
  ----------------------------------------
  Token endpoint:  http://localhost:{args.port}/oauth2/token
  Auth endpoint:   http://localhost:{args.port}/oauth2/auth
  OIDC discovery:  http://localhost:{args.port}/.well-known/openid-configuration
  Introspect:      http://localhost:{args.port}/oauth2/introspect

  Client ID:       {VALID_CLIENT_ID}
  Client Secret:   {VALID_CLIENT_SECRET}
  Scopes:          {', '.join(SCOPES.keys())}
""")

    uvicorn.run(app, host=args.host, port=args.port)
