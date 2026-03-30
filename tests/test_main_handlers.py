import asyncio
import sys
import pathlib
import pytest
import types

# Ensure repository root is on sys.path so tests can import local modules
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import a2a_models

# ── JSON-RPC helpers ────────────────────────────────────────────────

def _make_jsonrpc_body(method: str, params: dict, rpc_id=1) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}


def _make_message_params(text: str = "hello", context_id: str = "ctx-rpc") -> dict:
    return {
        "message": {
            "messageId": "msg-rpc-1",
            "role": "ROLE_USER",
            "parts": [{"text": text}],
            "contextId": context_id,
        }
    }


@pytest.mark.asyncio
async def test_handle_with_agent_sets_metadata(monkeypatch):
    import main

    async def fake_run(user_text, mcp, context_id, bearer_token):
        # return summary, artifacts, mcp_called
        artifact = a2a_models.Artifact(parts=[a2a_models.Part(text="raw")])
        return "summary text", [artifact], True

    monkeypatch.setattr(main, 'agent', types.SimpleNamespace(run=fake_run))

    res = await main._handle_with_agent('tid-1', 'ctx-1', 'hello', 'bearer-token')
    # res is a dict from TaskResponse.model_dump
    task = res['task']
    assert task['metadata']['mcp_called'] is True
    assert task['metadata']['auth_injected'] is True
    assert task['artifacts'] is not None


@pytest.mark.asyncio
async def test_handle_with_agent_no_mcp(monkeypatch):
    import main

    async def fake_run(user_text, mcp, context_id, bearer_token):
        return "just a joke", [], False

    monkeypatch.setattr(main, 'agent', types.SimpleNamespace(run=fake_run))

    res = await main._handle_with_agent('tid-2', 'ctx-2', 'tell me a joke', None)
    task = res['task']
    assert task['metadata']['mcp_called'] is False
    assert task['metadata']['auth_injected'] is False
    assert task.get('artifacts') is None


@pytest.mark.asyncio
async def test_handle_with_keywords_sets_metadata(monkeypatch):
    import main
    import tool_router

    # Temporarily register a tool so the keyword router can route to it.
    # We use get_entities (the current generic MCP tool name) and supply it
    # via the structured data part so resolve_intent is bypassed entirely.
    tool_router.TOOL_REGISTRY["get_entities"] = {
        "description": "test stub",
        "example_prompts": [],
        "default_arguments": {},
        "required": [],
    }
    try:
        part = a2a_models.Part(data={"tool": "get_entities"})
        user_msg = a2a_models.Message(role="ROLE_USER", parts=[part])

        async def fake_execute_tool(tool_name, arguments, mcp, bearer_token=None):
            return a2a_models.Artifact(parts=[a2a_models.Part(text="entity data")])

        monkeypatch.setattr(main, 'execute_tool', fake_execute_tool)

        res = await main._handle_with_keywords('tid-kw', 'ctx-kw', 'ignored', user_msg, 'the-token')
        task = res['task']
        assert task['metadata']['mcp_called'] is True
        assert task['metadata']['auth_injected'] is True
        assert task['artifacts'] is not None
    finally:
        tool_router.TOOL_REGISTRY.pop("get_entities", None)


# ── JSON-RPC endpoint tests ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_jsonrpc_message_send_success(monkeypatch):
    """POST / with method=message/send returns a valid JSON-RPC envelope."""
    import main
    from fastapi.testclient import TestClient

    async def fake_run(user_text, mcp, context_id, bearer_token):
        artifact = a2a_models.Artifact(parts=[a2a_models.Part(text="rpc result")])
        return "rpc summary", [artifact], True

    with TestClient(main.app) as client:
        monkeypatch.setattr(main, 'agent', types.SimpleNamespace(run=fake_run))  # after startup
        resp = client.post(
            "/",
            json=_make_jsonrpc_body("message/send", _make_message_params("get defect 42")),
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert "error" not in body or body.get("error") is None
    result = body["result"]
    task = result["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["metadata"]["mcp_called"] is True


@pytest.mark.asyncio
async def test_jsonrpc_message_send_no_agent(monkeypatch):
    """POST / with method=message/send falls back to keyword router when agent is None."""
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, 'agent', None)

    async def fake_execute_tool(tool_name, arguments, mcp, bearer_token=None):
        return a2a_models.Artifact(parts=[a2a_models.Part(text="keyword result")])

    monkeypatch.setattr(main, 'execute_tool', fake_execute_tool)

    params = _make_message_params()
    # embed tool name in parts data so keyword router finds it
    params["message"]["parts"] = [{"data": {"tool": "get_defect"}}]

    with TestClient(main.app) as client:
        resp = client.post("/", json=_make_jsonrpc_body("message/send", params))

    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    result = body["result"]
    task = result["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_jsonrpc_invalid_method():
    """POST / with an unknown method returns JSON-RPC method-not-found error."""
    import main
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        resp = client.post("/", json=_make_jsonrpc_body("foo/bar", {}))

    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == -32601
    assert "foo/bar" in body["error"]["message"]


@pytest.mark.asyncio
async def test_jsonrpc_not_yet_implemented_methods():
    """tasks/get and tasks/cancel return method-not-implemented, not 405."""
    import main
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        for method in ("tasks/get", "tasks/cancel", "tasks/resubscribe"):
            resp = client.post("/", json=_make_jsonrpc_body(method, {}, rpc_id=99))
            assert resp.status_code == 200, f"{method} returned HTTP {resp.status_code}"
            body = resp.json()
            assert body["jsonrpc"] == "2.0"
            assert body["id"] == 99
            assert body["error"]["code"] == -32601, f"Unexpected code for {method}"


@pytest.mark.asyncio
async def test_jsonrpc_parse_error():
    """Malformed JSON body returns JSON-RPC parse error."""
    import main
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        resp = client.post(
            "/",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_jsonrpc_invalid_request_missing_method():
    """Missing 'method' field returns JSON-RPC invalid-request error."""
    import main
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        resp = client.post("/", json={"jsonrpc": "2.0", "id": 2})

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_jsonrpc_wrong_version():
    """Wrong jsonrpc version returns JSON-RPC invalid-request error."""
    import main
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        resp = client.post("/", json={"jsonrpc": "1.0", "id": 3, "method": "message/send", "params": {}})

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_jsonrpc_bearer_token_passthrough(monkeypatch):
    """Authorization: Bearer header is forwarded as octane_token when no API_KEY override."""
    import main
    import config
    from fastapi.testclient import TestClient

    captured = {}

    async def fake_run(user_text, mcp, context_id, bearer_token):
        captured["bearer_token"] = bearer_token
        return "ok", [], False

    # Prevent startup from overwriting agent with a real GeminiAgent instance
    monkeypatch.setattr(config, 'GEMINI_API_KEY', '')
    monkeypatch.setattr(main, 'agent', types.SimpleNamespace(run=fake_run))
    # Ensure no forced token substitution happens
    monkeypatch.setattr(config, 'API_KEY', '')
    monkeypatch.setattr(config, 'A2A_API_KEY', '')

    with TestClient(main.app) as client:
        client.post(
            "/",
            json=_make_jsonrpc_body("message/send", _make_message_params()),
            headers={"Authorization": "Bearer my-real-oauth-token"},
        )

    assert captured.get("bearer_token") == "my-real-oauth-token"


@pytest.mark.asyncio
async def test_jsonrpc_id_preserved(monkeypatch):
    """The 'id' from the request is echoed in the response envelope."""
    import main
    from fastapi.testclient import TestClient

    async def fake_run(user_text, mcp, context_id, bearer_token):
        return "done", [], False

    monkeypatch.setattr(main, 'agent', types.SimpleNamespace(run=fake_run))

    with TestClient(main.app) as client:
        resp = client.post(
            "/",
            json=_make_jsonrpc_body("message/send", _make_message_params(), rpc_id="req-abc-123"),
        )

    body = resp.json()
    assert body["id"] == "req-abc-123"


@pytest.mark.asyncio
async def test_jsonrpc_invalid_params(monkeypatch):
    """Params that don't match SendMessageRequest return a JSON-RPC invalid-params error."""
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, 'agent', None)

    with TestClient(main.app) as client:
        resp = client.post(
            "/",
            json=_make_jsonrpc_body("message/send", {"completely": "wrong"}, rpc_id=5),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 5
    assert body["error"]["code"] == -32602


def test_agentcard_streaming_flag_and_post_message_send(monkeypatch):
    """Verify AgentCard advertises streaming: false and POST / accepts message/send."""
    import main
    from fastapi.testclient import TestClient
    import types
    import a2a_models

    async def fake_run(user_text, mcp, context_id, bearer_token):
        artifact = a2a_models.Artifact(parts=[a2a_models.Part(text="rpc-ok")])
        return "summary", [artifact], True

    # Ensure startup doesn't overwrite our fake agent
    monkeypatch.setattr(main, 'agent', types.SimpleNamespace(run=fake_run))

    with TestClient(main.app) as client:
        # Check AgentCard streaming capability
        resp = client.get("/.well-known/agent-card.json")
        assert resp.status_code == 200
        card = resp.json()
        caps = card.get("capabilities", {})
        # Expect streaming to be explicitly false for Gemini Enterprise non-streaming mode
        assert caps.get("streaming") is False

        # Verify POST / accepts JSON-RPC message/send
        rpc_body = {
            "jsonrpc": "2.0",
            "id": "test-1",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "m-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "hello"}],
                }
            },
        }

        post_resp = client.post("/", json=rpc_body)
        assert post_resp.status_code == 200
        post_json = post_resp.json()
        assert post_json.get("jsonrpc") == "2.0"
        assert post_json.get("id") == "test-1"
        assert "error" not in post_json or post_json.get("error") is None
        assert "result" in post_json
