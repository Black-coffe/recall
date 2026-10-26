"""The MCP prompt `post_digest` (mcp-triggers): `/mcp__recall__post_digest` in Claude Code.

A prompt is not a tool: it must not change the registry, it runs nothing, and it exists only where
the `pipes` tool it points to is registered. Offline: a fresh `mcp_server` copy, a temp pipes dir.
"""
from __future__ import annotations

import asyncio

from tests.test_mcp_registry import load_mcp_copy


def _prompt_names(mod) -> set:
    return {p.name for p in asyncio.run(mod.mcp.list_prompts())}


def _render(mod, arguments=None) -> str:
    res = asyncio.run(mod.mcp.render_prompt("post_digest", arguments))
    return "\n".join(m.content.text for m in res.messages)


def _fake_pipes(monkeypatch, names):
    from app.services import pipes as pipes_svc

    class _P:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(pipes_svc, "load_pipes", lambda *a, **k: [_P(n) for n in names])


def test_listed_on_the_full_profile_only():
    assert "post_digest" in _prompt_names(load_mcp_copy())
    assert "post_digest" not in _prompt_names(load_mcp_copy("read"))


def test_default_renders_a_weekly_digest_run(monkeypatch):
    _fake_pipes(monkeypatch, ["open-questions", "weekly-digest"])
    text = _render(load_mcp_copy())
    assert "`weekly-digest`" in text
    assert "`pipes`" in text and 'action="run"' in text and "get_job_status" in text


def test_named_pipe_renders_that_pipe(monkeypatch):
    _fake_pipes(monkeypatch, ["open-questions", "weekly-digest"])
    text = _render(load_mcp_copy(), {"pipe": "open-questions"})
    assert 'name="open-questions"' in text


def test_unknown_pipe_lists_the_names_and_runs_nothing(monkeypatch):
    _fake_pipes(monkeypatch, ["open-questions", "weekly-digest"])
    text = _render(load_mcp_copy(), {"pipe": "monday"})
    assert "`monday`" in text and "`open-questions`" in text and "`weekly-digest`" in text
    assert 'action="run"' not in text


def test_registry_unchanged_by_the_prompt():
    full = load_mcp_copy()
    assert "post_digest" not in full._TOOL_CLASS
    assert "post_digest" not in full._registered_tool_names()
    assert set(full._registered_tool_names()) == set(full._TOOL_CLASS)
