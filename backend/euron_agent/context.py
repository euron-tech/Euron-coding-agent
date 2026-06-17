"""Context-window management and @file mention expansion.

- `estimate_tokens` — a cheap, dependency-free token estimate (~4 chars/token).
- `expand_mentions` — turns `@path/to/file` in a task into inlined file content.
- `compact_history` — when the conversation gets too big, trims the oldest tool
  outputs (safe: it never breaks the assistant→tool message pairing) so the
  request stays under the model's budget.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tools import ToolContext

_MENTION_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_./\\\-]+)")
_TRIM_NOTE = "[older tool output trimmed to save context]"


def estimate_tokens(messages: list[dict]) -> int:
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        for tc in m.get("tool_calls") or []:
            chars += len(str(tc.get("function", {}).get("arguments", "")))
    return chars // 4


def expand_mentions(task: str, ctx: "ToolContext") -> str:
    """Inline the contents of any @-mentioned files that exist in the workspace."""
    seen: set[str] = set()
    blocks: list[str] = []
    for match in _MENTION_RE.finditer(task):
        rel = match.group(1).strip(".,;:)")
        if rel in seen:
            continue
        seen.add(rel)
        try:
            if ctx.is_ignored(rel):
                continue
            full = ctx.resolve(rel)
            if not full.is_file():
                continue
            if full.stat().st_size > ctx.cfg.max_file_bytes:
                blocks.append(f"--- @{rel} (too large to inline) ---")
                continue
            text = full.read_text(encoding="utf-8", errors="replace")
            blocks.append(f"--- @{rel} ---\n{text}")
        except Exception:
            continue
    if not blocks:
        return task
    return task + "\n\nReferenced files:\n" + "\n\n".join(blocks)


def compact_history(messages: list[dict], max_tokens: int, keep_recent: int = 6):
    """Return (messages, changed). Trims oldest tool outputs when over budget."""
    if estimate_tokens(messages) <= max_tokens:
        return messages, False
    out = [dict(m) for m in messages]
    # never touch the system message (0) or the most recent `keep_recent`
    end = max(1, len(out) - keep_recent)
    changed = False
    for i in range(1, end):
        if estimate_tokens(out) <= max_tokens:
            break
        m = out[i]
        if m.get("role") == "tool" and m.get("content") != _TRIM_NOTE:
            m["content"] = _TRIM_NOTE
            changed = True
    return out, changed
