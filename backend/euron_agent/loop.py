"""The agentic loop.

`AgentSession` holds one conversation. `run(task)` processes a user turn: it
drives the LLM, executes tool calls (gating mutations behind approval), and
streams everything through AgentIO. It also handles cancellation, @file mentions,
context compaction, per-turn checkpoints (undo), streamed command output, token
usage, and optional cross-restart persistence.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from . import events as ev
from . import gitignore, history
from .checkpoints import Checkpointer
from .config import Config
from .context import compact_history, expand_mentions
from .events import AgentIO
from .llm import LLMError, build_client
from .prompts import system_prompt
from .tool_schemas import MUTATING_TOOLS, TOOL_SCHEMAS
from .tools import ToolContext, execute, list_files, preview_for, run_command

_FILE_MUTATORS = {"write_file", "edit_file", "create_file", "delete_file"}


class AgentSession:
    def __init__(
        self,
        workspace: str,
        config: Config,
        io: AgentIO,
        *,
        persist: bool = False,
    ):
        self.workspace = workspace
        self.config = config
        self.io = io
        self.client = build_client(config.provider, config.agent)

        ignore = list(config.ignore)
        if config.agent.use_gitignore:
            ignore += gitignore.load_gitignore_patterns(Path(workspace))
        self.ctx = ToolContext(workspace, config.agent, ignore)

        self.checkpointer = Checkpointer()
        self.session_tokens = 0
        self.persist = persist
        self._cancelled = False
        self.messages: list[dict] = history.load_history(workspace) if persist else []

    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        self._cancelled = True

    def undo(self) -> list[str]:
        return self.checkpointer.undo_last_turn()

    def _ensure_system(self) -> None:
        if not self.messages:
            tree = list_files(self.ctx).output
            self.messages.append(
                {"role": "system", "content": system_prompt(self.workspace, tree)}
            )
        elif self.messages[0].get("role") != "system":
            tree = list_files(self.ctx).output
            self.messages.insert(
                0, {"role": "system", "content": system_prompt(self.workspace, tree)}
            )

    def _auto_approved(self, name: str) -> bool:
        if name not in MUTATING_TOOLS:
            return self.config.agent.auto_approve_reads
        if name == "run_command":
            return self.config.agent.auto_approve_commands
        return self.config.agent.auto_approve_writes

    # ------------------------------------------------------------------ #
    async def run(self, task: str) -> None:
        self._cancelled = False
        self._ensure_system()
        self.checkpointer.begin_turn()
        self.messages.append({"role": "user", "content": expand_mentions(task, self.ctx)})
        try:
            await self._agent_loop()
        except LLMError as e:
            await self.io.emit(ev.error(f"LLM error: {e}"))
            await self.io.emit(ev.done("failed"))
        except Exception as e:  # noqa: BLE001
            await self.io.emit(ev.error(f"Agent error: {type(e).__name__}: {e}"))
            await self.io.emit(ev.done("failed"))
        finally:
            if self.persist:
                history.save_history(self.workspace, self.messages)

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

            await self.io.emit(ev.status(f"thinking (step {step + 1})"))

            resp = await asyncio.to_thread(
                self.client.chat,
                self.messages,
                TOOL_SCHEMAS,
                self.io.on_token,
                self.config.agent.stream,
            )

            self.session_tokens += resp.prompt_tokens + resp.completion_tokens
            await self.io.emit(
                ev.usage(resp.prompt_tokens, resp.completion_tokens, self.session_tokens)
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
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in resp.tool_calls
                    ],
                }
            )

            for tc in resp.tool_calls:
                await self._handle_tool_call(tc)

        await self.io.emit(ev.error("Reached max steps without finishing."))
        await self.io.emit(ev.done("max_steps"))

    async def _handle_tool_call(self, tc) -> None:
        # If cancelled mid-turn, still record a tool result so message pairing
        # stays valid (important for persistence/resume).
        if self._cancelled:
            msg = "Cancelled by user before execution."
            await self.io.emit(ev.tool_result(tc.id, tc.name, False, msg))
            self._append_tool_result(tc.id, msg)
            return

        await self.io.emit(ev.tool_start(tc.id, tc.name, tc.arguments))

        if tc.name in MUTATING_TOOLS and not self._auto_approved(tc.name):
            preview = preview_for(self.ctx, tc.name, tc.arguments)
            decision = await self.io.request_approval(
                ev.approval_request(tc.id, tc.name, tc.arguments, preview)
            )
            if not decision.approved:
                note = decision.feedback or "no reason given"
                msg = f"User REJECTED this action. Reason: {note}. Do not retry it as-is."
                await self.io.emit(ev.tool_result(tc.id, tc.name, False, msg))
                self._append_tool_result(tc.id, msg)
                return

        # Snapshot before a file mutation so the turn can be undone.
        if tc.name in _FILE_MUTATORS and tc.arguments.get("path"):
            try:
                self.checkpointer.record(self.ctx.resolve(tc.arguments["path"]))
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
        self._append_tool_result(tc.id, outcome.output or "(no output)")

    def _append_tool_result(self, call_id: str, content: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
