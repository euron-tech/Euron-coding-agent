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
        deploy_cfg: Optional[dict] = None,
    ):
        self.root = Path(workspace).resolve()
        self.cfg = agent_cfg
        self.ignore = ignore
        # web search backend, e.g. {"provider": "duckduckgo", "api_key": ""}
        self.web = web or {}
        # auto-deploy policy (free_tier_only / allow_billable / cost_ceiling_usd)
        self.deploy_cfg = deploy_cfg or {}

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
# Code intelligence / security (read-only)
# --------------------------------------------------------------------------- #
def repo_map(ctx: ToolContext, path: str = ".", lang: str = "") -> ToolOutcome:
    """Compact symbol/outline map of the repo — read this before reading files."""
    from . import repomap

    return ToolOutcome(repomap.build_map(ctx, path, lang or None))


def secret_scan(ctx: ToolContext, path: str = ".") -> ToolOutcome:
    """Scan the workspace for hard-coded secrets/credentials."""
    from . import secrets

    count, report = secrets.scan(ctx, path)
    return ToolOutcome(report, ok=(count == 0))


def dependency_audit(ctx: ToolContext) -> ToolOutcome:
    """Audit project dependencies for known vulnerabilities."""
    from . import depaudit

    clean, report = depaudit.audit(ctx)
    return ToolOutcome(report, ok=clean)


# --------------------------------------------------------------------------- #
# Deploy / monitor / self-heal
# --------------------------------------------------------------------------- #
def deploy_check(ctx: ToolContext) -> ToolOutcome:
    """List deploy targets and whether each is ready (CLI installed + token set)."""
    from . import deploy as _dep

    rows = _dep.readiness()
    ready = [r for r in rows if r["ready"]]
    lines = [f"Deploy targets ({len(ready)} ready):"]
    for r in rows:
        mark = "✔" if r["ready"] else ("·" if r["cli_ok"] else "✗")
        tier = "free" if r["free_tier"] and not r["billable"] else "billable"
        lines.append(f"  {mark} {r['name']:<14} [{r['kind']}/{tier}] cli={r['cli']} "
                     f"token={r['token_env']} {'READY' if r['ready'] else ''}")
    lines.append("\nSuggested default: " + (_dep.suggest() or "(none)"))
    return ToolOutcome("\n".join(lines))


def deploy(ctx: ToolContext, target: str, params: Optional[dict] = None) -> ToolOutcome:
    """Deploy the current project to a target (gated by the spend policy)."""
    from . import deploy as _dep
    from . import guardrails

    decision, reason = guardrails.spend_gate(ctx.deploy_cfg, target)
    if decision == "deny":
        return ToolOutcome(f"Deploy to {target} blocked: {reason}", ok=False)
    ok, cmd = _dep.deploy_command(target, params)
    if not ok:
        return ToolOutcome(cmd, ok=False)
    if decision == "ask":
        # Surface to the agent; the run_command approval gate is the human checkpoint.
        cmd = cmd  # the agent will run this via run_command after confirming with the user
        out = run_command(ctx, cmd)
        note = f"[spend-gate: {reason}]\n"
        return ToolOutcome(note + out.output, ok=out.ok)
    out = run_command(ctx, cmd)
    return ToolOutcome(out.output, ok=out.ok)


def rollback(ctx: ToolContext, target: str, params: Optional[dict] = None) -> ToolOutcome:
    """Roll a target back to its previous known-good release (safe to automate)."""
    from . import deploy as _dep

    ok, cmd = _dep.rollback_command(target, params)
    if not ok:
        return ToolOutcome(cmd, ok=False)
    return run_command(ctx, cmd)


def provision_db(ctx: ToolContext, provider: str, params: Optional[dict] = None) -> ToolOutcome:
    """Provision a managed database (neon / supabase)."""
    from . import deploy as _dep

    ok, cmd = _dep.deploy_command(provider, params)
    if not ok:
        return ToolOutcome(cmd, ok=False)
    return run_command(ctx, cmd)


def monitor_status(ctx: ToolContext) -> ToolOutcome:
    """Report configured monitoring providers and current error/uptime status."""
    from . import monitor

    return ToolOutcome(monitor.status())


def health_check(ctx: ToolContext, url: str, path: str = "/health") -> ToolOutcome:
    """Check a deployed app's health endpoint."""
    from . import selfheal

    healthy, detail = selfheal.check_health(url, path)
    return ToolOutcome(detail, ok=healthy)


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
# Git worktrees — isolated copies to work safely in parallel
# --------------------------------------------------------------------------- #
def worktree_add(ctx: ToolContext, name: str, branch: str = "") -> ToolOutcome:
    rel = f".euron/worktrees/{name}"
    flag = f"-b {branch} " if branch else ""
    out = run_command(ctx, f"git worktree add {flag}{rel}")
    if out.ok:
        out.output += f"\n(isolated worktree at {rel}; run commands there with: cd {rel} && …)"
    return out


def worktree_list(ctx: ToolContext) -> ToolOutcome:
    return run_command(ctx, "git worktree list")


def worktree_remove(ctx: ToolContext, name: str) -> ToolOutcome:
    return run_command(ctx, f"git worktree remove .euron/worktrees/{name} --force")


# --------------------------------------------------------------------------- #
# CI / PR helpers
# --------------------------------------------------------------------------- #
def git_branch(ctx: ToolContext, name: str) -> ToolOutcome:
    return run_command(ctx, f"git checkout -b {name}")


def git_push(ctx: ToolContext, branch: str = "") -> ToolOutcome:
    target = branch or "HEAD"
    return run_command(ctx, f"git push -u origin {target}")


def open_pr(ctx: ToolContext, title: str, body: str = "") -> ToolOutcome:
    if not shutil.which("gh"):
        return ToolOutcome("GitHub CLI (gh) not found — install it to open PRs.", ok=False)
    safe_t = title.replace('"', '\\"')
    safe_b = body.replace('"', '\\"')
    return run_command(ctx, f'gh pr create --title "{safe_t}" --body "{safe_b}"')


# --------------------------------------------------------------------------- #
# Dispatch table
# --------------------------------------------------------------------------- #
TOOL_FUNCS = {
    "list_files": list_files,
    "read_file": read_file,
    "search_text": search_text,
    "glob": glob_files,
    "repo_map": repo_map,
    "secret_scan": secret_scan,
    "dependency_audit": dependency_audit,
    "deploy_check": deploy_check,
    "deploy": deploy,
    "rollback": rollback,
    "provision_db": provision_db,
    "monitor_status": monitor_status,
    "health_check": health_check,
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
    "worktree_add": worktree_add,
    "worktree_list": worktree_list,
    "worktree_remove": worktree_remove,
    "git_branch": git_branch,
    "git_push": git_push,
    "open_pr": open_pr,
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
        if name in ("deploy", "provision_db"):
            from . import deploy as _dep
            tgt = args.get("target") or args.get("provider", "")
            ok, cmd = _dep.deploy_command(tgt, args.get("params"))
            return f"DEPLOY {tgt}: $ {cmd}" if ok else f"⚠ {cmd}"
        if name == "rollback":
            from . import deploy as _dep
            ok, cmd = _dep.rollback_command(args.get("target", ""), args.get("params"))
            return f"ROLLBACK: $ {cmd}" if ok else f"⚠ {cmd}"
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
