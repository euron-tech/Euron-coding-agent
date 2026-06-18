# Changelog

## 1.0.7

- Live status line (CLI): spinner, rotating verb, elapsed time, live token count
  and cost, and tool / sub-agent counters with the current activity - while
  streaming tokens.
- Completion summary after every task: duration, tokens, cost, and the steps
  taken (every tool and sub-agent, in order).
- Memory & context optimization: large tool outputs are bounded (head+tail) in
  history; automatic compaction once over `agent.max_context_tokens`; live token
  accounting.

## 0.6.0

The final buildable batch.

- **Plugins** — install bundles of skills + commands + MCP servers from a folder
  or `.zip` URL: `euron-agent plugin add <dir|url>` / `list` / `remove`.
- **Named sessions** — resume + dashboard + transcript search:
  `euron-agent sessions`, `chat --session <id>` / `--resume`, `/search <text>`.
- **CI / PR helpers** — `git_branch`, `git_push`, `open_pr` (via the `gh` CLI).
- **Auto-diagnostics** — after a task, offer to fix new error diagnostics in the
  files the agent changed (`euronAgent.autoDiagnostics`).
- **Reveal changed files** — the extension opens files as the agent edits them.

## 0.5.0

Claude + Cursor power-ups.

- **Skills** — packaged capabilities in `.euron/skills/<name>/SKILL.md`, loaded on
  demand via `use_skill` (Claude-Code-style progressive disclosure).
- **Model fallback chains** — `fallback_models` tries other models if the primary
  keeps failing.
- **Git worktree isolation** — `worktree_add/list/remove` (Cursor "shadow workspace").
- **`/review`** — review the current git changes for bugs (Claude `/code-review` /
  Cursor BugBot, local); **Review Changes** command in the extension.
- **`/usage`** — tokens, cost, and per-tool/sub-agent breakdown.
- **`/effort low|medium|high`** — reasoning effort preset (+ `euronAgent.effort`).
- **`/skills`** — list available skills.

## 0.4.0

Phases 3–4 — config, control, and a richer UI (model-agnostic).

- **Permissions engine** — allow / ask / deny rules by tool + glob, with an
  **Always** button that persists an allow rule.
- **Hooks** — run shell commands on PreToolUse/PostToolUse/Stop/UserPromptSubmit
  (a non-zero PreToolUse exit blocks the tool).
- **Project memory** — auto-loads `AGENTS.md`/`EURON.md` into context;
  "Create Project Memory" command / CLI `/init`.
- **Custom slash commands** — `.euron/commands/*.md` with `$ARGUMENTS`.
- **Cost tracking** — live $ estimate alongside tokens.
- **Multimodal** — attach images () to vision-capable models.
- **Extended thinking** — provider-native where available (Anthropic thinking,
  OpenAI reasoning effort), guarded so non-reasoning models are unaffected.
- **Richer panel** — markdown rendering, "Fix Diagnostics" command, Set Model.

## 0.3.0

Claude-Code-style capabilities (Phases 1–2), staying model-agnostic.

- **Plan mode** — research read-only, propose a plan, approve, then execute
  (toolbar toggle / CLI `/plan`).
- **Sub-agents** — `spawn_agent` delegates focused sub-tasks (optional cheaper
  `subagent_model`); activity streamed to the panel.
- **TODO checklist** — live task list rendered in the panel and CLI.
- **MCP** — connect Model Context Protocol servers; their tools appear as
  `mcp__server__tool` (install `euron-coding-agent[mcp]`).
- **Web tools** — `web_search` (DuckDuckGo keyless, or Tavily/Brave/SerpAPI) and
  `web_fetch`.
- **More tools** — `glob`, `multi_edit` (atomic), background processes
  (`bash_background`/`process_*`), and `git_status`/`git_diff`/`git_commit`.
- **`/compact`** — LLM-summarize the conversation to free context.

## 0.2.0

Big feature release — the whole "not yet implemented" roadmap, plus a cloud posture.

- **Stop** button to cancel a running task; **Undo** to revert a task's file changes.
- **Persistent history** per workspace across reloads (`euronAgent.persistHistory`).
- **Context management**: token-usage display, automatic compaction when the
  conversation exceeds the model's window.
- **@file mentions**: type `@path/to/file` in a task to inline its contents.
- **Streaming command output** — `run_command` output appears live.
- **Auto-approve toggle** in the toolbar (no more approving every step).
- **Multi-root workspaces**: pick which folder the agent operates on.
- **Cloud/self-host ready backend**: bearer-token auth, bind to any host
  (`serve --host 0.0.0.0 --token ...`), set `euronAgent.token` for a remote backend.
- LLM **retry/backoff**, `.gitignore`-aware ignores, binary-file guard.
- A real **pytest** suite in the repo.

## 0.1.1

- CLI: bare `euron-agent` now opens an interactive chat (Claude-CLI style).
- CLI: configure everything in-session — `/provider`, `/key`, `/model`,
  `/baseurl`, `/config` — persisted to `~/.euron-agent/config.json`.
- Fix: `euron-agent init` works from a pip install (templates are now embedded).

## 0.1.0

Initial release.

- Agentic chat panel in the VS Code sidebar (plan → read → edit → run).
- Streaming responses with per-action **approval** and inline **diffs**.
- Provider-agnostic: Euron/Euri, OpenAI, OpenRouter, Anthropic, Ollama, or any
  OpenAI-compatible / self-hosted endpoint.
- **Zero manual setup**: the extension auto-installs and manages its Python
  backend in a private environment.
- API keys stored securely in VS Code SecretStorage.
