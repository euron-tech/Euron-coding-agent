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
import os
import sys
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


# --------------------------------------------------------------------------- #
# Terminal IO
# --------------------------------------------------------------------------- #
class TerminalIO(AgentIO):
    def __init__(self, auto_approve: bool):
        self.auto_approve = auto_approve
        self._dirty = False  # unfinished streamed line on screen

    def _newline_if_dirty(self) -> None:
        if self._dirty:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._dirty = False

    def emit_sync(self, event: dict) -> None:
        if event.get("type") in ("token", "command_output"):
            sys.stdout.write(event["text"])
            sys.stdout.flush()
            self._dirty = True

    async def emit(self, event: dict) -> None:
        t = event["type"]
        if t == "status":
            return
        if t == "assistant_message":
            self._newline_if_dirty()
            return
        self._newline_if_dirty()
        if t == "tool_start":
            args = event["args"]
            detail = args.get("path") or args.get("command") or args.get("query") or ""
            console.print(f"[cyan]⚙ {event['name']}[/cyan] [dim]{detail}[/dim]")
        elif t == "diff":
            self._print_diff(event["patch"])
        elif t == "tool_result":
            mark = "[green]✓[/green]" if event["ok"] else "[red]✗[/red]"
            out = (event["output"] or "").strip()
            if out:
                snippet = out if len(out) < 1200 else out[:1200] + " …"
                console.print(f"{mark} [dim]{snippet}[/dim]")
        elif t == "error":
            console.print(f"[red]error:[/red] {event['message']}")
        elif t == "usage":
            console.print(
                f"[dim]· {event['prompt_tokens']}+{event['completion_tokens']} tok "
                f"(session {event['session_tokens']})[/dim]"
            )
        elif t == "info":
            console.print(f"[dim]ℹ {event['message']}[/dim]")
        elif t == "thinking":
            console.print(f"[dim]💭 {event['text']}[/dim]")
        elif t == "plan":
            console.print(Panel(event["text"], title="Proposed plan", border_style="magenta"))
        elif t == "todos":
            marks = {"completed": "[green]✔[/green]", "in_progress": "[yellow]▸[/yellow]", "pending": "[dim]○[/dim]"}
            console.print("[bold]Tasks:[/bold]")
            for item in event["items"]:
                console.print(f"  {marks.get(item.get('status'), '○')} {item.get('content', '')}")
        elif t == "subagent_start":
            console.print(f"[cyan]↳ sub-agent:[/cyan] {event['description']}")
        elif t == "subagent_end":
            console.print(f"[cyan]↳ sub-agent done[/cyan] [dim]{event['summary']}[/dim]")
        elif t == "cancelled":
            console.print("[yellow]■ cancelled[/yellow]")
        elif t == "done":
            console.print("[dim]— done —[/dim]")

    def _print_diff(self, patch: str) -> None:
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                console.print(f"[green]{line}[/green]")
            elif line.startswith("-") and not line.startswith("---"):
                console.print(f"[red]{line}[/red]")
            elif line.startswith("@@"):
                console.print(f"[magenta]{line}[/magenta]")
            else:
                console.print(f"[dim]{line}[/dim]")

    async def request_approval(self, request: dict) -> ApprovalDecision:
        self._newline_if_dirty()
        preview = request.get("preview") or ""
        title = f"Approve {request['name']}?"
        if preview:
            if "\n" in preview and ("+++" in preview or "@@" in preview):
                self._print_diff(preview)
            else:
                console.print(Panel(preview, title=title, border_style="yellow"))
        if self.auto_approve:
            console.print("[green]auto-approved[/green]")
            return ApprovalDecision(approved=True)
        answer = await asyncio.to_thread(
            Prompt.ask,
            f"[yellow]{title}[/yellow] (y/n, or type feedback to reject)",
            default="y",
        )
        a = answer.strip().lower()
        if a in ("y", "yes", ""):
            return ApprovalDecision(approved=True)
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
    io = TerminalIO(auto_approve=args.yes)
    await AgentSession(workspace, cfg, io).run(task)


def cmd_run(args) -> None:
    asyncio.run(_run_task(args.task, args))


HELP = """[bold]commands[/bold]
  /provider [name]   switch provider (interactive if no name)
  /key [value]       set API key for the current provider (hidden prompt if blank)
  /model [name]      set the model for the current provider
  /baseurl [url]     set a custom base URL (self-hosted / custom endpoints)
  /config            show current provider, model, base URL, key status
  /providers         list known providers
  /plan              plan mode for the next task (research → approve → execute)
  /compact           summarize the conversation to free up context
  /undo              revert the file changes from the last task
  /reset             clear the conversation context
  /yes               toggle auto-approve for edits & commands
  /help              show this help (Ctrl+C during a task = stop)
  /exit              quit"""


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
    else:
        console.print(f"[red]unknown command[/red] {cmd}  (/help)")
    return "handled"


async def _chat(args) -> None:
    cfg = resolve_config(args)
    workspace = str(Path(args.workspace).resolve())
    io = TerminalIO(auto_approve=getattr(args, "yes", False))
    session = AgentSession(workspace, cfg, io, persist=getattr(args, "resume", False))
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

    while True:
        try:
            msg = await asyncio.to_thread(Prompt.ask, "[bold cyan]you[/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            break
        msg = msg.strip()
        if not msg:
            continue
        if msg.startswith("/"):
            if await _handle_command(msg, session, args, io) == "exit":
                break
            continue
        if _key_missing(session.config):
            console.print(
                "[yellow]No API key set — use /key first (or /provider to switch).[/yellow]"
            )
            continue
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
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="euron-agent", description="Euron coding agent.")
    p.add_argument("--config", help="Path to config.yaml")
    p.add_argument("--provider", help="Override active provider profile")
    p.add_argument("--model", help="Override model id")
    p.add_argument("--workspace", default=os.getcwd(), help="Workspace root (default: cwd)")
    sub = p.add_subparsers(dest="command")
    sub.required = False  # bare `euron-agent` -> chat

    r = sub.add_parser("run", help="Run a single task and exit")
    r.add_argument("task")
    r.add_argument("--yes", "-y", action="store_true", help="Auto-approve all actions")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("chat", help="Interactive REPL")
    c.add_argument("--yes", "-y", action="store_true", help="Auto-approve all actions")
    c.add_argument("--resume", action="store_true", help="Resume this workspace's saved history")
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
