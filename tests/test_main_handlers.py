import asyncio
import sys
import pathlib
import pytest
import types

# Ensure repository root is on sys.path so tests can import local modules
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import a2a_models


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

    # Build a Message with a Part that declares a tool
    part = a2a_models.Part(data={"tool": "get_defect"})
    user_msg = a2a_models.Message(role="ROLE_USER", parts=[part])

    # Monkeypatch execute_tool to return an Artifact
    async def fake_execute_tool(tool_name, arguments, mcp, bearer_token=None):
        return a2a_models.Artifact(parts=[a2a_models.Part(text="defect data")])

    monkeypatch.setattr(main, 'execute_tool', fake_execute_tool)

    res = await main._handle_with_keywords('tid-kw', 'ctx-kw', 'ignored', user_msg, 'the-token')
    task = res['task']
    assert task['metadata']['mcp_called'] is True
    assert task['metadata']['auth_injected'] is True
    assert task['artifacts'] is not None
