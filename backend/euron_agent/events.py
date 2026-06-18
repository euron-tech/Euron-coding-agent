"""Event protocol and the AgentIO interface.

The agent loop is transport-agnostic: it talks to the outside world only
through an `AgentIO` implementation. The CLI implements it with the terminal;
the FastAPI server implements it over a WebSocket. Both speak the same set of
event dicts so the VS Code webview and the CLI render identical information.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional


# --------------------------------------------------------------------------- #
# Event constructors (plain dicts so they serialize straight to JSON)
# --------------------------------------------------------------------------- #
def status(message: str) -> dict:
    return {"type": "status", "message": message}


def token(text: str) -> dict:
    """A streamed chunk of assistant text."""
    return {"type": "token", "text": text}


def assistant_message(text: str) -> dict:
    """A complete assistant message (sent once a turn finishes)."""
    return {"type": "assistant_message", "text": text}


def tool_start(call_id: str, name: str, args: dict) -> dict:
    return {"type": "tool_start", "id": call_id, "name": name, "args": args}


def tool_result(call_id: str, name: str, ok: bool, output: str) -> dict:
    return {"type": "tool_result", "id": call_id, "name": name, "ok": ok, "output": output}


def diff(path: str, patch: str, is_new: bool = False) -> dict:
    return {"type": "diff", "path": path, "patch": patch, "is_new": is_new}


def approval_request(call_id: str, name: str, args: dict, preview: Optional[str]) -> dict:
    return {
        "type": "approval_request",
        "id": call_id,
        "name": name,
        "args": args,
        "preview": preview,
    }


def done(summary: str = "") -> dict:
    return {"type": "done", "summary": summary}


def error(message: str) -> dict:
    return {"type": "error", "message": message}


def command_output(call_id: str, text: str) -> dict:
    """A streamed chunk of stdout/stderr from a running command."""
    return {"type": "command_output", "id": call_id, "text": text}


def usage(prompt: int, completion: int, total_session: int) -> dict:
    return {
        "type": "usage",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "session_tokens": total_session,
    }


def cancelled() -> dict:
    return {"type": "cancelled"}


def info(message: str) -> dict:
    """A neutral notice (compaction, checkpoint, undo, …)."""
    return {"type": "info", "message": message}


def thinking(text: str) -> dict:
    """A streamed chunk of the model's reasoning."""
    return {"type": "thinking", "text": text}


def plan(text: str) -> dict:
    """A proposed plan emitted in plan mode, awaiting approval."""
    return {"type": "plan", "text": text}


def todos(items: list) -> dict:
    """The agent's current task checklist."""
    return {"type": "todos", "items": items}


def subagent_start(call_id: str, description: str) -> dict:
    return {"type": "subagent_start", "id": call_id, "description": description}


def subagent_end(call_id: str, summary: str) -> dict:
    return {"type": "subagent_end", "id": call_id, "summary": summary}


# --------------------------------------------------------------------------- #
# Approval decision + IO interface
# --------------------------------------------------------------------------- #
@dataclass
class ApprovalDecision:
    approved: bool
    feedback: Optional[str] = None  # optional user note fed back to the model


class AgentIO(abc.ABC):
    """How the agent loop emits output and requests human approval."""

    @abc.abstractmethod
    def emit_sync(self, event: dict) -> None:
        """Emit an event from anywhere — including a worker thread. Used for
        streamed `token` and `command_output`. Implementations must be
        thread-safe / non-blocking."""

    @abc.abstractmethod
    async def emit(self, event: dict) -> None:
        """Emit a structured event (always from the event loop)."""

    @abc.abstractmethod
    async def request_approval(self, request: dict) -> ApprovalDecision:
        """Ask the human to approve a gated action and block until answered."""

    # Back-compat convenience used by the LLM stream callback.
    def on_token(self, text: str) -> None:
        self.emit_sync(token(text))
