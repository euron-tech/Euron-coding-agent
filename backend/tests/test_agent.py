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
    import euron_agent.sessions as s
    monkeypatch.setattr(s, "SESSIONS_DIR", tmp_path / "sessions")
    sess, io = make_session(tmp_path, [LLMResponse(content="hello", tool_calls=[])], persist=True)
    run(sess.run("hi there"))
    # a new persisted session for the same workspace resumes the latest one
    sess2, _ = make_session(tmp_path, [LLMResponse(content="again", tool_calls=[])], persist=True)
    assert any(m.get("content") == "hello" for m in sess2.messages)


def test_sessions_list_and_search(tmp_path, monkeypatch):
    import euron_agent.sessions as s
    monkeypatch.setattr(s, "SESSIONS_DIR", tmp_path / "sessions")
    sid = s.new_id()
    s.save(sid, str(tmp_path), [{"role": "user", "content": "fix the LOGIN bug"}])
    rows = s.list_sessions(str(tmp_path))
    assert rows and rows[0]["title"].startswith("fix the LOGIN")
    assert s.latest_id(str(tmp_path)) == sid
    hits = s.search("login", str(tmp_path))
    assert hits and hits[0]["id"] == sid


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


# --------------------------------------------------------------------------- #
# 0.4.0: permissions / hooks / memory / commands / pricing / multimodal
# --------------------------------------------------------------------------- #
def test_permissions_rules(tmp_path, monkeypatch):
    import euron_agent.permissions as pm
    monkeypatch.setattr(pm, "PERMISSIONS_FILE", tmp_path / "x.json")
    perms = pm.Permissions.from_config(
        {"deny": ["run_command(rm -rf*)"], "allow": ["read_file(**)"]},
        auto_writes=False, auto_commands=False,
    )
    assert perms.decide("run_command", {"command": "rm -rf /"}) == "deny"
    assert perms.decide("read_file", {"path": "x"}) == "allow"
    assert perms.decide("run_command", {"command": "ls"}) == "ask"
    assert perms.decide("write_file", {"path": "x"}) == "ask"


def test_always_allow(tmp_path, monkeypatch):
    import euron_agent.permissions as pm
    monkeypatch.setattr(pm, "PERMISSIONS_FILE", tmp_path / "p.json")
    p = pm.Permissions(default_writes="ask")
    assert p.decide("write_file", {"path": "a.py"}) == "ask"
    p.add_always_allow("write_file", {"path": "a.py"})
    assert p.decide("write_file", {"path": "a.py"}) == "allow"


def test_loop_permission_deny(tmp_path, monkeypatch):
    import euron_agent.permissions as pm
    monkeypatch.setattr(pm, "PERMISSIONS_FILE", tmp_path / "pd.json")
    (tmp_path / "d.txt").write_text("x", encoding="utf-8")
    cfg = load_config(provider="openai", api_key="x")
    cfg.permissions = {"deny": ["delete_file(**)"]}
    io = CollectIO()
    sess = AgentSession(str(tmp_path), cfg, io)
    sess.client = ScriptedClient([
        LLMResponse(content="", tool_calls=[ToolCall("1", "delete_file", {"path": "d.txt"})]),
        LLMResponse(content="ok", tool_calls=[]),
    ])
    run(sess.run("delete it"))
    assert (tmp_path / "d.txt").exists()
    assert any("Denied" in (m.get("content") or "")
               for m in sess.messages if m.get("role") == "tool")


def test_loop_hook_blocks(tmp_path, monkeypatch):
    import euron_agent.permissions as pm
    monkeypatch.setattr(pm, "PERMISSIONS_FILE", tmp_path / "ph.json")
    cfg = load_config(provider="openai", api_key="x")
    cfg.agent.auto_approve_writes = True
    cfg.hooks = {"PreToolUse": [{"matcher": "write_file", "command": "exit 3"}]}
    io = CollectIO()
    sess = AgentSession(str(tmp_path), cfg, io)
    sess.client = ScriptedClient([
        LLMResponse(content="", tool_calls=[ToolCall("1", "write_file", {"path": "h.txt", "content": "x"})]),
        LLMResponse(content="ok", tool_calls=[]),
    ])
    run(sess.run("write h"))
    assert not (tmp_path / "h.txt").exists()
    assert any("Blocked by PreToolUse" in (m.get("content") or "")
               for m in sess.messages if m.get("role") == "tool")


def test_memory(tmp_path, monkeypatch):
    import euron_agent.memory as mem
    monkeypatch.setattr(mem, "USER_FILE", tmp_path / "nouser.md")
    (tmp_path / "AGENTS.md").write_text("PROJECT_RULE_X", encoding="utf-8")
    assert "PROJECT_RULE_X" in mem.load_memory(str(tmp_path))
    sub = tmp_path / "proj"
    sub.mkdir()
    p = mem.write_template(str(sub))
    assert p.exists() and "Project memory" in p.read_text()


def test_custom_commands(tmp_path):
    from euron_agent.commands import expand_command, load_commands
    d = tmp_path / ".euron" / "commands"
    d.mkdir(parents=True)
    (d / "review.md").write_text("Review $ARGUMENTS for bugs. First: $1", encoding="utf-8")
    cmds = load_commands(str(tmp_path))
    assert "review" in cmds
    out = expand_command(cmds["review"], "file.py extra")
    assert "file.py extra" in out and "First: file.py" in out


def test_pricing():
    from euron_agent.pricing import cost_for, is_priced, rate_for
    assert abs(cost_for("gpt-4o-mini", 1_000_000, 0) - 0.15) < 1e-9
    assert cost_for("unknown-model-xyz", 1000, 1000) == 0.0
    assert not is_priced("unknown-model-xyz")
    # current models are now priced (the gpt-5.5 / $0 bug)
    assert is_priced("gpt-5.5")
    assert is_priced("gemini-2.0-flash") and is_priced("claude-sonnet-4-6")
    # config override prices any model
    over = {"my-model": {"input": 2.0, "output": 8.0}}
    assert is_priced("my-model", over)
    assert abs(cost_for("my-model", 1_000_000, 1_000_000, over) - 10.0) < 1e-9
    assert rate_for("my-model", over) == (2.0, 8.0)


def test_multimodal(tmp_path):
    sess, io = make_session(tmp_path, [LLMResponse(content="saw it", tool_calls=[])])
    run(sess.run("describe", images=["data:image/png;base64,AAAA"]))
    user = [m for m in sess.messages if m["role"] == "user"][-1]
    assert isinstance(user["content"], list)
    assert any(b.get("type") == "image_url" for b in user["content"])
    # usage event now carries a cost field
    assert any("session_cost" in e for e in io.events if e["type"] == "usage")


def test_anthropic_image_conversion():
    from euron_agent.llm import AnthropicClient
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]
    _system, conv = AnthropicClient._to_anthropic_messages(msgs)
    blocks = conv[0]["content"]
    assert any(b["type"] == "image" for b in blocks)


# --------------------------------------------------------------------------- #
# 0.5.0: skills / fallback / worktrees / usage
# --------------------------------------------------------------------------- #
def test_skills(tmp_path):
    from euron_agent.skills import load_skills, skills_summary
    d = tmp_path / ".euron" / "skills" / "deploy"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: Deploy the app\n---\nRun make deploy.",
                                encoding="utf-8")
    skills = load_skills(str(tmp_path))
    assert skills["deploy"]["description"] == "Deploy the app"
    assert "deploy" in skills_summary(skills)


def test_loop_use_skill(tmp_path):
    d = tmp_path / ".euron" / "skills" / "hello"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: say hi\n---\nSECRET_PLAYBOOK", encoding="utf-8")
    sess, io = make_session(tmp_path, [
        LLMResponse(content="", tool_calls=[ToolCall("1", "use_skill", {"name": "hello"})]),
        LLMResponse(content="done", tool_calls=[]),
    ])
    run(sess.run("use the hello skill"))
    assert any("SECRET_PLAYBOOK" in (m.get("content") or "")
               for m in sess.messages if m.get("role") == "tool")


def test_fallback_client():
    from euron_agent.llm import FallbackClient, LLMError, LLMResponse

    class Boom:
        provider = None

        def chat(self, *a, **k):
            raise LLMError("primary down")

    class Good:
        provider = None

        def chat(self, *a, **k):
            return LLMResponse(content="fallback worked")

    fc = FallbackClient([Boom(), Good()])
    assert fc.chat([], None, None, False).content == "fallback worked"


def test_worktree(tmp_path):
    import subprocess
    for c in ("git init -q", "git config user.email t@t.com", "git config user.name t"):
        subprocess.run(c, cwd=tmp_path, shell=True)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run("git add -A && git commit -q -m init", cwd=tmp_path, shell=True)
    from euron_agent.tools import worktree_add, worktree_list, worktree_remove
    ctx = ctx_for(tmp_path)
    assert worktree_add(ctx, "feature", "feat-branch").ok
    assert "feature" in worktree_list(ctx).output
    assert worktree_remove(ctx, "feature").ok


def test_usage_tracking(tmp_path):
    sess, io = make_session(tmp_path, [
        LLMResponse(content="", tool_calls=[ToolCall("1", "list_files", {})]),
        LLMResponse(content="done", tool_calls=[]),
    ])
    run(sess.run("list the files"))
    assert sess.tool_calls["list_files"] == 1


# --------------------------------------------------------------------------- #
# 0.6.0: plugins
# --------------------------------------------------------------------------- #
def test_plugins(tmp_path, monkeypatch):
    import euron_agent.plugins as pl
    monkeypatch.setattr(pl, "PLUGINS_DIR", tmp_path / "plugins")
    src = tmp_path / "myplug"
    (src / "skills" / "foo").mkdir(parents=True)
    (src / "commands").mkdir(parents=True)
    (src / "euron-plugin.yaml").write_text(
        "name: myplug\ndescription: test plugin\nmcp:\n  servers:\n    s1:\n      command: echo\n",
        encoding="utf-8")
    (src / "skills" / "foo" / "SKILL.md").write_text(
        "---\ndescription: foo skill\n---\nbody", encoding="utf-8")
    (src / "commands" / "hi.md").write_text("say hi to $ARGUMENTS", encoding="utf-8")

    assert pl.install(str(src)) == "myplug"
    assert any(p["name"] == "myplug" for p in pl.list_plugins())
    assert "s1" in pl.plugin_mcp_servers()

    from euron_agent.commands import load_commands
    from euron_agent.skills import load_skills
    assert "foo" in load_skills(str(tmp_path / "ws"))
    assert "hi" in load_commands(str(tmp_path / "ws"))
    assert pl.remove("myplug")


# --------------------------------------------------------------------------- #
# 1.0.0: providers / teams / schedules / notify / headless
# --------------------------------------------------------------------------- #
def test_many_providers():
    cfg = load_config()
    expected = {"gemini", "groq", "cerebras", "deepseek", "together", "mistral", "xai", "vercel", "lmstudio"}
    assert expected <= set(cfg.all_providers)
    g = load_config(provider="gemini")
    assert "generativelanguage" in (g.provider.base_url or "")


def test_team_id():
    from euron_agent.teams import team_id
    assert team_id("Auth Sprint!") == "team-auth-sprint"


def test_cron_match():
    from datetime import datetime
    from euron_agent.schedules import cron_match
    dt = datetime(2026, 6, 15, 9, 0)
    assert cron_match("* * * * *", dt)
    assert cron_match("0 9 * * *", dt)
    assert not cron_match("30 9 * * *", dt)
    assert cron_match("0 9 15 6 *", dt)
    assert not cron_match("0 9 16 6 *", dt)
    assert cron_match("0 9 * * MON-FRI", dt) == (dt.weekday() < 5)
    assert cron_match("*/15 * * * *", datetime(2026, 6, 15, 9, 30))


def test_schedules_crud(tmp_path, monkeypatch):
    from datetime import datetime
    import euron_agent.schedules as sc
    monkeypatch.setattr(sc, "SCHEDULES_FILE", tmp_path / "sch.json")
    s = sc.create("daily", "* * * * *", "do it", str(tmp_path))
    assert sc.list_schedules()[0]["name"] == "daily"
    assert sc.get(s["id"])["cron"] == "* * * * *"
    now = datetime(2026, 6, 15, 9, 0)
    assert sc.due(now)
    sc.mark_run(s["id"], now.strftime("%Y-%m-%d %H:%M"))
    assert not sc.due(now)
    assert sc.remove(s["id"])


def test_notify_dispatch(monkeypatch):
    import httpx
    import euron_agent.notify as nt
    monkeypatch.setattr(httpx, "post", lambda *a, **k: type("R", (), {"status_code": 200})())
    assert nt.send_slack("http://x", "hi")
    assert nt.send_google_chat("http://g", "hi")
    assert nt.send_whatsapp("sid", "tok", "+1", "+2", "hi")
    assert nt.send_linear("key", "team", "hi")
    assert nt.dispatch({"slack_webhook": "http://x", "discord_webhook": "http://y"}, "hi") == ["slack", "discord"]
    full = nt.dispatch({
        "slack_webhook": "http://x", "google_chat_webhook": "http://g",
        "twilio_sid": "s", "twilio_token": "t", "whatsapp_from": "+1", "whatsapp_to": "+2",
        "linear_api_key": "k", "linear_team_id": "team",
    }, "hi")
    assert set(full) == {"slack", "google_chat", "whatsapp", "linear"}
    assert nt.dispatch({}, "hi") == []


def test_loop_notify(tmp_path, monkeypatch):
    sent = []
    import euron_agent.notify as nt
    monkeypatch.setattr(nt, "dispatch", lambda cfg, text: sent.append(text) or ["slack"])
    cfg = load_config(provider="openai", api_key="x")
    cfg.notifications = {"on": ["done"], "slack_webhook": "http://x"}
    io = CollectIO()
    sess = AgentSession(str(tmp_path), cfg, io)
    sess.client = ScriptedClient([LLMResponse(content="all done", tool_calls=[])])
    run(sess.run("hi"))
    assert sent and "all done" in sent[0]


def test_headless_run(tmp_path, monkeypatch):
    import euron_agent.loop as loopmod
    monkeypatch.setattr(loopmod, "build_client",
                        lambda p, a=None: ScriptedClient([LLMResponse(content="headless ok", tool_calls=[])]))
    from euron_agent.headless import run_headless
    res = run(run_headless("do x", str(tmp_path)))
    assert res["final"] == "headless ok"


def test_max_tokens_compat():
    from euron_agent.config import ProviderConfig
    from euron_agent.llm import OpenAICompatClient, _detect_param_fix

    err = ("Unsupported parameter: 'max_tokens' is not supported with this model. "
           "Use 'max_completion_tokens' instead.")
    assert _detect_param_fix(err, {"max_tokens": 100}) == "max_completion_tokens"
    assert _detect_param_fix(
        "temperature does not support 0.2; only the default (1) is supported",
        {"temperature": 0.2}) == "drop_temperature"

    prov = ProviderConfig(name="openai", type="openai", api_key="k", model="gpt-5.5")
    c = OpenAICompatClient(prov)

    calls = {"n": 0, "last": None}

    class Msg:
        content = "ok"
        tool_calls = None

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]
        usage = None

    def fake_create(**kwargs):
        calls["n"] += 1
        calls["last"] = kwargs
        if "max_tokens" in kwargs:
            raise Exception("Unsupported parameter: 'max_tokens' is not supported. "
                            "Use 'max_completion_tokens' instead.")
        return Resp()

    c.client.chat.completions.create = fake_create
    resp = c.chat([{"role": "user", "content": "hi"}], stream=False)
    assert resp.content == "ok"
    assert "max_completion_tokens" in calls["last"] and "max_tokens" not in calls["last"]
    # the fix is remembered: the next call goes straight through (one attempt)
    calls["n"] = 0
    c.chat([{"role": "user", "content": "again"}], stream=False)
    assert calls["n"] == 1


def test_ingest_files_folders_images(tmp_path):
    from euron_agent import ingest
    ctx = ctx_for(tmp_path)
    (tmp_path / "data.txt").write_text("INGEST_ME_PLEASE", encoding="utf-8")
    sub = tmp_path / "folder"
    sub.mkdir()
    (sub / "a.py").write_text("CODE_ALPHA", encoding="utf-8")
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    # absolute file path in the message
    text, imgs = ingest.gather(f"look at {tmp_path / 'data.txt'}", ctx)
    assert "INGEST_ME_PLEASE" in text and imgs == []
    # quoted folder (drag-drop with spaces style)
    text2, _ = ingest.gather(f'summarize "{sub}"', ctx)
    assert "CODE_ALPHA" in text2
    # image -> multimodal data url
    _t3, imgs3 = ingest.gather(str(tmp_path / "pic.png"), ctx)
    assert imgs3 and imgs3[0].startswith("data:image")
    # bare filename with extension that exists
    text4, _ = ingest.gather("read data.txt", ctx)
    assert "INGEST_ME_PLEASE" in text4
    # a normal message with no paths is unchanged
    text5, imgs5 = ingest.gather("just refactor the auth module", ctx)
    assert text5 == "just refactor the auth module" and imgs5 == []


def test_scaffold_onboard(tmp_path):
    from euron_agent import scaffold
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"jest","build":"tsc"}}', encoding="utf-8")
    assert scaffold.needs_scaffold(str(tmp_path))
    created = scaffold.scaffold(str(tmp_path))
    assert any("AGENTS.md" in c for c in created)
    assert any("PROJECT.md" in c for c in created)
    assert any("SKILL.md" in c for c in created)
    assert not scaffold.needs_scaffold(str(tmp_path))
    info = scaffold.detect_project(str(tmp_path))
    assert "Python" in info["stack"] and "Node.js" in info["stack"]
    assert any("npm run test" in c for c in info["test"])
    assert scaffold.scaffold(str(tmp_path)) == []  # idempotent
    # the scaffolded skill + memory are picked up
    from euron_agent.memory import load_memory
    from euron_agent.skills import load_skills
    assert "explore-codebase" in load_skills(str(tmp_path))
    assert "Project memory" in load_memory(str(tmp_path))


def test_dangerous_mode(tmp_path, monkeypatch):
    import euron_agent.permissions as pm
    monkeypatch.setattr(pm, "PERMISSIONS_FILE", tmp_path / "dp.json")
    (tmp_path / "d.txt").write_text("x", encoding="utf-8")
    cfg = load_config(provider="openai", api_key="x")
    cfg.permissions = {"deny": ["delete_file(**)"]}  # even a deny rule is bypassed
    io = CollectIO(approve=False)  # would reject if it were ever asked
    sess = AgentSession(str(tmp_path), cfg, io, dangerous=True)
    sess.client = ScriptedClient([
        LLMResponse(content="", tool_calls=[ToolCall("1", "delete_file", {"path": "d.txt"})]),
        LLMResponse(content="done", tool_calls=[]),
    ])
    run(sess.run("delete it"))
    assert not (tmp_path / "d.txt").exists()  # ran despite deny + approve=False
    assert not any(e["type"] == "approval_request" for e in io.events)  # never asked


def test_team_mode(tmp_path, monkeypatch):
    import euron_agent.sessions as s
    monkeypatch.setattr(s, "SESSIONS_DIR", tmp_path / "sessions")
    cfg = load_config(provider="openai", api_key="x")
    io = CollectIO()
    sess = AgentSession(str(tmp_path), cfg, io, team="auth-sprint")
    sess.client = ScriptedClient([LLMResponse(content="coordinated", tool_calls=[])])
    run(sess.run("plan auth"))
    assert sess.session_id == "team-auth-sprint"
    # team coordinator instructions injected into the system prompt
    assert any("COORDINATOR" in (m.get("content") or "") for m in sess.messages if m.get("role") == "system")


# --------------------------------------------------------------------------- #
# v1.2.0 — code-intel, security, audit, sandbox, budget, doctor, ci
# --------------------------------------------------------------------------- #
def test_repo_map(tmp_path):
    from euron_agent import repomap
    ctx = ctx_for(tmp_path)
    (tmp_path / "m.py").write_text(
        "class Foo:\n    def bar(self):\n        pass\n\ndef top_level():\n    return 1\n",
        encoding="utf-8")
    out = repomap.build_map(ctx, ".")
    assert "m.py" in out and "class Foo" in out and "def bar" in out and "def top_level" in out
    # language filter excludes non-matching
    assert "no recognizable" in repomap.build_map(ctx, ".", "go")


def test_secret_scan(tmp_path):
    from euron_agent import secrets
    ctx = ctx_for(tmp_path)
    (tmp_path / "ok.py").write_text("api_key = os.environ['X']\n", encoding="utf-8")
    count, report = secrets.scan(ctx, ".")
    assert count == 0
    (tmp_path / "bad.py").write_text(
        'GITHUB="ghp_' + "a" * 36 + '"\npassword = "hunter2hunter2"\n', encoding="utf-8")
    count, report = secrets.scan(ctx, ".")
    assert count >= 1 and "bad.py" in report
    # full secret value is masked, not echoed
    assert "ghp_" + "a" * 36 not in report


def test_secret_scan_ignores_placeholders(tmp_path):
    from euron_agent import secrets
    ctx = ctx_for(tmp_path)
    (tmp_path / "c.py").write_text('password = "your-password-here"\n', encoding="utf-8")
    count, _ = secrets.scan(ctx, ".")
    assert count == 0


def test_dependency_audit_no_manifest(tmp_path):
    from euron_agent import depaudit
    ctx = ctx_for(tmp_path)
    clean, report = depaudit.audit(ctx)
    assert clean and "No recognized dependency manifest" in report


def test_sandbox_policy():
    from euron_agent import sandbox
    assert sandbox.check_command({}, "rm -rf /tmp/x")[0] is True  # no policy
    deny = {"deny_commands": [r"git push"]}
    assert sandbox.check_command(deny, "git push origin main")[0] is False
    net = {"block_network": True}
    assert sandbox.check_command(net, "curl http://evil.com")[0] is False
    assert sandbox.check_command(net, "pytest -q")[0] is True
    allow = {"allow_commands": [r"^pytest"]}
    assert sandbox.check_command(allow, "pytest -q")[0] is True
    assert sandbox.check_command(allow, "rm file")[0] is False
    assert sandbox.is_destructive("rm -rf /") is True


def test_audit_log_chain(tmp_path):
    from euron_agent import audit_log
    log = audit_log.AuditLog(str(tmp_path), enabled=True)
    log.record(ts="2026-01-01T00:00:00", tool="run_command",
               args={"command": "ls"}, decision="allow", ok=True, summary="ok")
    log.record(ts="2026-01-01T00:00:01", tool="write_file",
               args={"path": "a", "content": "x" * 500}, decision="allow", ok=True)
    intact, msg = audit_log.verify(str(tmp_path))
    assert intact, msg
    # Tamper with a record -> chain breaks.
    p = tmp_path / ".euron" / "audit" / "audit.log"
    lines = p.read_text(encoding="utf-8").splitlines()
    assert "ls" in lines[0]
    lines[0] = lines[0].replace("ls", "rm -rf /")  # only appears in the command arg
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    intact, msg = audit_log.verify(str(tmp_path))
    assert not intact and ("tamper" in msg.lower() or "mismatch" in msg.lower())


def test_doctor_checks(tmp_path):
    from euron_agent import doctor
    checks = doctor.run_checks(None, str(tmp_path))
    assert any(c.name == "Python" for c in checks)
    report = doctor.format_report(checks)
    assert "environment check" in report.lower()


def test_ci_workflow(tmp_path):
    from euron_agent import ci
    ok, where = ci.write_workflow(str(tmp_path))
    assert ok and (tmp_path / ".github" / "workflows" / "euron-agent.yml").exists()
    # idempotent without force
    ok2, _ = ci.write_workflow(str(tmp_path))
    assert not ok2


def test_tools_repo_map_and_scan_registered():
    from euron_agent.tools import TOOL_FUNCS
    for name in ("repo_map", "secret_scan", "dependency_audit"):
        assert name in TOOL_FUNCS


def test_subagent_budget_blocks(tmp_path, monkeypatch):
    # subagent_max_calls=0 means a spawn is refused, not executed.
    cfg = load_config()
    cfg.agent.subagent_max_calls = 0
    io = CollectIO()
    sess = AgentSession(str(tmp_path), cfg, io)

    class _TC:
        id = "x"
        name = "spawn_agent"
        arguments = {"description": "d", "prompt": "p"}

    run(sess._spawn_agent(_TC()))
    last = sess.messages[-1]
    assert "budget exhausted" in last["content"]


# --------------------------------------------------------------------------- #
# v1.3.0 — multi-model routing
# --------------------------------------------------------------------------- #
def test_model_router_default():
    from euron_agent.modelrouter import ModelRouter
    cfg = load_config()
    r = ModelRouter(cfg)
    for ph in ("plan", "execute", "subagent", "verify", "escalate"):
        assert r.is_default_phase(ph)
        assert r.model_for_phase(ph) == cfg.provider.model
    assert not r.has_distinct_escalation()
    assert len(r.summary()) == 5


def test_model_router_multi_provider():
    from euron_agent.modelrouter import ModelRouter
    cfg = load_config()
    cfg.models = {
        "planner": {"provider": "anthropic", "model": "claude-opus-4-8"},
        "cheap": {"model": "gpt-4o-mini"},   # same provider, cheaper model
    }
    cfg.routing = {"strategy": "auto", "plan": "planner", "subagent": "cheap",
                   "verify": "cheap", "escalate": "planner"}
    r = ModelRouter(cfg)
    assert r.role_for_phase("plan") == "planner"
    assert r.model_for_phase("plan") == "claude-opus-4-8"
    assert r.provider_for_phase("plan").name == "anthropic"
    assert not r.is_default_phase("plan")
    assert r.model_for_phase("subagent") == "gpt-4o-mini"
    assert r.provider_for_phase("subagent").name == cfg.provider.name
    assert r.has_distinct_escalation()
    rows = {row["phase"]: row for row in r.summary()}
    assert rows["plan"]["model"] == "claude-opus-4-8"
    assert rows["subagent"]["out_per_1m"] is not None  # gpt-4o-mini is priced


def test_model_router_backcompat_router_cheap():
    from euron_agent.modelrouter import ModelRouter
    cfg = load_config()
    cfg.router = {"cheap": "gpt-4o-mini"}
    cfg.models = {}
    cfg.routing = {}
    r = ModelRouter(cfg)
    assert "cheap" in r.roles
    assert r.role_for_phase("subagent") == "cheap"
    assert r.model_for_phase("subagent") == "gpt-4o-mini"


def test_model_router_string_shorthand():
    from euron_agent.modelrouter import ModelRouter
    cfg = load_config()
    cfg.models = {"cheap": "gpt-4o-mini"}   # shorthand: role -> model id
    cfg.routing = {"subagent": "cheap"}
    r = ModelRouter(cfg)
    assert r.model_for_phase("subagent") == "gpt-4o-mini"


def test_escalation_trigger(tmp_path):
    cfg = load_config()
    cfg.models = {"strong": {"model": "gpt-4o"}}
    cfg.routing = {"strategy": "auto", "escalate": "strong", "escalate_after": 2}
    sess = AgentSession(str(tmp_path), cfg, CollectIO())
    assert sess.router.has_distinct_escalation()
    assert sess._escalated is False
    for _ in range(2):  # two consecutive all-failed steps
        sess._step_total, sess._step_fail = 1, 1
        run(sess._maybe_escalate())
    assert sess._escalated is True


def test_escalation_disabled_in_fixed(tmp_path):
    cfg = load_config()
    cfg.models = {"strong": {"model": "gpt-4o"}}
    cfg.routing = {"strategy": "fixed", "escalate": "strong", "escalate_after": 1}
    sess = AgentSession(str(tmp_path), cfg, CollectIO())
    sess._step_total, sess._step_fail = 1, 1
    run(sess._maybe_escalate())
    assert sess._escalated is False


# --------------------------------------------------------------------------- #
# v1.4.0 — auto-deploy / monitor / self-heal / guardrails
# --------------------------------------------------------------------------- #
def test_deploy_registry_and_commands(monkeypatch):
    from euron_agent import deploy
    assert deploy.target("cloudrun") and deploy.target("vercel")
    assert deploy.target("nope") is None
    # token + cli gating
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    ok, msg = deploy.deploy_command("vercel")
    assert not ok and ("CLI not installed" in msg or "No credential" in msg)
    # command building with params
    monkeypatch.setattr(deploy, "cli_installed", lambda t: True)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    ok, cmd = deploy.deploy_command("cloudrun", {"service": "api", "region": "us-central1"})
    assert ok and "gcloud run deploy api" in cmd and "us-central1" in cmd
    ok, rb = deploy.rollback_command("kubernetes", {"deployment": "web"})
    assert ok and "rollout undo deployment/web" in rb
    ok, _ = deploy.rollback_command("netlify")  # no rollback cmd
    assert not ok


def test_deploy_readiness_and_suggest(monkeypatch):
    from euron_agent import deploy
    monkeypatch.setattr(deploy, "cli_installed", lambda t: t.name in ("vercel", "cloudrun"))
    monkeypatch.setattr(deploy, "has_token", lambda t: t.name == "vercel")
    rows = {r["name"]: r for r in deploy.readiness()}
    assert rows["vercel"]["ready"] is True
    assert rows["cloudrun"]["ready"] is False  # cli yes, token no
    assert deploy.suggest("next.js app") == "vercel"  # ready + frontend-preferred


def test_deploy_config_template():
    from euron_agent import deploy
    fly = deploy.config_template("fly.toml", {"app": "x", "region": "iad", "port": 8000})
    assert 'app = "x"' in fly and "8000" in fly
    df = deploy.config_template("Dockerfile.python", {"entry": "app.py", "port": 8080})
    assert "python:3.12-slim" in df and "app.py" in df


def test_guardrails_spend_gate():
    from euron_agent import guardrails
    # free-tier target → allow regardless of config
    assert guardrails.spend_gate({}, "cloudrun")[0] == "allow"
    # billable target, default free_tier_only → deny
    assert guardrails.spend_gate({}, "fly")[0] == "deny"
    # billable allowed → allow
    assert guardrails.spend_gate({"allow_billable": True}, "fly")[0] == "allow"
    # free_tier_only off but not explicitly allowed → ask
    assert guardrails.spend_gate({"free_tier_only": False}, "fly")[0] == "ask"
    assert guardrails.spend_gate({}, "nope")[0] == "deny"


def test_guardrails_redact(monkeypatch):
    from euron_agent import guardrails
    monkeypatch.setenv("VERCEL_TOKEN", "supersecrettoken123456")
    out = guardrails.redact("deploying with supersecrettoken123456 now")
    assert "supersecrettoken123456" not in out and "VERCEL_TOKEN" in out
    assert "***redacted***" in guardrails.redact("key ghp_" + "a" * 36)


def test_monitor_status_and_instrument(monkeypatch):
    from euron_agent import monitor
    for v in ("SENTRY_DSN", "OTEL_EXPORTER_OTLP_ENDPOINT", "DD_API_KEY",
              "UPTIMEROBOT_API_KEY", "BETTERSTACK_TOKEN", "SENTRY_AUTH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    assert "No monitoring configured" in monitor.status()
    monkeypatch.setenv("SENTRY_DSN", "https://x@sentry.io/1")
    assert monitor.instrument_env()["SENTRY_DSN"].startswith("https://")
    assert monitor.configured_providers()["sentry"] is True
    ok, _ = monitor.register_uptime_command("https://x.app")
    assert ok is False  # no uptime token
    monkeypatch.setenv("UPTIMEROBOT_API_KEY", "u123")
    ok, cmd = monitor.register_uptime_command("https://x.app")
    assert ok and "uptimerobot" in cmd and "https://x.app" in cmd


def test_selfheal_health_and_rollback(monkeypatch, tmp_path):
    from euron_agent import selfheal, deploy
    # health check: stub http_health
    monkeypatch.setattr(selfheal, "http_health", lambda url, timeout=10.0: (True, "HTTP 200"))
    ok, detail = selfheal.check_health("https://x.app", "/health")
    assert ok and "healthy" in detail
    monkeypatch.setattr(selfheal, "http_health", lambda url, timeout=10.0: (False, "HTTP 500"))
    ok, detail = selfheal.check_health("https://x.app", attempts=2, delay=0, _sleep=lambda s: None)
    assert not ok and "unhealthy" in detail
    # heal_once: unhealthy → rollback attempted (stub deploy + run_command)
    monkeypatch.setattr(deploy, "rollback_command", lambda n, p=None: (True, "echo rolled-back"))
    ctx = ctx_for(tmp_path)
    report = selfheal.heal_once(ctx, "https://x.app", "cloudrun", attempts=1, delay=0)
    assert report["action"] == "rollback" and report["rollback_ok"] is True


def test_deploy_tools_registered_and_gated(tmp_path, monkeypatch):
    from euron_agent.tools import TOOL_FUNCS, ToolContext, deploy_check, execute
    for name in ("deploy", "rollback", "provision_db", "deploy_check",
                 "monitor_status", "health_check"):
        assert name in TOOL_FUNCS
    ctx = ToolContext(str(tmp_path), AgentConfig(), [], deploy_cfg={})
    assert "Deploy targets" in deploy_check(ctx).output
    # deploy tool respects the spend gate (billable target blocked by default)
    monkeypatch.setattr("euron_agent.deploy.cli_installed", lambda t: True)
    monkeypatch.setenv("FLY_API_TOKEN", "x")
    out = execute(ctx, "deploy", {"target": "fly"})
    assert not out.ok and "blocked" in out.output
