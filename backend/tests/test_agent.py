"""Test suite for the Euron Agent backend.

Run from backend/:  pytest -q
Uses a scripted FakeClient so no network/API key is needed.
"""
import asyncio
import json
from pathlib import Path

import pytest

from euron_agent.checkpoints import Checkpointer
from euron_agent.config import AgentConfig, load_config
from euron_agent.context import compact_history, estimate_tokens, expand_mentions
from euron_agent.events import AgentIO, ApprovalDecision
from euron_agent.gitignore import load_gitignore_patterns
from euron_agent.llm import LLMResponse, ToolCall, _retryable, _safe_json_loads
from euron_agent.loop import AgentSession
from euron_agent import history
from euron_agent.tools import ToolContext, edit_file, execute, read_file, run_command


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ctx_for(tmp_path) -> ToolContext:
    return ToolContext(str(tmp_path), AgentConfig(), [".env", ".git/**"])


class CollectIO(AgentIO):
    """Auto-approving IO that records every event."""

    def __init__(self, approve=True):
        self.events = []
        self.approve = approve

    def emit_sync(self, event):
        self.events.append(event)

    async def emit(self, event):
        self.events.append(event)

    async def request_approval(self, request):
        self.events.append(request)
        return ApprovalDecision(self.approve)

    def types(self):
        return [e["type"] for e in self.events]


class ScriptedClient:
    """Returns a queued list of LLMResponses, one per chat() call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, tools=None, stream_cb=None, stream=True):
        self.calls += 1
        if stream_cb:
            stream_cb("…")
        return self.responses.pop(0)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_builtin_providers_and_overrides():
    cfg = load_config(provider="euri", api_key="k", base_url="https://x/v1", model="m")
    assert cfg.provider.name == "euri"
    assert cfg.provider.api_key == "k"
    assert cfg.provider.base_url == "https://x/v1"
    assert cfg.provider.model == "m"
    assert {"euri", "openai", "ollama", "anthropic", "custom"} <= set(cfg.all_providers)


# --------------------------------------------------------------------------- #
# tools: sandbox / edit / binary / command / ignore
# --------------------------------------------------------------------------- #
def test_sandbox_blocks_escape(tmp_path):
    ctx = ctx_for(tmp_path)
    with pytest.raises(PermissionError):
        ctx.resolve("../outside.txt")


def test_edit_file_unique_and_missing(tmp_path):
    ctx = ctx_for(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    miss = edit_file(ctx, "a.py", "nope", "y")
    assert not miss.ok and "not found" in miss.output
    dup = edit_file(ctx, "a.py", "x = 1", "y")
    assert not dup.ok and "not unique" in dup.output
    ok = edit_file(ctx, "a.py", "x = 1", "y", replace_all=True)
    assert ok.ok and (tmp_path / "a.py").read_text() == "y\ny\n"
    assert ok.diff


def test_read_binary_refused(tmp_path):
    ctx = ctx_for(tmp_path)
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02ELF")
    out = read_file(ctx, "b.bin")
    assert not out.ok and "binary" in out.output


def test_run_command_streams(tmp_path):
    ctx = ctx_for(tmp_path)
    chunks = []
    out = run_command(ctx, "echo hello", on_output=chunks.append)
    assert out.ok
    assert "hello" in out.output
    assert any("hello" in c for c in chunks)


def test_ignore_blocks_env(tmp_path):
    ctx = ctx_for(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    out = read_file(ctx, ".env")
    assert not out.ok


def test_execute_unknown_tool(tmp_path):
    out = execute(ctx_for(tmp_path), "nope", {})
    assert not out.ok


# --------------------------------------------------------------------------- #
# gitignore
# --------------------------------------------------------------------------- #
def test_gitignore_parsing(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n*.log\n!keep.log\n", encoding="utf-8")
    pats = load_gitignore_patterns(tmp_path)
    assert "node_modules" in pats
    assert any("*.log" == p for p in pats)
    assert all(not p.startswith("!") for p in pats)


# --------------------------------------------------------------------------- #
# context management
# --------------------------------------------------------------------------- #
def test_estimate_and_compact():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "tool", "tool_call_id": str(i), "content": "x" * 4000} for i in range(10)]
    msgs += [{"role": "user", "content": "recent"}]
    assert estimate_tokens(msgs) > 1000
    out, changed = compact_history(msgs, max_tokens=500, keep_recent=2)
    assert changed
    assert estimate_tokens(out) < estimate_tokens(msgs)
    assert out[0]["content"] == "s"  # system preserved


def test_expand_mentions(tmp_path):
    ctx = ctx_for(tmp_path)
    (tmp_path / "note.txt").write_text("HELLO_CONTENT", encoding="utf-8")
    out = expand_mentions("look at @note.txt please", ctx)
    assert "HELLO_CONTENT" in out
    # non-existent mention is left as-is
    assert expand_mentions("just @nothing here", ctx) == "just @nothing here"


# --------------------------------------------------------------------------- #
# checkpoints
# --------------------------------------------------------------------------- #
def test_checkpoint_undo(tmp_path):
    cp = Checkpointer()
    f = tmp_path / "c.txt"
    f.write_text("original", encoding="utf-8")
    cp.begin_turn()
    cp.record(f)
    f.write_text("changed", encoding="utf-8")
    newfile = tmp_path / "new.txt"
    cp.record(newfile)
    newfile.write_text("created", encoding="utf-8")
    reverted = cp.undo_last_turn()
    assert f.read_text() == "original"
    assert not newfile.exists()
    assert len(reverted) == 2


# --------------------------------------------------------------------------- #
# llm helpers
# --------------------------------------------------------------------------- #
def test_safe_json_loads():
    assert _safe_json_loads('{"a":1}') == {"a": 1}
    assert _safe_json_loads("") == {}
    assert "__raw__" in _safe_json_loads("not json")


def test_retryable_classification():
    class E(Exception):
        status_code = 401

    class F(Exception):
        status_code = 500

    assert not _retryable(E())
    assert _retryable(F())
    assert _retryable(ConnectionError("boom"))


# --------------------------------------------------------------------------- #
# history persistence
# --------------------------------------------------------------------------- #
def test_history_roundtrip(tmp_path, monkeypatch):
    import euron_agent.history as h
    monkeypatch.setattr(h, "SESSIONS_DIR", tmp_path / "sessions")
    msgs = [{"role": "user", "content": "hi"}]
    h.save_history(str(tmp_path), msgs)
    assert h.load_history(str(tmp_path)) == msgs
    h.clear_history(str(tmp_path))
    assert h.load_history(str(tmp_path)) == []


# --------------------------------------------------------------------------- #
# end-to-end loop with a scripted client
# --------------------------------------------------------------------------- #
def make_session(tmp_path, responses, approve=True, **kw):
    cfg = load_config(provider="openai", api_key="x")
    io = CollectIO(approve=approve)
    sess = AgentSession(str(tmp_path), cfg, io, **kw)
    sess.client = ScriptedClient(responses)
    return sess, io


def test_loop_create_edit_run(tmp_path):
    responses = [
        LLMResponse(content="creating", tool_calls=[
            ToolCall("1", "create_file", {"path": "hi.py", "content": "print('hi')\n"})]),
        LLMResponse(content="editing", tool_calls=[
            ToolCall("2", "edit_file", {"path": "hi.py", "old_string": "hi", "new_string": "yo"})]),
        LLMResponse(content="running", tool_calls=[
            ToolCall("3", "run_command", {"command": "echo done"})]),
        LLMResponse(content="all set", tool_calls=[]),
    ]
    sess, io = make_session(tmp_path, responses)
    run(sess.run("do the thing"))
    assert (tmp_path / "hi.py").read_text() == "print('yo')\n"
    assert "done" in "".join(e.get("text", "") for e in io.events if e["type"] == "command_output")
    assert "diff" in io.types() and "done" in io.types()
    assert any(e["type"] == "usage" for e in io.events)


def test_loop_rejection(tmp_path):
    responses = [
        LLMResponse(content="", tool_calls=[
            ToolCall("1", "write_file", {"path": "x.py", "content": "bad"})]),
        LLMResponse(content="ok I won't", tool_calls=[]),
    ]
    sess, io = make_session(tmp_path, responses, approve=False)
    run(sess.run("write x"))
    assert not (tmp_path / "x.py").exists()
    assert any("REJECTED" in m.get("content", "") for m in sess.messages if m.get("role") == "tool")


def test_loop_undo(tmp_path):
    (tmp_path / "f.py").write_text("orig\n", encoding="utf-8")
    responses = [
        LLMResponse(content="", tool_calls=[
            ToolCall("1", "write_file", {"path": "f.py", "content": "new\n"})]),
        LLMResponse(content="done", tool_calls=[]),
    ]
    sess, io = make_session(tmp_path, responses)
    run(sess.run("change f"))
    assert (tmp_path / "f.py").read_text() == "new\n"
    reverted = sess.undo()
    assert (tmp_path / "f.py").read_text() == "orig\n"
    assert reverted


def test_loop_mentions_inlined(tmp_path):
    (tmp_path / "ctx.txt").write_text("MENTIONED_DATA", encoding="utf-8")
    sess, io = make_session(tmp_path, [LLMResponse(content="seen", tool_calls=[])])
    run(sess.run("use @ctx.txt"))
    user_msg = [m for m in sess.messages if m["role"] == "user"][-1]
    assert "MENTIONED_DATA" in user_msg["content"]


def test_loop_persist_roundtrip(tmp_path, monkeypatch):
    import euron_agent.history as h
    monkeypatch.setattr(h, "SESSIONS_DIR", tmp_path / "sessions")
    sess, io = make_session(tmp_path, [LLMResponse(content="hello", tool_calls=[])], persist=True)
    run(sess.run("hi there"))
    # a new session with persist should reload the prior messages
    sess2, _ = make_session(tmp_path, [LLMResponse(content="again", tool_calls=[])], persist=True)
    assert any(m.get("content") == "hello" for m in sess2.messages)


# --------------------------------------------------------------------------- #
# new tools: glob / multi_edit / background / git / web
# --------------------------------------------------------------------------- #
def test_glob(tmp_path):
    from euron_agent.tools import glob_files
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.txt").write_text("y", encoding="utf-8")
    out = glob_files(ctx_for(tmp_path), "**/*.py")
    assert "src/a.py" in out.output


def test_multi_edit_atomic(tmp_path):
    from euron_agent.tools import multi_edit
    ctx = ctx_for(tmp_path)
    f = tmp_path / "m.py"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    ok = multi_edit(ctx, "m.py", [
        {"old_string": "a", "new_string": "x"},
        {"old_string": "c", "new_string": "z"},
    ])
    assert ok.ok and f.read_text() == "x\nb\nz\n"
    # atomic: a failing edit leaves the file untouched
    f.write_text("a\nb\n", encoding="utf-8")
    bad = multi_edit(ctx, "m.py", [
        {"old_string": "a", "new_string": "x"},
        {"old_string": "NOPE", "new_string": "z"},
    ])
    assert not bad.ok and f.read_text() == "a\nb\n"


def test_background_manager(tmp_path):
    import time
    from euron_agent.background import BackgroundManager
    m = BackgroundManager()
    pid = m.start(str(tmp_path), "echo hello")
    time.sleep(0.5)
    out = m.output(pid)
    assert "hello" in out or "exited" in out
    assert pid in m.list_()
    m.kill(pid)


def test_git_tools(tmp_path):
    import subprocess
    for c in ("git init -q", "git config user.email t@t.com", "git config user.name t"):
        subprocess.run(c, cwd=tmp_path, shell=True)
    (tmp_path / "f.txt").write_text("hi", encoding="utf-8")
    from euron_agent.tools import git_status, git_commit
    ctx = ctx_for(tmp_path)
    assert "f.txt" in git_status(ctx).output
    assert git_commit(ctx, "init").ok


def test_web_helpers(monkeypatch):
    import euron_agent.webtools as wt
    assert "hello" in wt._strip_html("<p>hello <b>world</b></p>")
    ok, text = wt._fmt([{"title": "T", "url": "http://x", "snippet": "S"}])
    assert ok and "T" in text and "http://x" in text

    class R:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><body>PAGE_CONTENT</body></html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(wt.httpx, "get", lambda *a, **k: R())
    ok, text = wt.web_fetch("http://x")
    assert ok and "PAGE_CONTENT" in text


# --------------------------------------------------------------------------- #
# meta tools: todo / plan mode / sub-agents / compact / mcp
# --------------------------------------------------------------------------- #
def test_todo_write(tmp_path):
    responses = [
        LLMResponse(content="", tool_calls=[ToolCall("1", "todo_write", {"todos": [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "pending"}]})]),
        LLMResponse(content="ok", tool_calls=[]),
    ]
    sess, io = make_session(tmp_path, responses)
    run(sess.run("multi step"))
    assert any(e["type"] == "todos" for e in io.events)
    assert sess.todos and sess.todos[0]["content"] == "a"


def test_plan_mode_approval(tmp_path):
    responses = [
        LLMResponse(content="here's my plan", tool_calls=[
            ToolCall("1", "update_plan", {"plan": "1. do x\n2. do y"})]),
        LLMResponse(content="implementing", tool_calls=[]),
    ]
    sess, io = make_session(tmp_path, responses, approve=True)
    sess.plan_mode = True
    run(sess.run("build feature"))
    assert any(e["type"] == "plan" for e in io.events)
    assert sess.plan_mode is False  # approved → plan mode turns off


def test_subagent(tmp_path, monkeypatch):
    import euron_agent.loop as loopmod
    monkeypatch.setattr(
        loopmod, "build_client",
        lambda p, a=None: ScriptedClient([LLMResponse(content="sub-agent analysis result", tool_calls=[])]),
    )
    parent = [
        LLMResponse(content="", tool_calls=[ToolCall("1", "spawn_agent", {
            "description": "analyze", "prompt": "analyze the repo"})]),
        LLMResponse(content="done", tool_calls=[]),
    ]
    cfg = load_config(provider="openai", api_key="x")
    io = CollectIO()
    sess = AgentSession(str(tmp_path), cfg, io)
    sess.client = ScriptedClient(parent)
    run(sess.run("delegate"))
    assert any(e["type"] == "subagent_start" for e in io.events)
    assert any(e["type"] == "subagent_end" for e in io.events)
    assert any("analysis result" in (m.get("content") or "")
               for m in sess.messages if m.get("role") == "tool")


def test_compact_summarize():
    from euron_agent.context import summarize_history
    client = ScriptedClient([LLMResponse(content="SUMMARY_BRIEF", tool_calls=[])])
    msgs = [{"role": "system", "content": "s"}]
    for _ in range(5):
        msgs.append({"role": "user", "content": "do x"})
        msgs.append({"role": "assistant", "content": "did x"})
    new, changed = summarize_history(client, msgs, keep_recent=2)
    assert changed
    assert any("SUMMARY_BRIEF" in (m.get("content") or "") for m in new)
    assert new[0]["content"] == "s"


def test_mcp_routing(tmp_path):
    class FakeMCP:
        started = True
        errors = []

        def schemas(self):
            return [{"type": "function", "function": {
                "name": "mcp__srv__do", "description": "", "parameters": {"type": "object"}}}]

        async def start(self):
            pass

        async def call(self, name, args):
            return f"mcp ok {name}"

    responses = [
        LLMResponse(content="", tool_calls=[ToolCall("1", "mcp__srv__do", {"x": 1})]),
        LLMResponse(content="done", tool_calls=[]),
    ]
    sess, io = make_session(tmp_path, responses, approve=True)
    sess.mcp = FakeMCP()
    run(sess.run("use mcp"))
    assert any("mcp ok" in (m.get("content") or "")
               for m in sess.messages if m.get("role") == "tool")
