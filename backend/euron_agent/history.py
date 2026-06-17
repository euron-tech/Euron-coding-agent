"""Persist conversation history per workspace so context survives restarts.

Stored under ``~/.euron-agent/sessions/<hash>.json``. The session saves after
every turn and can reload on startup, so closing the CLI or reloading the VS Code
window no longer wipes the conversation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .settings import SETTINGS_DIR

SESSIONS_DIR = SETTINGS_DIR / "sessions"


def _session_file(workspace: str) -> Path:
    key = hashlib.sha1(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:16]
    return SESSIONS_DIR / f"{key}.json"


def load_history(workspace: str) -> list[dict]:
    f = _session_file(workspace)
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data.get("messages", []) if isinstance(data, dict) else []
    except Exception:
        return []


def save_history(workspace: str, messages: list[dict]) -> None:
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        _session_file(workspace).write_text(
            json.dumps({"workspace": str(Path(workspace).resolve()), "messages": messages}),
            encoding="utf-8",
        )
    except Exception:
        pass


def clear_history(workspace: str) -> None:
    try:
        _session_file(workspace).unlink()
    except Exception:
        pass
