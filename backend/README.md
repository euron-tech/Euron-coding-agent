# euron-coding-agent

The Python agent backend and standalone CLI behind the Euron Coding Agent VS Code
extension. An open-source, model-agnostic agentic coding assistant that plans,
edits code, runs commands, searches the web, reviews diffs, coordinates teams of
sub-agents, and runs on cron schedules - with your approval at every step.

Provider-agnostic: works with Anthropic, OpenAI, Google Gemini, OpenRouter, Groq,
Cerebras, DeepSeek, Together, Mistral, xAI, Vercel AI Gateway, Euron/Euri,
Ollama, LM Studio, or any OpenAI-compatible / self-hosted endpoint.

## Install

    pip install euron-coding-agent
    euron-agent update                            # update to the latest anytime
    # optional extras:
    pip install "euron-coding-agent[anthropic]"   # native Anthropic client
    pip install "euron-coding-agent[mcp]"         # Model Context Protocol servers

Tip: install into a virtual environment for isolation. The client auto-adapts to
each provider's API (e.g. models needing max_completion_tokens instead of
max_tokens, or rejecting a custom temperature) - no model-specific config needed.

## Quick start (CLI)

    euron-agent                                    # interactive chat in the current folder
    euron-agent run "add a /health route to app.py"
    euron-agent run "summarize the repo" --json     # headless JSON output for CI/scripts
    euron-agent serve --host 0.0.0.0 --port 8000    # self-host with bearer-token auth

Set a provider and key right inside the chat (no files needed):

    you: /provider openai      # or gemini, anthropic, groq, ollama, ...
    you: /key                  # paste your API key (stored in ~/.euron-agent)
    you: create a FastAPI hello-world app with a test

Or use environment variables (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY,
EURI_API_KEY, ...) or a config.yaml. Built-in provider profiles mean no config
file is required - just pick a provider and supply a key.

## CLI commands

    euron-agent                     interactive chat (remembers provider/key)
    euron-agent run "<task>"        one-shot task ( --yes to auto-approve, --json headless )
    euron-agent providers           list configured providers
    euron-agent models              show the multi-model routing table (per-phase model + price)
    euron-agent init                scaffold config.yaml + .env
    euron-agent serve               run the API / WebSocket server ( --port 0 = auto )
    euron-agent --team-name <name> "<task>"   multi-agent team (coordinator + specialists)
    euron-agent team                list multi-agent teams
    euron-agent schedule create "<name>" --cron "0 9 * * MON-FRI" --prompt "<task>"
    euron-agent schedule list | run <id> | remove <id> | daemon
    euron-agent plugin add <dir|zip-url> | list | remove <name>
    euron-agent security            run a security audit of the codebase
    euron-agent scan                fast secret + dependency vulnerability scan
    euron-agent secfix              autonomous security remediation (audit→fix→verify)
    euron-agent test [--all]        write + run tests (--all = whole-project suite)
    euron-agent ship "<what>"       plan→build→test→scan→deploy→monitor, end to end
    euron-agent deploy [--check]    deploy to a live URL (or list target readiness)
    euron-agent monitor             monitoring status (errors/uptime/providers)
    euron-agent heal [<url>]        self-heal: health-check, auto-rollback, error→PR
    euron-agent doctor              environment self-check
    euron-agent audit [--lines N]   show & verify the tamper-evident action log
    euron-agent init-ci             scaffold a GitHub Actions workflow
    euron-agent sessions            list saved sessions (dashboard)
    euron-agent chat --session <id> | --resume   resume a past session

In-chat commands: /provider /key /model /effort /plan /execute /review /security
/scan /secfix /test /testall /audit /doctor /compact /init /skills /search /usage
/undo /reset /yes /help /exit, plus any custom command in .euron/commands/.

## What's new (1.4.0)

- **The full loop — one sentence to a live, monitored, self-healing app.**
  `euron-agent ship "build a FastAPI todo API with a Neon DB and put it live"` runs
  plan → build → test → security-scan → **deploy** → **monitor**.
- **Auto-deploy** headless to Cloud Run, Cloudflare/Pages, Vercel, Netlify, Render,
  Fly, Railway, Docker, Kubernetes/Helm, AWS (SAM/App Runner), Azure Container Apps,
  plus Neon/Supabase databases — each via its CLI + a credential env var.
- **Spend gate:** free-tier targets deploy directly; billable ones are blocked unless
  you set `deploy.allow_billable`. Secrets are read from env, never printed, and every
  deploy/rollback is recorded in the audit log.
- **Monitoring** (`euron-agent monitor`): wires Sentry / OpenTelemetry / uptime and
  reports current errors + uptime.
- **Self-healing** (`euron-agent heal`): auto-rollback to the last known-good release
  on a failed health check; production errors become a fix + regression test in a PR
  (never auto-merged). See docs/DEPLOY.md.

## What's new (1.3.0)

- **Multi-model routing**: assign a different model (from any provider) to each
  phase — `planner`, `executor`, `cheap` (sub-agents), `verifier` — via `models:` +
  `routing:` in config.yaml. Cost-aware `strategy: auto` runs cheap models where safe
  and **auto-escalates** to a stronger model when the cheaper one keeps failing, so
  you spend the least that still gets the job done. No provider lock-in.
- `euron-agent models` / `/models`: see which model + price runs each phase.
- Cost is now tracked per model actually used. See docs/MULTI_MODEL.md.

## What's new (1.2.0)

- **repo_map** tool: a compact symbol/outline map of the codebase so the agent
  reads outlines first and full file bodies only on demand (token-saving).
- **secret_scan** + **dependency_audit** tools, and **/scan** / **/secfix**
  commands — find hard-coded credentials and vulnerable deps, then remediate in an
  autonomous audit → fix → verify loop.
- **Tamper-evident audit log** (`.euron/audit/`) — SHA-256 hash-chained record of
  every action; `euron-agent audit` / `/audit` verifies the chain.
- **Sandbox / egress policy** (`sandbox:`): deny-by-default command rules, allowlist,
  and `block_network`, enforced before every shell command (even in dangerous mode).
- **Agent-of-agent budgets** (`subagent_max_calls`, `subagent_token_budget`),
  **model routing** (`router: {cheap, heavy}`), an opt-in **verifier** sub-agent
  (`verify_edits`), and **self-heal** (`self_heal: N`).
- **euron-agent doctor** (environment self-check), **init-ci** (GitHub Actions),
  and **Anthropic prompt caching** of the static system prompt.

## What's new (1.1.0)

- Drag-and-drop context: reference or drop a **file, folder, or image** path into
  chat and it is read and used automatically. Files of any reasonable length
  (smart head+tail truncation), folders read recursively, and images sent as
  multimodal blocks so vision models can actually see them. No special syntax -
  quoted paths, absolute/relative paths, bare filenames, and `@mentions` all work.
- Built-in **security audit** (`/security`, `euron-agent security`): vulnerability
  review with severity-ranked findings and concrete fixes.
- **Autonomous testing**: `/test [target]` writes and runs tests for a file/module;
  `/testall` (or `euron-agent test --all`) builds and runs a comprehensive test
  suite for the whole project and reports coverage gaps.
- **Plan mode and execute mode**, switchable mid-session with `/plan` and
  `/execute` (execute is the default).
- Earlier: auto-onboarding (`.euron/` memory + skill + project doc), live status
  line + completion summary, dangerous (YOLO) mode, `/` command autocomplete,
  accurate per-model cost, and memory/context optimization.

## Features

- Auto-onboarding (.euron/ memory + skill + project doc, created automatically).
- Agentic tool-calling loop: plan, read, search, edit, run, verify.
- 28 sandboxed tools: read/write/edit/multi_edit/create/delete files, glob,
  search, run_command (streaming), background processes, git status/diff/commit/
  branch/push, open_pr, git worktrees, web_search, web_fetch.
- Plan mode and execute mode, sub-agents, and multi-agent teams with persistent state.
- Drag-and-drop context: read files (any length), whole folders, and images.
- Full loop: one sentence → build → test → scan → deploy to a live URL → monitor → self-heal.
- Auto-deploy to 13+ targets (Cloud Run/Cloudflare/Vercel/Netlify/Fly/Railway/Render/Docker/K8s/AWS/Azure + Neon/Supabase) with a free-tier-first spend gate.
- Multi-model routing: a different model per phase across providers, cost-aware with auto-escalation.
- Token-friendly code intelligence: `repo_map` outline tool, model routing, prompt caching.
- Security suite: `/security`, `/scan`, `/secfix`, secret_scan + dependency_audit tools.
- Tamper-evident audit log + sandbox/egress policy; sub-agent budgets, verifier, self-heal.
- Autonomous test writing/running (`/test`, `/testall`); `doctor` + `init-ci` helpers.
- Scheduled agents on cron schedules (independent of any terminal).
- MCP servers, plugins, skills, project memory (AGENTS.md), and custom commands.
- Permissions (allow/ask/deny), hooks, approvals with diffs, checkpoints, undo.
- Web search/fetch, multimodal image input, token and cost tracking, extended
  thinking, model fallback chains.
- Messaging notifications: Slack, Discord, Telegram, Google Chat, WhatsApp, Linear.
- Headless JSON mode and a self-hostable server with bearer-token auth.

## Server

- GET  /health      liveness.
- GET  /providers   configured providers.
- POST /agent/run   one-shot, non-interactive (set "auto_approve": true).
- WS   /ws          streaming agent with per-action approval (used by the extension).

The WebSocket init message accepts provider, model, api_key, and base_url so
secrets never need to live on disk. Set EURON_AGENT_TOKEN (or serve --token) to
require bearer auth.

See the project README at https://github.com/euron-tech/Euron-coding-agent for the
full architecture and the VS Code extension.

## License

Apache License 2.0. Copyright 2026 Euron Engage Sphere Technology Private Limited.
