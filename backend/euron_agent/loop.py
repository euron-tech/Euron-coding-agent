"""The agentic loop.

`AgentSession` holds one conversation. `run(task)` processes a user turn: it
drives the LLM, executes tool calls (gating mutations behind approval), and
streams everything through AgentIO. Features: cancellation, @file mentions,
context compaction, per-turn checkpoints (undo), streamed command output, token
usage, persistence, **plan mode**, **TODO checklist**, **sub-agents**, and
**MCP** tool routing.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from collections import Counter

from . import events as ev
from . import audit_log, gitignore, ingest, memory, pricing, sandbox, sessions
from . import skills as skills_mod
from .checkpoints import Checkpointer
from .config import Config
from .context import compact_history, expand_mentions
from .events import AgentIO, ApprovalDecision
from .hooks import HookRunner
from .llm import LLMError, build_client
from .mcp_client import MCPManager, is_mcp_tool
from .modelrouter import ModelRouter
from .permissions import Permissions
from .prompts import system_prompt
from .tool_schemas import LOOP_TOOLS, schemas_for
from .tools import ToolContext, execute, list_files, preview_for, run_command

_FILE_MUTATORS = {"write_file", "edit_file", "multi_edit", "create_file", "delete_file"}
_MAX_SUBAGENT_DEPTH = 2
# Memory/context: bound how much of any single tool result is kept in history.
_MAX_TOOL_RESULT_CHARS = 16000


class _SubAgentIO(AgentIO):
    """IO for a sub-agent: forwards tool activity to the parent and captures the
    sub-agent's final message as the result."""

    def __init__(self, parent: AgentIO):
        self.parent = parent
        self.last_assistant = ""

    def emit_sync(self, event: dict) -> None:
        if event.get("type") == "command_output":
            self.parent.emit_sync(event)

    async def emit(self, event: dict) -> None:
        t = event.get("type")
        if t == "assistant_message":
            self.last_assistant = event["text"]
        if t in ("tool_start", "tool_result", "diff", "error"):
            await self.parent.emit(event)

    async def request_approval(self, request: dict) -> ApprovalDecision:
        return await self.parent.request_approval(request)


class AgentSession:
    def __init__(
        self,
        workspace: str,
        config: Config,
        io: AgentIO,
        *,
        persist: bool = False,
        plan_mode: bool = False,
        depth: int = 0,
        session_id: str | None = None,
        team: str | None = None,
        dangerous: bool = False,
        audit: "audit_log.AuditLog | None" = None,
        budget: dict | None = None,
    ):
        self.workspace = workspace
        self.config = config
        self.io = io
        self.team = team
        self.dangerous = dangerous  # YOLO: never ask, run everything
        # Multi-model router: one model per phase (plan/execute/subagent/verify),
        # cost-aware with escalation. self.client is the active/execute-phase client
        # (built here so it stays the patch/injection point); the router supplies a
        # distinct client only for phases that diverge to another model.
        self.client = build_client(config.provider, config.agent)
        self.router = ModelRouter(config)
        # Tamper-evident audit trail (shared across nested sub-agents).
        self.audit = audit or audit_log.AuditLog(workspace, enabled=config.agent.audit_log)
        # Shared agent-of-agent budget (calls + tokens) across the whole tree.
        self._budget = budget if budget is not None else {"calls": 0, "tokens": 0}
        self._turn_mutated: set[str] = set()
        self._heal_attempts: dict[str, int] = {}
        self._escalated = False
        self._fail_streak = 0
        self._step_total = 0
        self._step_fail = 0
        if team:
            from . import teams

            persist = True
            session_id = teams.team_id(team)

        ignore = list(config.ignore)
        if config.agent.use_gitignore:
            ignore += gitignore.load_gitignore_patterns(Path(workspace))
        self.ctx = ToolContext(workspace, config.agent, ignore, web=config.web,
                               deploy_cfg=config.deploy)

        self.checkpointer = Checkpointer()
        self.session_tokens = 0
        self.session_cost = 0.0
        self.tool_calls: Counter = Counter()
        self.subagent_calls = 0
        self.skills = skills_mod.load_skills(workspace)
        self.persist = persist
        self.plan_mode = plan_mode
        self.depth = depth
        self.todos: list = []
        if depth == 0:
            from . import plugins as _plugins

            mcp_servers = {**_plugins.plugin_mcp_servers(), **config.mcp_servers}
        else:
            mcp_servers = {}
        self.mcp = MCPManager(mcp_servers)
        self.permissions = Permissions.from_config(
            config.permissions,
            auto_writes=config.agent.auto_approve_writes,
            auto_commands=config.agent.auto_approve_commands,
        )
        self.hooks = HookRunner(config.hooks, workspace)
        self._cancelled = False
        self.session_id = None
        self.messages: list[dict] = []
        if persist:
            self.session_id = session_id or sessions.latest_id(workspace) or sessions.new_id()
            self.messages = sessions.load(self.session_id)

    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        self._cancelled = True

    def undo(self) -> list[str]:
        return self.checkpointer.undo_last_turn()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _note(self, ok: bool) -> None:
        """Record a per-step tool outcome (drives cost-aware escalation)."""
        self._step_total += 1
        if not ok:
            self._step_fail += 1

    def _record_audit(self, tool: str, args: dict, decision: str,
                      ok: bool | None, summary: str = "") -> None:
        try:
            self.audit.record(ts=self._now(), tool=tool, args=args, decision=decision,
                              ok=ok, summary=summary, depth=self.depth)
        except Exception:
            pass

    def _system_content(self) -> str:
        tree = list_files(self.ctx).output
        base = system_prompt(self.workspace, tree)
        mem = memory.load_memory(self.workspace)
        if mem:
            base += "\n\n# Memory / project instructions\n" + mem
        skill_text = skills_mod.skills_summary(self.skills)
        if skill_text:
            base += "\n\n# Skills\n" + skill_text
        if self.team:
            from . import teams

            base += teams.coordinator_prompt(self.team)
        return base

    def _ensure_system(self) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": self._system_content()})
        elif self.messages[0].get("role") != "system":
            self.messages.insert(0, {"role": "system", "content": self._system_content()})

    def _tools_for_turn(self) -> list:
        return schemas_for(self.plan_mode) + (self.mcp.schemas() if not self.plan_mode else [])

    # ------------------------------------------------------------------ #
    async def run(self, task: str, images: list | None = None) -> None:
        self._cancelled = False
        if not self.mcp.started:
            try:
                await self.mcp.start()
                if self.mcp.errors:
                    await self.io.emit(ev.info("MCP: " + "; ".join(self.mcp.errors)))
            except Exception as e:  # noqa: BLE001
                await self.io.emit(ev.info(f"MCP unavailable: {e}"))
        self._ensure_system()
        self.checkpointer.begin_turn()
        self._turn_mutated = set()
        self._heal_attempts = {}
        self._escalated = False
        self._fail_streak = 0

        if self.hooks.active:
            await asyncio.to_thread(self.hooks.run, "UserPromptSubmit", {"prompt": task})

        if self.plan_mode:
            task = "[PLAN MODE — research and propose a plan with update_plan; do NOT edit yet]\n" + task
        expanded = expand_mentions(task, self.ctx)
        # Ingest any dropped/referenced files, folders, or images.
        expanded, dropped_images = ingest.gather(expanded, self.ctx)
        all_images = list(images or []) + dropped_images
        if all_images:
            content = [{"type": "text", "text": expanded}] + [
                {"type": "image_url", "image_url": {"url": u}} for u in all_images
            ]
        else:
            content = expanded
        self.messages.append({"role": "user", "content": content})

        status = "done"
        try:
            await self._agent_loop()
            await self._maybe_verify_edits()
        except LLMError as e:
            status = "error"
            await self.io.emit(ev.error(f"LLM error: {e}"))
            await self.io.emit(ev.done("failed"))
        except Exception as e:  # noqa: BLE001
            status = "error"
            await self.io.emit(ev.error(f"Agent error: {type(e).__name__}: {e}"))
            await self.io.emit(ev.done("failed"))
        finally:
            if self.hooks.active:
                await asyncio.to_thread(self.hooks.run, "Stop", {})
            if self.persist and self.session_id:
                sessions.save(self.session_id, self.workspace, self.messages)
            await self._maybe_notify(status)

    async def _maybe_notify(self, status: str) -> None:
        notif = self.config.notifications
        if not notif or self.depth != 0:
            return
        if status not in notif.get("on", ["done"]):
            return
        summary = next(
            (m["content"] for m in reversed(self.messages)
             if m.get("role") == "assistant" and m.get("content")),
            "(no summary)",
        )
        from . import notify as _notify

        text = f"Euron Agent [{status}] in {self.workspace}\n{str(summary)[:1500]}"
        await asyncio.to_thread(_notify.dispatch, notif, text)

    async def _agent_loop(self) -> None:
        for step in range(self.config.agent.max_steps):
            if self._cancelled:
                await self.io.emit(ev.cancelled())
                await self.io.emit(ev.done("cancelled"))
                return

            if self.config.agent.compact_history:
                compacted, changed = compact_history(
                    self.messages, self.config.agent.max_context_tokens
                )
                if changed:
                    self.messages = compacted
                    await self.io.emit(ev.info("compacted older context to fit the window"))

            # Pick the model for this step: planner in plan mode, executor
            # otherwise — or the escalation model once the cheaper one has
            # struggled (cost-aware, but never at the expense of getting it done).
            phase = "plan" if self.plan_mode else "execute"
            if self._escalated and self.router.strategy != "fixed":
                phase = "escalate"
            # Reuse the injected/active client for phases that resolve to the active
            # model; only build a separate client when a distinct model is configured.
            client = (self.client if self.router.is_default_phase(phase)
                      else self.router.client_for_phase(phase))
            model_used = getattr(getattr(client, "provider", None), "model",
                                 self.config.provider.model)
            await self.io.emit(ev.status(f"thinking (step {step + 1}) · {model_used}"))

            resp = await asyncio.to_thread(
                client.chat,
                self.messages,
                self._tools_for_turn(),
                self.io.on_token,
                self.config.agent.stream,
            )

            self.session_tokens += resp.prompt_tokens + resp.completion_tokens
            self.session_cost += pricing.cost_for(
                model_used, resp.prompt_tokens, resp.completion_tokens,
                self.config.pricing,
            )
            await self.io.emit(
                ev.usage(
                    resp.prompt_tokens, resp.completion_tokens, self.session_tokens, self.session_cost
                )
            )

            if resp.content:
                await self.io.emit(ev.assistant_message(resp.content))

            if not resp.tool_calls:
                self.messages.append({"role": "assistant", "content": resp.content})
                await self.io.emit(ev.done(resp.content[:280]))
                return

            self.messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in resp.tool_calls
                    ],
                }
            )

            self._step_total = 0
            self._step_fail = 0
            for tc in resp.tool_calls:
                await self._handle_tool_call(tc)
            await self._maybe_escalate()

        await self.io.emit(ev.error("Reached max steps without finishing."))
        await self.io.emit(ev.done("max_steps"))

    async def _maybe_escalate(self) -> None:
        """Cost-aware quality guard: if every tool in a step failed for several
        steps running, jump to the stronger 'escalate' model for the rest of the
        turn. Cheap models do the easy work; the strong model rescues the hard parts."""
        if self._escalated or self.router.strategy == "fixed":
            return
        if self._step_total and self._step_fail == self._step_total:
            self._fail_streak += 1
        else:
            self._fail_streak = 0
        if (self._fail_streak >= self.router.escalate_after
                and self.router.has_distinct_escalation()):
            self._escalated = True
            self._fail_streak = 0
            await self.io.emit(ev.info(
                f"escalating to a stronger model ({self.router.model_for_phase('escalate')}) "
                "after repeated failures"))

    # ------------------------------------------------------------------ #
    async def _handle_tool_call(self, tc) -> None:
        self.tool_calls[tc.name] += 1
        if self._cancelled:
            msg = "Cancelled by user before execution."
            await self.io.emit(ev.tool_result(tc.id, tc.name, False, msg))
            self._append_tool_result(tc.id, msg)
            return

        # Loop-handled meta tools.
        if tc.name in LOOP_TOOLS:
            await self._handle_loop_tool(tc)
            return

        await self.io.emit(ev.tool_start(tc.id, tc.name, tc.arguments))

        # Sandbox / egress policy for shell commands — deny-by-default, pre-approval,
        # enforced even in dangerous mode so autonomous runs stay contained.
        if tc.name in ("run_command", "bash_background"):
            allowed, reason = sandbox.check_command(
                self.config.sandbox, tc.arguments.get("command", "")
            )
            if not allowed:
                msg = f"Command blocked by sandbox policy: {reason}"
                await self.io.emit(ev.tool_result(tc.id, tc.name, False, msg))
                self._append_tool_result(tc.id, msg)
                self._record_audit(tc.name, tc.arguments, "sandbox-deny", False, reason)
                self._note(False)
                return

        # Permission decision: allow / ask / deny. Dangerous mode allows everything.
        decision = "allow" if self.dangerous else self.permissions.decide(tc.name, tc.arguments)
        if decision == "deny":
            msg = f"Denied by permission policy: {tc.name} is not allowed."
            await self.io.emit(ev.tool_result(tc.id, tc.name, False, msg))
            self._append_tool_result(tc.id, msg)
            self._record_audit(tc.name, tc.arguments, "deny", False)
            self._note(False)
            return
        if decision == "ask":
            preview = (
                preview_for(self.ctx, tc.name, tc.arguments)
                if not is_mcp_tool(tc.name)
                else f"{tc.name}({json.dumps(tc.arguments)[:400]})"
            )
            ok = await self.io.request_approval(
                ev.approval_request(tc.id, tc.name, tc.arguments, preview)
            )
            if not ok.approved:
                note = ok.feedback or "no reason given"
                msg = f"User REJECTED this action. Reason: {note}. Do not retry it as-is."
                await self.io.emit(ev.tool_result(tc.id, tc.name, False, msg))
                self._append_tool_result(tc.id, msg)
                self._record_audit(tc.name, tc.arguments, "reject", False, note)
                self._note(False)
                return
            if ok.always:
                self.permissions.add_always_allow(tc.name, tc.arguments)
                await self.io.emit(ev.info(f"Always allowing {tc.name} for similar actions."))

        # PreToolUse hook — non-zero exit blocks the tool.
        if self.hooks.active:
            blocked, hookmsg = await asyncio.to_thread(
                self.hooks.run, "PreToolUse", {"tool": tc.name, "args": tc.arguments}
            )
            if blocked:
                msg = f"Blocked by PreToolUse hook: {hookmsg}"
                await self.io.emit(ev.tool_result(tc.id, tc.name, False, msg))
                self._append_tool_result(tc.id, msg)
                return

        # MCP routing (async).
        if is_mcp_tool(tc.name):
            out = await self.mcp.call(tc.name, tc.arguments)
            await self.io.emit(ev.tool_result(tc.id, tc.name, True, out))
            self._append_tool_result(tc.id, out)
            self._note(True)
            return

        if tc.name in _FILE_MUTATORS and tc.arguments.get("path"):
            try:
                self.checkpointer.record(self.ctx.resolve(tc.arguments["path"]))
                self._turn_mutated.add(tc.arguments["path"])
            except Exception:
                pass

        if tc.name == "run_command":
            def on_out(text: str, _id=tc.id):
                self.io.emit_sync(ev.command_output(_id, text))

            outcome = await asyncio.to_thread(
                run_command, self.ctx, tc.arguments.get("command", ""), on_out
            )
        else:
            outcome = await asyncio.to_thread(execute, self.ctx, tc.name, tc.arguments)

        if outcome.diff:
            await self.io.emit(ev.diff(tc.arguments.get("path", ""), outcome.diff, outcome.is_new))
        await self.io.emit(ev.tool_result(tc.id, tc.name, outcome.ok, outcome.output))
        self._record_audit(tc.name, tc.arguments, "allow", outcome.ok,
                           (outcome.output or "")[:200])
        self._note(outcome.ok)

        result_text = outcome.output or "(no output)"
        heal = self._self_heal_note(tc, outcome)
        if heal:
            result_text += "\n\n" + heal
        self._append_tool_result(tc.id, result_text)

        if self.hooks.active:
            await asyncio.to_thread(
                self.hooks.run, "PostToolUse",
                {"tool": tc.name, "args": tc.arguments, "ok": outcome.ok},
            )

    async def _handle_loop_tool(self, tc) -> None:
        if tc.name == "todo_write":
            self.todos = tc.arguments.get("todos", [])
            await self.io.emit(ev.todos(self.todos))
            done = sum(1 for t in self.todos if t.get("status") == "completed")
            self._append_tool_result(tc.id, f"Checklist updated ({done}/{len(self.todos)} done).")
            return

        if tc.name == "update_plan":
            plan_text = tc.arguments.get("plan", "")
            await self.io.emit(ev.plan(plan_text))
            decision = await self.io.request_approval(
                ev.approval_request(tc.id, "update_plan", {}, plan_text)
            )
            if decision.approved:
                self.plan_mode = False
                self._append_tool_result(
                    tc.id, "Plan APPROVED. Plan mode is now off — implement the plan."
                )
            else:
                note = decision.feedback or "revise it"
                self._append_tool_result(tc.id, f"Plan rejected: {note}. Revise the plan.")
            return

        if tc.name == "spawn_agent":
            await self._spawn_agent(tc)
            return

        if tc.name == "use_skill":
            name = tc.arguments.get("name", "")
            skill = self.skills.get(name)
            await self.io.emit(ev.tool_start(tc.id, "use_skill", {"name": name}))
            if skill:
                out = f"Skill '{name}':\n{skill['body']}"
            else:
                out = f"No such skill: {name}. Available: {', '.join(self.skills) or '(none)'}"
            await self.io.emit(ev.tool_result(tc.id, "use_skill", bool(skill), out[:300]))
            self._append_tool_result(tc.id, out)
            return

        self._append_tool_result(tc.id, f"Unknown meta-tool: {tc.name}")

    async def _spawn_agent(self, tc) -> None:
        if self.depth >= _MAX_SUBAGENT_DEPTH:
            self._append_tool_result(tc.id, "Sub-agents cannot spawn more sub-agents.")
            return
        # Agent-of-agent budget guard (shared across the whole sub-agent tree).
        if self._budget["calls"] >= self.config.agent.subagent_max_calls:
            self._append_tool_result(
                tc.id, f"Sub-agent budget exhausted "
                f"({self.config.agent.subagent_max_calls} calls). Do the work yourself.")
            return
        if self._budget["tokens"] >= self.config.agent.subagent_token_budget:
            self._append_tool_result(
                tc.id, "Sub-agent token budget exhausted. Finish without more sub-agents.")
            return
        self._budget["calls"] += 1
        self.subagent_calls += 1
        desc = tc.arguments.get("description", "sub-task")
        prompt = tc.arguments.get("prompt", "")
        await self.io.emit(ev.subagent_start(tc.id, desc))

        sub_cfg = self._cfg_for_phase("subagent")
        sub_io = _SubAgentIO(self.io)
        sub = AgentSession(self.workspace, sub_cfg, sub_io, depth=self.depth + 1,
                           audit=self.audit, budget=self._budget)
        await sub.run(prompt)
        self.session_tokens += sub.session_tokens
        self._budget["tokens"] += sub.session_tokens
        summary = sub_io.last_assistant or "(sub-agent produced no summary)"
        await self.io.emit(ev.subagent_end(tc.id, summary[:280]))
        self._append_tool_result(tc.id, f"Sub-agent '{desc}' result:\n{summary}")

    def _cfg_for_phase(self, phase: str):
        """A config whose ACTIVE provider is the model assigned to `phase` — so a
        spawned sub-agent runs on (say) the cheap model while still inheriting the
        full models/routing config for its own nested decisions."""
        prov = self.router.provider_for_phase(phase)
        if prov is self.config.provider:
            return self.config
        return replace(self.config, provider=prov)

    def _self_heal_note(self, tc, outcome) -> str:
        """When a test/build command fails and self_heal is on, nudge the agent to
        diagnose and fix it, bounded by `agent.self_heal` attempts per command."""
        limit = self.config.agent.self_heal
        if limit <= 0 or outcome.ok or tc.name != "run_command":
            return ""
        cmd = tc.arguments.get("command", "")
        if not re.search(r"(?i)\b(test|pytest|build|lint|tsc|mypy|cargo|gradle|jest|vitest)\b", cmd):
            return ""
        n = self._heal_attempts.get(cmd, 0) + 1
        self._heal_attempts[cmd] = n
        if n > limit:
            return (f"[self-heal] Gave up after {limit} attempt(s) on `{cmd}`. "
                    "Summarize the remaining failure for the user.")
        return (f"[self-heal {n}/{limit}] This command FAILED. Diagnose the root cause, "
                f"apply a fix, then re-run `{cmd}` to confirm it passes.")

    async def _maybe_verify_edits(self) -> None:
        """After a turn that changed files, optionally spawn a critic sub-agent that
        adversarially reviews the diff before the user trusts it."""
        if (not self.config.agent.verify_edits or self.depth != 0
                or not self._turn_mutated):
            return
        if self._budget["calls"] >= self.config.agent.subagent_max_calls:
            return
        files = ", ".join(sorted(self._turn_mutated)[:20])
        await self.io.emit(ev.info(f"verifying edits to: {files}"))
        prompt = (
            "You are a strict code reviewer. Review the changes just made to these "
            f"files: {files}. Use git_diff (and read_file) to inspect them. Check: "
            "does it compile/parse, does it match the apparent intent, did it break "
            "anything, are there bugs, security issues, or missing tests? Reply with "
            "a short verdict (APPROVE or NEEDS-WORK) and a bullet list of concrete "
            "issues. Do NOT edit files."
        )
        self._budget["calls"] += 1
        sub_io = _SubAgentIO(self.io)
        sub_cfg = self._cfg_for_phase("verify")
        sub = AgentSession(self.workspace, sub_cfg, sub_io, depth=self.depth + 1,
                           audit=self.audit, budget=self._budget)
        await sub.run(prompt)
        self.session_tokens += sub.session_tokens
        verdict = sub_io.last_assistant or "(no verdict)"
        await self.io.emit(ev.assistant_message("🔎 Verifier review:\n" + verdict))

    def _append_tool_result(self, call_id: str, content: str) -> None:
        # Bound very large tool outputs kept in context (keep head + tail).
        if content and len(content) > _MAX_TOOL_RESULT_CHARS:
            head = _MAX_TOOL_RESULT_CHARS * 2 // 3
            tail = _MAX_TOOL_RESULT_CHARS - head
            content = (content[:head] + "\n…[output trimmed to save context]…\n" + content[-tail:])
        self.messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
