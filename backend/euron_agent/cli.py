"""Command-line interface.

Just run `euron-agent` to drop into an interactive chat (Claude-CLI style) where
you configure everything in-session:

    euron-agent                       # interactive chat in the current folder
    euron-agent run "add a /health route to app.py"
    euron-agent serve --port 0        # API/WebSocket server (0 = auto-port)
    euron-agent providers             # list providers
    euron-agent init                  # scaffold config.yaml + .env (optional)

In chat, configure with slash commands (persisted to ~/.euron-agent/config.json):
    /provider [name]   /key [value]   /model [name]   /baseurl [url]
    /config   /providers   /reset   /yes   /help   /exit
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from . import settings as user_settings
from .config import BUILTIN_PROVIDERS, load_config
from .events import AgentIO, ApprovalDecision
from .llm import build_client
from .loop import AgentSession


def _force_utf8() -> None:
    """Avoid UnicodeEncodeError for box-drawing/emoji on Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


_force_utf8()
console = Console(legacy_windows=False)

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_VERBS = ["Thinking", "Working", "Pondering", "Forging", "Composing",
          "Reasoning", "Crafting", "Synthesizing", "Exploring"]


# --------------------------------------------------------------------------- #
# Terminal IO
# --------------------------------------------------------------------------- #
class TerminalIO(AgentIO):
    """Terminal renderer with a Claude-Code-style live status footer (spinner,
    rotating verb, elapsed time, live tokens/cost, tool & sub-agent counters, and
    the current activity), streamed tokens, and a completion summary."""

    def __init__(self, auto_approve: bool):
        self.auto_approve = auto_approve
        self._tty = sys.stdout.isatty()
        self._live = None
        self._ticker = None
        self._stream = ""        # in-progress streamed text (assistant / command)
        self._activity = ""      # current tool / sub-agent label
        self._start = 0.0
        self.tokens = 0
        self.cost = 0.0
        self.tool_count = 0
        self.agent_count = 0
        self.steps: list = []
        self._finished = True
        self._dirty = False  # plain-mode: unfinished streamed line on screen

    # ----- live status footer -------------------------------------------- #
    def begin_run(self) -> None:
        self._stream = ""
        self._activity = ""
        self.steps = []
        self.tool_count = self.agent_count = self.tokens = 0
        self.cost = 0.0
        self._finished = False
        self._start = time.monotonic()
        if self._tty:
            self._start_live()

    def _start_live(self) -> None:
        try:
            from rich.live import Live

            self._live = Live(self._render(), console=console, refresh_per_second=12)
            self._live.start()
            self._ticker = asyncio.create_task(self._tick())
        except Exception:
            self._live = None

    async def _tick(self) -> None:
        try:
            while self._live is not None:
                await asyncio.sleep(0.1)
                if self._live is not None:
                    self._live.update(self._render())
        except asyncio.CancelledError:
            pass

    def _render(self):
        from rich.console import Group
        from rich.text import Text

        body = Text(self._stream)
        elapsed = time.monotonic() - self._start
        verb = _VERBS[int(elapsed / 2) % len(_VERBS)]
        spin = _SPINNER[int(elapsed * 10) % len(_SPINNER)]
        tok = f"{self.tokens / 1000:.1f}k" if self.tokens >= 1000 else str(self.tokens)
        footer = Text()
        footer.append(f"  {spin} ", style="cyan")
        footer.append(f"{verb}… ", style="bold cyan")
        footer.append(f"{elapsed:.0f}s · {tok} tok", style="dim")
        if self.cost:
            footer.append(f" · ${self.cost:.4f}", style="dim")
        footer.append(f" · {self.tool_count} tools", style="dim")
        if self.agent_count:
            footer.append(f" · {self.agent_count} agents", style="dim")
        if self._activity:
            footer.append(f"  ⤷ {self._activity}", style="yellow")
        return Group(body, footer)

    def _print(self, renderable) -> None:
        (self._live.console if self._live is not None else console).print(renderable)

    def _flush_stream(self) -> None:
        if self._live is not None:
            if self._stream:
                from rich.text import Text

                self._print(Text(self._stream))   # commit streamed text above footer
                self._stream = ""
        elif self._dirty:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._dirty = False

    def emit_sync(self, event: dict) -> None:
        if event.get("type") in ("token", "command_output"):
            if self._live is not None:
                self._stream += event["text"]
            else:
                sys.stdout.write(event["text"])
                sys.stdout.flush()
                self._dirty = True

    async def emit(self, event: dict) -> None:
        t = event["type"]
        if t in ("status", "token"):
            return
        self._flush_stream()  # commit any in-progress streamed text first
        if t == "assistant_message":
            return  # already streamed + committed
        elif t == "tool_start":
            self.tool_count += 1
            args = event["args"]
            detail = args.get("path") or args.get("command") or args.get("query") or args.get("name") or ""
            label = f"{event['name']} {detail}".strip()
            self._activity = label
            self.steps.append(label)
            self._print(f"[cyan]⚙ {event['name']}[/cyan] [dim]{detail}[/dim]")
        elif t == "diff":
            self._print_diff(event["patch"])
        elif t == "tool_result":
            self._activity = ""
            mark = "[green]✓[/green]" if event["ok"] else "[red]✗[/red]"
            out = (event["output"] or "").strip()
            if out:
                snippet = out if len(out) < 1200 else out[:1200] + " …"
                self._print(f"{mark} [dim]{snippet}[/dim]")
        elif t == "error":
            self._print(f"[red]error:[/red] {event['message']}")
        elif t == "usage":
            self.tokens = event.get("session_tokens", self.tokens)
            self.cost = event.get("session_cost", self.cost)
        elif t == "info":
            self._print(f"[dim]ℹ {event['message']}[/dim]")
        elif t == "thinking":
            self._print(f"[dim]💭 {event['text']}[/dim]")
        elif t == "plan":
            self._print(Panel(event["text"], title="Proposed plan", border_style="magenta"))
        elif t == "todos":
            marks = {"completed": "[green]✔[/green]", "in_progress": "[yellow]▸[/yellow]", "pending": "[dim]○[/dim]"}
            self._print("[bold]Tasks:[/bold]")
            for item in event["items"]:
                self._print(f"  {marks.get(item.get('status'), '○')} {item.get('content', '')}")
        elif t == "subagent_start":
            self.agent_count += 1
            self._activity = f"sub-agent: {event['description']}"
            self.steps.append(f"sub-agent: {event['description']}")
            self._print(f"[cyan]↳ sub-agent:[/cyan] {event['description']}")
        elif t == "subagent_end":
            self._activity = ""
            self._print(f"[cyan]↳ sub-agent done[/cyan] [dim]{event['summary']}[/dim]")
        elif t == "cancelled":
            self._print("[yellow]■ cancelled[/yellow]")
        elif t == "done":
            self._finish()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._flush_stream()
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        if self._live is not None:
            try:
                from rich.text import Text

                self._live.update(Text(""))
                self._live.stop()
            except Exception:
                pass
            self._live = None
        elapsed = time.monotonic() - self._start
        cost = f" · ${self.cost:.4f}" if self.cost else ""
        lines = [f"completed in {elapsed:.0f}s · {self.tokens} tokens{cost} · "
                 f"{self.tool_count} tool calls · {self.agent_count} sub-agents"]
        if self.steps:
            lines.append("")
            lines.append("[bold]Steps taken:[/bold]")
            for i, s in enumerate(self.steps[:25], 1):
                lines.append(f"  {i}. {s}")
            if len(self.steps) > 25:
                lines.append(f"  … and {len(self.steps) - 25} more")
        console.print(Panel("\n".join(lines), title="Summary", border_style="green"))

    def _pause_live(self) -> bool:
        if self._live is None:
            return False
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        try:
            self._live.stop()
        except Exception:
            pass
        self._live = None
        return True

    def _print_diff(self, patch: str) -> None:
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                self._print(f"[green]{line}[/green]")
            elif line.startswith("-") and not line.startswith("---"):
                self._print(f"[red]{line}[/red]")
            elif line.startswith("@@"):
                self._print(f"[magenta]{line}[/magenta]")
            else:
                self._print(f"[dim]{line}[/dim]")

    async def request_approval(self, request: dict) -> ApprovalDecision:
        self._flush_stream()
        resume = self._pause_live()
        preview = request.get("preview") or ""
        title = f"Approve {request['name']}?"
        if preview:
            if "\n" in preview and ("+++" in preview or "@@" in preview):
                self._print_diff(preview)
            else:
                console.print(Panel(preview, title=title, border_style="yellow"))
        if self.auto_approve:
            console.print("[green]auto-approved[/green]")
            if resume:
                self._start_live()
            return ApprovalDecision(approved=True)
        try:
            answer = await asyncio.to_thread(
                Prompt.ask,
                f"[yellow]{title}[/yellow] (y=yes, a=always, n=no, or type feedback)",
                default="y",
            )
        finally:
            if resume:
                self._start_live()
        a = answer.strip().lower()
        if a in ("y", "yes", ""):
            return ApprovalDecision(approved=True)
        if a in ("a", "always"):
            return ApprovalDecision(approved=True, always=True)
        if a in ("n", "no"):
            return ApprovalDecision(approved=False, feedback="rejected by user")
        return ApprovalDecision(approved=False, feedback=answer.strip())


# --------------------------------------------------------------------------- #
# Config resolution (CLI args + persisted user settings)
# --------------------------------------------------------------------------- #
def resolve_config(args):
    """Merge built-ins ← config.yaml ← ~/.euron-agent ← CLI flags."""
    s = user_settings.load()
    base = load_config(args.config)
    provider = args.provider or s.get("provider") or base.provider.name
    over = (s.get("providers") or {}).get(provider, {})
    cfg = load_config(
        args.config,
        provider=provider,
        model=args.model or over.get("model"),
        api_key=over.get("api_key"),
        base_url=over.get("base_url"),
    )
    if getattr(args, "yes", False):
        cfg.agent.auto_approve_writes = True
        cfg.agent.auto_approve_commands = True
    return cfg


def _key_missing(cfg) -> bool:
    p = cfg.provider
    if p.api_key:
        return False
    if not p.api_key_env:  # e.g. ollama / custom — no key required
        return False
    return not os.getenv(p.api_key_env)


def _reload(session: AgentSession, args) -> None:
    cfg = resolve_config(args)
    session.config = cfg
    session.ctx.cfg = cfg.agent
    session.client = build_client(cfg.provider, cfg.agent)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def _run_task(task: str, args) -> None:
    cfg = resolve_config(args)
    workspace = str(Path(args.workspace).resolve())
    console.print(
        f"[dim]workspace={workspace} · provider={cfg.provider.name} · "
        f"model={cfg.provider.model}[/dim]"
    )
    if _key_missing(cfg):
        console.print(
            f"[red]No API key for '{cfg.provider.name}'.[/red] Run "
            f"[bold]euron-agent[/bold] and use /key, or set the env var "
            f"{cfg.provider.api_key_env}."
        )
        return
    dangerous = getattr(args, "dangerous", False)
    io = TerminalIO(auto_approve=args.yes or dangerous)
    io.begin_run()
    await AgentSession(workspace, cfg, io, dangerous=dangerous).run(task)


def cmd_run(args) -> None:
    if getattr(args, "json", False):
        from .headless import run_headless

        res = asyncio.run(run_headless(
            args.task, str(Path(args.workspace).resolve()),
            provider=args.provider, model=args.model,
            json_stream=True, team=getattr(args, "team_name", None),
        ))
        print(json.dumps({"type": "result", **res}))
    else:
        asyncio.run(_run_task(args.task, args))


def cmd_team(args) -> None:
    from . import teams

    rows = teams.list_teams()
    if rows:
        for t in rows:
            console.print(f"  [cyan]{t['id']}[/cyan]  {(t.get('title') or '')[:60]}")
        console.print("[dim]resume: euron-agent --team-name <name>[/dim]")
    else:
        console.print("[dim]no teams yet — start one with: euron-agent --team-name <name>[/dim]")


def cmd_schedule(args) -> None:
    from . import schedules

    if args.action == "create":
        if not (args.name and args.cron and args.prompt):
            console.print("[red]usage: schedule create <name> --cron \"...\" --prompt \"...\"[/red]")
            return
        s = schedules.create(args.name, args.cron, args.prompt,
                             str(Path(args.workspace).resolve()), args.provider, args.model)
        console.print(f"[green]created[/green] schedule {s['id']} · {s['cron']} · {s['name']}")
    elif args.action == "list":
        rows = schedules.list_schedules()
        if not rows:
            console.print("[dim]no schedules[/dim]")
        for r in rows:
            console.print(f"  [cyan]{r['id']}[/cyan]  {r['cron']:<18} {r['name']}  [dim]{r['workspace']}[/dim]")
    elif args.action == "remove":
        console.print("[green]removed[/green]" if schedules.remove(args.name or "") else "[yellow]not found[/yellow]")
    elif args.action == "run":
        from .headless import run_headless

        s = schedules.get(args.name or "")
        if not s:
            console.print("[red]no such schedule id[/red]")
            return
        res = asyncio.run(run_headless(s["prompt"], s["workspace"],
                                       provider=s.get("provider"), model=s.get("model")))
        console.print(res["final"][:1000])
    elif args.action == "daemon":
        asyncio.run(_schedule_daemon())


async def _schedule_daemon() -> None:
    from datetime import datetime

    from . import schedules
    from .headless import run_headless

    console.print("[cyan]Euron Agent scheduler[/cyan] running — Ctrl+C to stop")
    while True:
        now = datetime.now().replace(second=0, microsecond=0)
        for s in schedules.due(now):
            schedules.mark_run(s["id"], now.strftime("%Y-%m-%d %H:%M"))
            console.print(f"[green]▸ running[/green] {s['name']} ({s['id']})")
            try:
                await run_headless(s["prompt"], s["workspace"],
                                   provider=s.get("provider"), model=s.get("model"))
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]schedule error:[/red] {e}")
        await asyncio.sleep(60)


HELP = """[bold]commands[/bold]
  /provider [name]   switch provider (interactive if no name)
  /key [value]       set API key for the current provider (hidden prompt if blank)
  /model [name]      set the model for the current provider
  /baseurl [url]     set a custom base URL (self-hosted / custom endpoints)
  /config            show current provider, model, base URL, key status
  /providers         list known providers
  /plan              plan mode for the next task (research → approve → execute)
  /review            review the current git changes for bugs (like a code review)
  /compact           summarize the conversation to free up context
  /init              create an AGENTS.md project-memory file
  /skills            list available skills (.euron/skills/<name>/SKILL.md)
  /search <text>     search your past sessions
  /usage             show tokens, cost, and tool usage this session
  /effort <level>    reasoning effort: low | medium | high
  /undo              revert the file changes from the last task
  /reset             clear the conversation context
  /yes               toggle auto-approve for edits & commands
  /dangerous         toggle DANGEROUS mode (run everything, never ask)
  /help              show this help (Ctrl+C during a task = stop)
  /exit              quit"""


# (name, description) for every built-in slash command - powers /help and the
# "/" autocomplete popup.
SLASH_COMMANDS = [
    ("provider", "switch provider"),
    ("key", "set API key"),
    ("model", "set the model"),
    ("baseurl", "set a custom base URL"),
    ("config", "show current settings"),
    ("providers", "list known providers"),
    ("plan", "plan mode for the next task"),
    ("review", "review the current git changes"),
    ("compact", "summarize the conversation"),
    ("init", "create AGENTS.md memory"),
    ("skills", "list available skills"),
    ("search", "search past sessions"),
    ("usage", "tokens, cost, tool usage"),
    ("effort", "reasoning effort low|medium|high"),
    ("undo", "revert the last task's changes"),
    ("reset", "clear the conversation context"),
    ("yes", "toggle auto-approve"),
    ("dangerous", "toggle DANGEROUS mode (no prompts)"),
    ("help", "show help"),
    ("exit", "quit"),
]


def _build_prompt_session(workspace: str):
    """A prompt_toolkit session whose completer pops up all slash commands the
    moment you type '/'. Returns None if prompt_toolkit isn't available or stdin
    isn't an interactive terminal (e.g. piped input)."""
    try:
        if not sys.stdin.isatty():
            return None
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
    except Exception:
        return None

    cmds = list(SLASH_COMMANDS)
    try:
        from .commands import load_commands

        for name in load_commands(workspace):
            cmds.append((name, "custom command"))
    except Exception:
        pass

    class _SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            word = text[1:].lower()
            for name, desc in cmds:
                if name.lower().startswith(word):
                    yield Completion("/" + name, start_position=-len(text),
                                     display="/" + name, display_meta=desc)

    return PromptSession(completer=_SlashCompleter(), complete_while_typing=True)


def _print_providers() -> None:
    from rich.table import Table

    s = user_settings.load()
    cfg = load_config()
    table = Table(title="Providers")
    table.add_column("name")
    table.add_column("type")
    table.add_column("model")
    table.add_column("key", justify="center")
    for name, p in cfg.all_providers.items():
        over = (s.get("providers") or {}).get(name, {})
        has_key = bool(
            over.get("api_key") or (p.api_key_env and os.getenv(p.api_key_env))
        )
        needs = bool(p.api_key_env)
        key_mark = "✓" if has_key else ("—" if not needs else "[red]✗[/red]")
        table.add_row(name, p.type, over.get("model") or p.model, key_mark)
    console.print(table)


def _pick_provider() -> str:
    names = list(BUILTIN_PROVIDERS)
    for i, n in enumerate(names, 1):
        console.print(f"  [cyan]{i}[/cyan]. {n}")
    ans = Prompt.ask("provider (number or name)", default="").strip()
    if ans.isdigit() and 1 <= int(ans) <= len(names):
        return names[int(ans) - 1]
    return ans


async def _handle_command(line: str, session: AgentSession, args, io: TerminalIO) -> str:
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/exit", "/quit"):
        return "exit"
    if cmd == "/help":
        console.print(HELP)
    elif cmd == "/plan":
        session.plan_mode = True
        console.print("[magenta]plan mode ON[/magenta] — your next task will research & propose a plan first.")
    elif cmd == "/compact":
        from .context import summarize_history

        new, changed = summarize_history(session.client, session.messages)
        if changed:
            session.messages = new
            console.print("[green]conversation compacted[/green]")
        else:
            console.print("[dim]nothing to compact yet[/dim]")
    elif cmd == "/undo":
        reverted = session.undo()
        if reverted:
            console.print(f"[green]reverted {len(reverted)} file(s)[/green]")
            for p in reverted:
                console.print(f"  [dim]{p}[/dim]")
        else:
            console.print("[dim]nothing to undo[/dim]")
    elif cmd == "/reset":
        session.messages.clear()
        console.print("[dim]context cleared[/dim]")
    elif cmd == "/yes":
        io.auto_approve = not io.auto_approve
        console.print(f"[dim]auto-approve = {io.auto_approve}[/dim]")
    elif cmd in ("/dangerous", "/yolo"):
        session.dangerous = not session.dangerous
        io.auto_approve = session.dangerous or io.auto_approve
        if session.dangerous:
            console.print("[bold red]DANGEROUS MODE ON[/bold red] - the agent will now run "
                          "EVERYTHING (edits, commands, deletes) without asking. Use with care.")
        else:
            console.print("[green]dangerous mode OFF[/green] - approvals restored.")
    elif cmd == "/providers":
        _print_providers()
    elif cmd == "/config":
        p = session.config.provider
        keyset = bool(p.api_key or (p.api_key_env and os.getenv(p.api_key_env)))
        console.print(
            f"provider : [cyan]{p.name}[/cyan]\n"
            f"model    : {p.model}\n"
            f"base_url : {p.base_url or '(default)'}\n"
            f"api key  : {'[green]set[/green]' if keyset else '[red]not set[/red]'}"
        )
    elif cmd == "/provider":
        name = rest or await asyncio.to_thread(_pick_provider)
        if not name:
            pass
        elif name not in session.config.all_providers:
            console.print(f"[red]unknown provider:[/red] {name}  (/providers)")
        else:
            user_settings.set_active_provider(name)
            args.provider = name
            args.model = None  # let the new provider's own model apply
            _reload(session, args)
            console.print(
                f"[green]provider → {session.config.provider.name}[/green] "
                f"({session.config.provider.model})"
            )
            if _key_missing(session.config):
                console.print("[yellow]no API key for this provider — use /key[/yellow]")
    elif cmd == "/key":
        provider = session.config.provider.name
        value = rest or await asyncio.to_thread(
            getpass.getpass, f"API key for {provider} (hidden): "
        )
        if value.strip():
            user_settings.set_provider_field(provider, "api_key", value.strip())
            _reload(session, args)
            console.print(f"[green]key saved for {provider}[/green]")
    elif cmd == "/model":
        provider = session.config.provider.name
        value = rest or await asyncio.to_thread(Prompt.ask, "model")
        if value.strip():
            user_settings.set_provider_field(provider, "model", value.strip())
            args.model = value.strip()
            _reload(session, args)
            console.print(f"[green]model → {session.config.provider.model}[/green]")
    elif cmd == "/baseurl":
        provider = session.config.provider.name
        value = rest or await asyncio.to_thread(Prompt.ask, "base url")
        if value.strip():
            user_settings.set_provider_field(provider, "base_url", value.strip())
            _reload(session, args)
            console.print(f"[green]base_url → {session.config.provider.base_url}[/green]")
    elif cmd == "/init":
        from .memory import write_template

        p = write_template(session.workspace)
        console.print(f"[green]wrote {p.name}[/green] — edit it with project instructions.")
    elif cmd == "/usage":
        from . import pricing

        p = session.config.provider
        priced = pricing.is_priced(p.model, session.config.pricing)
        cost_str = f"${session.session_cost:.4f}" if priced else "[yellow]pricing not set[/yellow]"
        console.print(
            f"[bold]Usage this session[/bold]\n"
            f"  model: [cyan]{p.name}/{p.model}[/cyan]\n"
            f"  tokens: {session.session_tokens}  ·  cost: {cost_str}\n"
            f"  sub-agents: {session.subagent_calls}"
        )
        if not priced and session.session_tokens:
            console.print(
                f"[dim]  No built-in price for '{p.model}'. Add it to config.yaml:\n"
                f"    pricing:\n      \"{p.model}\": {{ input: 1.25, output: 10 }}   # USD per 1M tokens[/dim]"
            )
        if session.tool_calls:
            console.print("  tools: " + ", ".join(f"{k} x{v}" for k, v in session.tool_calls.most_common()))
    elif cmd == "/skills":
        if session.skills:
            for name, s in session.skills.items():
                console.print(f"  [cyan]{name}[/cyan] — {s['description']}")
        else:
            console.print("[dim]no skills found (add .euron/skills/<name>/SKILL.md)[/dim]")
    elif cmd == "/search":
        from . import sessions as _sessions

        if not rest:
            console.print("[red]usage: /search <text>[/red]")
        else:
            hits = _sessions.search(rest, session.workspace)
            if hits:
                for h in hits:
                    console.print(f"  [cyan]{h['id']}[/cyan] {h['title']}  [dim]…{h['snippet']}…[/dim]")
            else:
                console.print("[dim]no matches in past sessions[/dim]")
    elif cmd == "/effort":
        level = (rest or "medium").strip().lower()
        if level not in ("low", "medium", "high"):
            console.print("[red]usage: /effort low|medium|high[/red]")
        else:
            session.config.agent.reasoning_effort = level
            session.config.agent.thinking = level == "high"
            from .llm import build_client

            session.client = build_client(session.config.provider, session.config.agent)
            console.print(f"[green]effort → {level}[/green]")
    elif cmd == "/review":
        return ("run:Review the current uncommitted git changes (call git_diff first) "
                "for bugs, security issues, race conditions, and improvements. Give a "
                "concise, prioritized findings list. Do NOT modify files unless asked.")
    else:
        return "unknown"
    return "handled"


async def _chat(args) -> None:
    cfg = resolve_config(args)
    workspace = str(Path(args.workspace).resolve())
    io = TerminalIO(auto_approve=getattr(args, "yes", False))
    sid = getattr(args, "session", None)
    team = getattr(args, "team_name", None)
    dangerous = getattr(args, "dangerous", False)
    persist = bool(getattr(args, "resume", False) or sid)
    session = AgentSession(workspace, cfg, io, persist=persist, session_id=sid,
                           team=team, dangerous=dangerous)
    if dangerous:
        io.auto_approve = True
    if team:
        console.print(f"[magenta]team mode:[/magenta] coordinating '{team}' (state persists)")
    if dangerous:
        console.print("[bold red]DANGEROUS MODE ON[/bold red] - the agent will run everything without asking.")
    console.print(
        Panel(
            f"Euron Agent · [bold]{cfg.provider.name}[/bold] / {cfg.provider.model}\n"
            f"workspace: {workspace}\n"
            "Type a task, or /help for commands.",
            border_style="cyan",
        )
    )
    if _key_missing(cfg):
        console.print(
            f"[yellow]No API key for '{cfg.provider.name}'.[/yellow] "
            "Set one with [bold]/key[/bold] (or switch with [bold]/provider[/bold])."
        )

    ptk = _build_prompt_session(workspace)  # '/' autocomplete popup (if available)
    if ptk is None:
        console.print("[dim](tip: pip install prompt_toolkit for '/' command autocomplete)[/dim]")

    while True:
        try:
            if ptk is not None:
                msg = await ptk.prompt_async("you ❯ ")
            else:
                msg = await asyncio.to_thread(Prompt.ask, "[bold cyan]you[/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            break
        msg = msg.strip()
        if not msg:
            continue
        if msg.startswith("/"):
            result = await _handle_command(msg, session, args, io)
            if result == "exit":
                break
            if result and result.startswith("run:"):
                msg = result[4:]  # a command that expands into a task
            elif result == "unknown":
                # maybe a custom command from .euron/commands/<name>.md
                from .commands import expand_command, load_commands

                name, _, rest = msg[1:].partition(" ")
                custom = load_commands(workspace)
                if name in custom:
                    msg = expand_command(custom[name], rest.strip())
                else:
                    console.print(f"[red]unknown command[/red] /{name}  (/help)")
                    continue
            else:
                continue
        if _key_missing(session.config):
            console.print(
                "[yellow]No API key set — use /key first (or /provider to switch).[/yellow]"
            )
            continue
        io.begin_run()
        task = asyncio.ensure_future(session.run(msg))
        try:
            await task
        except KeyboardInterrupt:
            session.cancel()
            console.print("\n[yellow]stopping…[/yellow]")
            try:
                await task
            except Exception:
                pass
        finally:
            io._finish()  # safety: ensure the live footer is stopped
    console.print("[dim]bye[/dim]")


def cmd_chat(args) -> None:
    asyncio.run(_chat(args))


def cmd_serve(args) -> None:
    from .server import serve

    console.print(f"[cyan]Euron Agent server[/cyan] on http://{args.host}:{args.port}")
    if args.host not in ("127.0.0.1", "localhost") and args.no_auth:
        console.print("[red]warning:[/red] serving on a public host with auth disabled!")
    serve(
        host=args.host,
        port=args.port,
        reload=args.reload,
        token=args.token,
        auth=not args.no_auth,
    )


def cmd_providers(args) -> None:
    _print_providers()


def cmd_update(args) -> None:
    import subprocess

    pkg = "euron-coding-agent"
    console.print(f"[cyan]Updating {pkg} to the latest version...[/cyan]")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir", pkg]
    )
    if r.returncode == 0:
        from . import __version__

        console.print(f"[green]Done.[/green] Restart euron-agent to use the new version "
                      f"(was {__version__}).")
    else:
        console.print("[red]Update failed.[/red] Close any running euron-agent and retry, "
                      "or run: pip install -U euron-coding-agent")


def cmd_plugin(args) -> None:
    from . import plugins

    if args.action == "list":
        rows = plugins.list_plugins()
        if rows:
            for p in rows:
                console.print(f"  [cyan]{p['name']}[/cyan]  {p['description']}")
        else:
            console.print("[dim]no plugins installed[/dim]")
    elif args.action == "add":
        if not args.source:
            console.print("[red]usage: euron-agent plugin add <dir|zip-url>[/red]")
            return
        name = plugins.install(args.source)
        console.print(f"[green]installed plugin '{name}'[/green]")
    elif args.action == "remove":
        ok = plugins.remove(args.source or "")
        console.print(f"[green]removed[/green]" if ok else "[yellow]not found[/yellow]")


def cmd_sessions(args) -> None:
    from rich.table import Table

    from . import sessions

    ws = None if getattr(args, "all", False) else str(Path(args.workspace).resolve())
    rows = sessions.list_sessions(ws)
    if not rows:
        console.print("[dim]no saved sessions[/dim]")
        return
    table = Table(title="Sessions")
    table.add_column("id")
    table.add_column("title")
    table.add_column("workspace")
    for r in rows[:40]:
        table.add_row(r["id"], (r.get("title") or "")[:50], (r.get("workspace") or "")[-40:])
    console.print(table)
    console.print("[dim]resume with: euron-agent chat --session <id>[/dim]")


# Embedded templates so `init` works even from a pip install (the example files
# are not shipped inside the wheel).
_CONFIG_TEMPLATE = """# Euron Agent config. `active` picks a provider profile below.
# Every profile is OpenAI-compatible unless type: anthropic. That covers
# Euron/Euri, OpenAI, OpenRouter, Ollama, vLLM, LM Studio, and more.
active: openai

providers:
  euri:
    type: openai
    base_url: https://api.euron.one/api/v1
    api_key_env: EURI_API_KEY
    model: gpt-4.1-mini
  openai:
    type: openai
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
    model: gpt-4o-mini
  ollama:
    type: openai
    base_url: http://localhost:11434/v1
    api_key_env: null
    model: qwen2.5-coder:7b
  anthropic:
    type: anthropic
    api_key_env: ANTHROPIC_API_KEY
    model: claude-sonnet-4-6

agent:
  max_steps: 30
  auto_approve_writes: false
  auto_approve_commands: false
"""

_ENV_TEMPLATE = """# Only set the key(s) for the provider(s) you use.
EURI_API_KEY=
OPENAI_API_KEY=
OPENROUTER_API_KEY=
ANTHROPIC_API_KEY=
"""


def cmd_init(args) -> None:
    for name, content in (("config.yaml", _CONFIG_TEMPLATE), (".env", _ENV_TEMPLATE)):
        dst = Path.cwd() / name
        if dst.exists():
            console.print(f"[yellow]skip[/yellow] {name} already exists")
        else:
            dst.write_text(content, encoding="utf-8")
            console.print(f"[green]created[/green] {name}")
    console.print(
        "Tip: you can also just run [bold]euron-agent[/bold] and use /provider and "
        "/key — no files needed."
    )


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def cmd_version(args) -> None:
    from . import __version__

    console.print(f"euron-agent {__version__}  (euron-coding-agent)")


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    p = argparse.ArgumentParser(prog="euron-agent", description="Euron coding agent.")
    p.add_argument(
        "--version", "-V", action="version",
        version=f"euron-agent {__version__} (euron-coding-agent)",
    )
    p.add_argument("--config", help="Path to config.yaml")
    p.add_argument("--provider", help="Override active provider profile")
    p.add_argument("--model", help="Override model id")
    p.add_argument("--workspace", default=os.getcwd(), help="Workspace root (default: cwd)")
    p.add_argument("--team-name", dest="team_name", help="Run as a coordinator for this named team")
    p.add_argument("--dangerous", action="store_true",
                   help="DANGEROUS: auto-run everything, never ask for approval (YOLO mode)")
    sub = p.add_subparsers(dest="command")
    sub.required = False  # bare `euron-agent` -> chat

    r = sub.add_parser("run", help="Run a single task and exit")
    r.add_argument("task")
    r.add_argument("--yes", "-y", action="store_true", help="Auto-approve all actions")
    r.add_argument("--json", action="store_true", help="Headless: stream events as JSON (auto-approve)")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("chat", help="Interactive REPL")
    c.add_argument("--yes", "-y", action="store_true", help="Auto-approve all actions")
    c.add_argument("--resume", action="store_true", help="Resume the latest session for this workspace")
    c.add_argument("--session", help="Resume a specific session id (see: euron-agent sessions)")
    c.set_defaults(func=cmd_chat)

    s = sub.add_parser("serve", help="Start the FastAPI server")
    s.add_argument("--host", default="127.0.0.1", help="Bind host (0.0.0.0 for remote/cloud)")
    s.add_argument("--port", type=int, default=8000, help="Port (0 = auto-pick a free port)")
    s.add_argument("--token", help="Bearer token clients must send (else one is generated)")
    s.add_argument("--no-auth", action="store_true", help="Disable token auth (local only!)")
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    sub.add_parser("providers", help="List configured providers").set_defaults(func=cmd_providers)
    sub.add_parser("init", help="Scaffold config.yaml and .env").set_defaults(func=cmd_init)
    sub.add_parser("update", help="Update euron-coding-agent to the latest version").set_defaults(func=cmd_update)
    sub.add_parser("version", help="Show the installed version").set_defaults(func=cmd_version)

    pl = sub.add_parser("plugin", help="Manage plugins (skills/commands/MCP bundles)")
    pl.add_argument("action", choices=["add", "list", "remove"])
    pl.add_argument("source", nargs="?", help="Plugin dir / .zip URL (add) or name (remove)")
    pl.set_defaults(func=cmd_plugin)

    se = sub.add_parser("sessions", help="List saved sessions (dashboard)")
    se.add_argument("--all", action="store_true", help="All workspaces, not just this one")
    se.set_defaults(func=cmd_sessions)

    sub.add_parser("team", help="List multi-agent teams").set_defaults(func=cmd_team)

    sch = sub.add_parser("schedule", help="Scheduled agents (cron)")
    sch.add_argument("action", choices=["create", "list", "remove", "run", "daemon"])
    sch.add_argument("name", nargs="?", help="Schedule name (create) or id (remove/run)")
    sch.add_argument("--cron", help='Cron expression, e.g. "0 9 * * MON-FRI"')
    sch.add_argument("--prompt", help="Task prompt to run")
    sch.set_defaults(func=cmd_schedule)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        args.func = cmd_chat
        args.yes = False
    try:
        args.func(args)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/dim]")
        return 130
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]fatal:[/red] {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
