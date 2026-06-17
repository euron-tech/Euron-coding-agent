"""Best-effort .gitignore → ignore-glob conversion.

This is intentionally lightweight (no `pathspec` dependency). It converts the
common .gitignore patterns into the fnmatch-style globs that ``ToolContext``
already understands. Negations (`!pattern`) are skipped — we only ever *add*
ignores, never un-ignore, which is the safe direction for an agent.
"""
from __future__ import annotations

from pathlib import Path


def load_gitignore_patterns(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.is_file():
        return []
    patterns: list[str] = []
    try:
        lines = gi.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        # strip a leading slash (anchored to root); we match on relative paths
        line = line.lstrip("/")
        # a trailing slash means "a directory"
        is_dir = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        patterns.append(line)
        # match contents of a directory too
        patterns.append(f"{line}/**")
        if not is_dir and "/" not in line and "*" not in line:
            # a bare name like "node_modules" should match at any depth
            patterns.append(f"**/{line}")
            patterns.append(f"**/{line}/**")
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for p in patterns:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
