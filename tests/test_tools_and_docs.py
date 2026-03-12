import importlib
import pathlib


def test_build_tools_includes_tell_joke():
    gemini_agent = importlib.import_module('gemini_agent')
    # _build_tools returns a list of callables; ensure tell_joke is present
    tools = gemini_agent._build_tools(mcp=None, artifacts=[])
    names = [fn.__name__ for fn in tools]
    assert 'tell_joke' in names


def test_readme_mentions_metadata_flags():
    readme = pathlib.Path('README.md').read_text(encoding='utf-8')
    assert 'mcp_called' in readme
    assert 'auth_injected' in readme


def test_static_index_references_flags():
    index = pathlib.Path('static/index.html').read_text(encoding='utf-8')
    assert 'task?.metadata?.mcp_called' in index or 'metadata?.mcp_called' in index
    assert 'auth_injected' in index
