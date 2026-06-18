"""Tool implementations. Every tool is workspace-sandboxed.

A tool returns a `ToolOutcome`:
  * output  — text fed back to the model
  * ok      — whether it succeeded
  * diff    — unified diff (for file-mutating tools, used for the approval UI)
  * is_new  — whether a file is being created (affects diff rendering)
"""
from __future__ import annotations

import difflib
import fnmatch
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import AgentConfig


@dataclass
class ToolOutcome:
    output: str
    ok: bool = True
    diff: Optional[str] = None
    is_new: bool = False


class ToolContext:
    def __init__(
        self,
        workspace: str,
        agent_cfg: AgentConfig,
        ignore: list[str],
        web: Optional[dict] = None,
    ):
        self.root = Path(workspace).resolve()
        self.cfg = agent_cfg
        self.ignore = ignore
        # web search backend, e.g. {"provider": "duckduckgo", "api_key": ""}
        self.web = web or {}

    # --- sandbox helpers --------------------------------------------------- #
    def resolve(self, rel: str) -> Path:
        full = (self.root / rel).resolve()
        try:
            full.relative_to(self.root)
        except ValueError:
            raise PermissionError(f"Path '{rel}' is outside the workspace.")
        return full

    def rel(self, full: Path) -> str:
        return str(full.relative_to(self.root)).replace(os.sep, "/")

    def is_ignored(self, rel_path: str) -> bool:
        rel_path = rel_path.replace(os.sep, "/")
        for pat in self.ignore:
            if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(
                rel_path, pat.rstrip("/*") + "/*"
            ):
                return True
            # match a directory prefix like ".git/**"
            top = pat.split("/", 1)[0]
            if rel_path == top or rel_path.startswith(top + "/"):
                if "**" in pat or "/" in pat:
                    return True
        return False


def _is_binary(path: Path) -> bool:
    """Heuristic: a NUL byte in the first 4 KB means it's not text."""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(4096)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Read-only tools
# --------------------------------------------------------------------------- #
def list_files(ctx: ToolContext, path: str = ".") -> ToolOutcome:
    base = ctx.resolve(path)
    if not base.exists():
        return ToolOutcome(f"Path not found: {path}", ok=False)
    rows: list[str] = []
    for root, dirs, files in os.walk(base):
        # prune ignored dirs in-place for speed
        rroot = ctx.rel(Path(root))
        dirs[:] = [
            d for d in sorted(dirs) if not ctx.is_ignored(f"{rroot}/{d}".lstrip("./"))
        ]
        for f in sorted(files):
            rp = ctx.rel(Path(root) / f)
            if not ctx.is_ignored(rp):
                rows.append(rp)
        if len(rows) > 800:
            rows.append("... (truncated)")
            break
    return ToolOutcome("\n".join(rows) if rows else "(empty)")


def read_file(
    ctx: ToolContext,
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> ToolOutcome:
    if ctx.is_ignored(path):
        return ToolOutcome(f"Refusing to read ignored/secret file: {path}", ok=False)
    full = ctx.resolve(path)
    if not full.is_file():
        return ToolOutcome(f"File not found: {path}", ok=False)
    if full.stat().st_size > ctx.cfg.max_file_bytes:
        return ToolOutcome(
            f"File too large ({full.stat().st_size} bytes > "
            f"{ctx.cfg.max_file_bytes}). Read a line range instead.",
            ok=False,
        )
    if _is_binary(full):
        return ToolOutcome(
            f"{path} appears to be a binary/non-text file; cannot read as text.",
            ok=False,
        )
    text = full.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if start_line or end_line:
        s = max(1, start_line or 1)
        e = min(len(lines), end_line or len(lines))
        snippet = lines[s - 1 : e]
        numbered = "\n".join(f"{s + i}\t{ln}" for i, ln in enumerate(snippet))
        return ToolOutcome(numbered or "(empty range)")
    numbered = "\n".join(f"{i + 1}\t{ln}" for i, ln in enumerate(lines))
    return ToolOutcome(numbered or "(empty file)")


def search_text(ctx: ToolContext, query: str, glob: Optional[str] = None) -> ToolOutcome:
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--line-number", "--no-heading", "--color", "never", "-S"]
        if glob:
            cmd += ["--glob", glob]
        cmd += [query, "."]
        try:
            res = subprocess.run(
                cmd, cwd=ctx.root, capture_output=True, text=True, timeout=20
            )
            out = res.stdout.strip()
            return ToolOutcome(out[:8000] if out else "(no matches)")
        except Exception:
            pass  # fall through to python implementation
    # pure-python fallback
    hits: list[str] = []
    for root, dirs, files in os.walk(ctx.root):
        dirs[:] = [
            d
            for d in dirs
            if not ctx.is_ignored(f"{ctx.rel(Path(root))}/{d}".lstrip("./"))
        ]
        for f in files:
            rp = ctx.rel(Path(root) / f)
            if ctx.is_ignored(rp) or (glob and not fnmatch.fnmatch(f, glob)):
                continue
            try:
                with open(Path(root) / f, encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if query in line:
                            hits.append(f"{rp}:{i}:{line.rstrip()}")
                            if len(hits) >= 200:
                                return ToolOutcome("\n".join(hits) + "\n... (truncated)")
            except Exception:
                continue
    return ToolOutcome("\n".join(hits) if hits else "(no matches)")


def glob_files(ctx: ToolContext, pattern: str, path: str = ".") -> ToolOutcome:
    """Find files by glob pattern (supports ** for recursion)."""
    base = ctx.resolve(path)
    if not base.exists():
        return ToolOutcome(f"Path not found: {path}", ok=False)
    matches: list[str] = []
    try:
        for p in sorted(base.glob(pattern)):
            if p.is_file():
                rel = ctx.rel(p)
                if not ctx.is_ignored(rel):
                    matches.append(rel)
            if len(matches) >= 500:
                matches.append("... (truncated)")
                break
    except Exception as e:  # noqa: BLE001
        return ToolOutcome(f"glob error: {e}", ok=False)
    return ToolOutcome("\n".join(matches) if matches else "(no matches)")


# --------------------------------------------------------------------------- #
# Mutating tools (gated by approval in the loop)
# --------------------------------------------------------------------------- #
def _unified(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _prepare_write(ctx: ToolContext, path: str, content: str) -> ToolOutcome:
    """Build the diff/preview WITHOUT writing (used to ask for approval)."""
    if ctx.is_ignored(path):
        return ToolOutcome(f"Refusing to write ignored/secret path: {path}", ok=False)
    full = ctx.resolve(path)
    is_new = not full.exists()
    before = "" if is_new else full.read_text(encoding="utf-8", errors="replace")
    patch = _unified(path, before, content)
    if not patch and not is_new:
        return ToolOutcome(f"No change: {path} already has that content.", ok=True)
    return ToolOutcome(output="", ok=True, diff=patch, is_new=is_new)


def write_file(ctx: ToolContext, path: str, content: str) -> ToolOutcome:
    pre = _prepare_write(ctx, path, content)
    if not pre.ok:
        return pre
    full = ctx.resolve(path)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return ToolOutcome(f"Wrote {path} ({len(content)} bytes).", diff=pre.diff, is_new=pre.is_new)


def edit_file(
    ctx: ToolContext,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> ToolOutcome:
    if ctx.is_ignored(path):
        return ToolOutcome(f"Refusing to edit ignored/secret path: {path}", ok=False)
    full = ctx.resolve(path)
    if not full.is_file():
        return ToolOutcome(f"File not found: {path}", ok=False)
    before = full.read_text(encoding="utf-8", errors="replace")
    count = before.count(old_string)
    if count == 0:
        return ToolOutcome(
            f"`old_string` not found in {path}. Re-read the file; it must match "
            f"exactly (whitespace included).",
            ok=False,
        )
    if count > 1 and not replace_all:
        return ToolOutcome(
            f"`old_string` is not unique in {path} ({count} matches). Add more "
            f"surrounding context or set replace_all=true.",
            ok=False,
        )
    after = before.replace(old_string, new_string)
    patch = _unified(path, before, after)
    full.write_text(after, encoding="utf-8")
    n = count if replace_all else 1
    return ToolOutcome(f"Edited {path} ({n} replacement(s)).", diff=patch)


def _apply_edits(before: str, edits: list, path: str):
    """Apply a list of {old_string,new_string,replace_all} edits. Returns
    (after, error). `error` is None on success."""
    text = before
    for i, e in enumerate(edits):
        old = e.get("old_string", "")
        new = e.get("new_string", "")
        if not old:
            return text, f"edit {i}: empty old_string"
        cnt = text.count(old)
        if cnt == 0:
            return text, f"edit {i}: old_string not found in {path}"
        if cnt > 1 and not e.get("replace_all"):
            return text, f"edit {i}: old_string not unique ({cnt}); add context or replace_all"
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text, None


def multi_edit(ctx: ToolContext, path: str, edits: list) -> ToolOutcome:
    """Apply several edits to one file atomically (all-or-nothing)."""
    if ctx.is_ignored(path):
        return ToolOutcome(f"Refusing to edit ignored/secret path: {path}", ok=False)
    full = ctx.resolve(path)
    if not full.is_file():
        return ToolOutcome(f"File not found: {path}", ok=False)
    before = full.read_text(encoding="utf-8", errors="replace")
    after, err = _apply_edits(before, edits or [], path)
    if err:
        return ToolOutcome(err, ok=False)
    patch = _unified(path, before, after)
    full.write_text(after, encoding="utf-8")
    return ToolOutcome(f"Applied {len(edits)} edit(s) to {path}.", diff=patch)


def create_file(ctx: ToolContext, path: str, content: str) -> ToolOutcome:
    full = ctx.resolve(path)
    if full.exists():
        return ToolOutcome(f"File already exists: {path}. Use write_file/edit_file.", ok=False)
    return write_file(ctx, path, content)


def delete_file(ctx: ToolContext, path: str) -> ToolOutcome:
    if ctx.is_ignored(path):
        return ToolOutcome(f"Refusing to delete ignored/secret path: {path}", ok=False)
    full = ctx.resolve(path)
    if not full.is_file():
        return ToolOutcome(f"File not found: {path}", ok=False)
    before = full.read_text(encoding="utf-8", errors="replace")
    full.unlink()
    return ToolOutcome(f"Deleted {path}.", diff=_unified(path, before, ""))


def run_command(
    ctx: ToolContext,
    command: str,
    on_output: Optional[Callable[[str], None]] = None,
) -> ToolOutcome:
    """Run a shell command, streaming each output line to `on_output` if given.

    Output is merged (stdout+stderr) and bounded by a watchdog timer so a hung
    process is always killed after `max_command_seconds`.
    """
    try:
        proc = subprocess.Popen(
            command,
            cwd=ctx.root,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as e:  # noqa: BLE001
        return ToolOutcome(f"Failed to start command: {e}", ok=False)

    killed = {"v": False}

    def _kill():
        killed["v"] = True
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(ctx.cfg.max_command_seconds, _kill)
    timer.start()
    captured: list[str] = []
    try:
        if proc.stdout:
            for line in proc.stdout:
                captured.append(line)
                if on_output:
                    on_output(line)
        proc.wait()
    finally:
        timer.cancel()

    rc = proc.returncode
    out = "".join(captured)
    if len(out) > 8000:
        out = "… (truncated)\n" + out[-8000:]
    parts = [f"$ {command}", f"(exit {rc})"]
    if killed["v"]:
        parts.append(f"[timed out after {ctx.cfg.max_command_seconds}s — killed]")
    if out.strip():
        parts.append("--- output ---\n" + out)
    return ToolOutcome("\n".join(parts), ok=(rc == 0 and not killed["v"]))


# --------------------------------------------------------------------------- #
# Web tools
# --------------------------------------------------------------------------- #
def web_fetch(ctx: ToolContext, url: str) -> ToolOutcome:
    from . import webtools

    ok, text = webtools.web_fetch(url, max_chars=ctx.cfg.max_file_bytes)
    return ToolOutcome(text, ok=ok)


def web_search(ctx: ToolContext, query: str) -> ToolOutcome:
    from . import webtools

    provider = ctx.web.get("provider", "duckduckgo")
    ok, text = webtools.web_search(query, provider=provider, api_key=ctx.web.get("api_key", ""))
    return ToolOutcome(text, ok=ok)


# --------------------------------------------------------------------------- #
# Background processes
# --------------------------------------------------------------------------- #
def bash_background(ctx: ToolContext, command: str) -> ToolOutcome:
    from .background import manager

    pid = manager.start(str(ctx.root), command)
    return ToolOutcome(f"Started background process {pid}: {command}")


def process_output(ctx: ToolContext, id: str, tail: int = 100) -> ToolOutcome:
    from .background import manager

    return ToolOutcome(manager.output(id, tail))


def process_kill(ctx: ToolContext, id: str) -> ToolOutcome:
    from .background import manager

    return ToolOutcome(manager.kill(id))


def process_list(ctx: ToolContext) -> ToolOutcome:
    from .background import manager

    return ToolOutcome(manager.list_())


# --------------------------------------------------------------------------- #
# Git helpers (thin, safe wrappers)
# --------------------------------------------------------------------------- #
def _git(ctx: ToolContext, args: str) -> ToolOutcome:
    return run_command(ctx, f"git {args}")


def git_status(ctx: ToolContext) -> ToolOutcome:
    return _git(ctx, "status --short --branch")


def git_diff(ctx: ToolContext, path: str = "") -> ToolOutcome:
    return _git(ctx, f"diff -- {path}" if path else "diff")


def git_commit(ctx: ToolContext, message: str, all: bool = True) -> ToolOutcome:
    safe = message.replace('"', '\\"')
    add = "git add -A && " if all else ""
    return run_command(ctx, f'{add}git commit -m "{safe}"')


# --------------------------------------------------------------------------- #
# Dispatch table
# --------------------------------------------------------------------------- #
TOOL_FUNCS = {
    "list_files": list_files,
    "read_file": read_file,
    "search_text": search_text,
    "glob": glob_files,
    "write_file": write_file,
    "edit_file": edit_file,
    "multi_edit": multi_edit,
    "create_file": create_file,
    "delete_file": delete_file,
    "run_command": run_command,
    "web_fetch": web_fetch,
    "web_search": web_search,
    "bash_background": bash_background,
    "process_output": process_output,
    "process_kill": process_kill,
    "process_list": process_list,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_commit": git_commit,
}


def preview_for(ctx: ToolContext, name: str, args: dict) -> Optional[str]:
    """Build a human-readable preview shown in the approval prompt."""
    try:
        if name in ("write_file", "create_file"):
            pre = _prepare_write(ctx, args.get("path", ""), args.get("content", ""))
            return pre.diff or pre.output
        if name == "edit_file":
            full = ctx.resolve(args["path"])
            if not full.is_file():
                return f"(new edit target missing: {args['path']})"
            before = full.read_text(encoding="utf-8", errors="replace")
            old, new = args.get("old_string", ""), args.get("new_string", "")
            if old not in before:
                return f"⚠ old_string not found in {args['path']}"
            after = before.replace(old, new, -1 if args.get("replace_all") else 1)
            return _unified(args["path"], before, after)
        if name == "multi_edit":
            full = ctx.resolve(args["path"])
            if not full.is_file():
                return f"(edit target missing: {args['path']})"
            before = full.read_text(encoding="utf-8", errors="replace")
            after, err = _apply_edits(before, args.get("edits", []), args["path"])
            return _unified(args["path"], before, after) if not err else f"⚠ {err}"
        if name == "delete_file":
            return f"DELETE {args.get('path')}"
        if name == "run_command":
            return f"$ {args.get('command')}"
        if name == "bash_background":
            return f"$ {args.get('command')}  (background)"
        if name == "git_commit":
            return f"git commit -m \"{args.get('message', '')}\""
    except Exception as e:  # noqa: BLE001
        return f"(could not build preview: {e})"
    return None


def execute(ctx: ToolContext, name: str, args: dict) -> ToolOutcome:
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return ToolOutcome(f"Unknown tool: {name}", ok=False)
    try:
        return fn(ctx, **args)
    except TypeError as e:
        return ToolOutcome(f"Bad arguments for {name}: {e}", ok=False)
    except PermissionError as e:
        return ToolOutcome(str(e), ok=False)
    except Exception as e:  # noqa: BLE001
        return ToolOutcome(f"Tool {name} failed: {type(e).__name__}: {e}", ok=False)
