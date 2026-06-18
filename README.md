<div align="center">

# Euron Coding Agent

**An open-source, model-agnostic agentic coding assistant - CLI + VS Code extension.**
A combination of the best ideas from Claude Code, Cursor, and Cline, built to run
anywhere, with any model, fully self-hostable.

[![PyPI](https://img.shields.io/pypi/v/euron-coding-agent.svg)](https://pypi.org/project/euron-coding-agent/)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://pypi.org/project/euron-coding-agent/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-88%20passing-brightgreen.svg)](backend/tests)

<!-- AUTOGEN:STATUS -->
**Latest: v1.4.0** · 37 tools · 15 providers · 88 tests passing
<!-- /AUTOGEN:STATUS -->

</div>

---

> **New in 1.4.0** - **The full loop: one sentence → built → tested → security-scanned
> → deployed to a live URL → monitored → self-healing.** `euron-agent ship "build a
> FastAPI todo API with a Neon DB and put it live"`. Deploy headless to **Cloud Run,
> Cloudflare, Vercel, Netlify, Fly, Railway, Render, Docker, K8s/Helm, AWS, Azure**
> (+ Neon/Supabase DBs) with a **free-tier-first spend gate**; wire **Sentry/OTel/uptime**
> monitoring; and **self-heal** — auto-rollback on a failed health check, error→PR for
> the rest. See [docs/DEPLOY.md](docs/DEPLOY.md). 1.3.0 added **multi-model routing**
> ([docs/MULTI_MODEL.md](docs/MULTI_MODEL.md)). See the [changelog](extension/CHANGELOG.md).

## What is it?

Euron Coding Agent is an autonomous coding agent with the brain in Python and a
thin front-end. It plans, reads your code, edits files, runs commands, searches
the web, reviews diffs, and coordinates teams of sub-agents - with your approval
at every step. It runs three ways:

- **CLI** - `pip install euron-coding-agent` then `euron-agent`
- **VS Code extension** (and any Open VSX editor: Cursor, VSCodium, Windsurf)
- **Self-hosted server** - `euron-agent serve --host 0.0.0.0` with bearer auth

It is not locked to any provider. Bring your own key for any model, or run fully
local with Ollama or LM Studio.

## Highlights

| Capability | Description |
|---|---|
| Agentic loop | plan, read, edit, run, verify, with native tool-calling |
| Any model | Anthropic, OpenAI, Gemini, OpenRouter, Groq, Cerebras, DeepSeek, Together, Mistral, xAI, Vercel AI Gateway, Ollama, LM Studio, or any OpenAI-compatible API |
| Multi-model routing | a different model per phase (plan/execute/sub-agent/verify) across providers, cost-aware with auto-escalation |
| Ship the full loop | one sentence → build → test → scan → **deploy to a live URL** → monitor → self-heal |
| Auto-deploy | headless to Cloud Run, Cloudflare, Vercel, Netlify, Fly, Railway, Render, Docker, K8s/Helm, AWS, Azure (+ Neon/Supabase), free-tier-first spend gate |
| Self-healing in prod | auto-rollback on failed health check; production errors → fix + regression test → PR |
| Multi-agent teams | a coordinator delegates subtasks to specialists; state persists |
| Scheduled agents | run tasks on cron (`schedule create --cron "0 9 * * MON-FRI"`) |
| MCP, plugins, skills | unlimited tool and capability extensibility |
| Rules, memory, commands | `AGENTS.md`, `.euron/skills`, `.euron/commands` |
| Plan + execute modes | think first with `/plan`, then `/execute` to make changes |
| Drag & drop context | drop a file, folder, or image path - it's read and used |
| Token-friendly | `repo_map` outline tool + Anthropic prompt caching + model routing |
| Security & testing | `/security`, `/scan`, `/secfix`, `/test`, `/testall`; secret + dep scanners |
| Tamper-evident audit | hash-chained log of every action (`/audit` to verify) |
| Sandbox / egress | deny-by-default command rules + network block, even in YOLO mode |
| Agent-of-agent | nested sub-agents with shared call/token budgets + verifier + self-heal |
| Approvals and permissions | allow/ask/deny rules, diffs, undo, cancel |
| Notifications | Slack, Discord, Telegram, Google Chat, WhatsApp, Linear |
| Multimodal | attach images to vision models |
| Web | `web_search` and `web_fetch` |
| Headless | `run --json` for CI/CD and scripting |
| Self-host / cloud | bind any host, bearer-token auth |

## Works with every model

Not locked to one provider. Built-in profiles (just pick one and add a key):

| Provider | Example models | Key env |
|---|---|---|
| Anthropic | Claude Opus / Sonnet / Haiku | `ANTHROPIC_API_KEY` |
| OpenAI | GPT series | `OPENAI_API_KEY` |
| Google Gemini | Gemini series (OpenAI-compatible endpoint) | `GEMINI_API_KEY` |
| OpenRouter | 200+ models from any provider | `OPENROUTER_API_KEY` |
| Groq / Cerebras | fast inference (Llama, etc.) | `GROQ_API_KEY` / `CEREBRAS_API_KEY` |
| DeepSeek / Together / Mistral / xAI | their hosted models | `*_API_KEY` |
| Vercel AI Gateway | models via the gateway | `AI_GATEWAY_API_KEY` |
| Euron / Euri | Euron-hosted models | `EURI_API_KEY` |
| Ollama / LM Studio | local models, no key | - |
| Custom | any OpenAI-compatible / self-hosted (incl. AWS Bedrock, Azure, GCP Vertex via a proxy) | configurable |

Capabilities (vision, tools, thinking) degrade gracefully when a model lacks them.

### Multi-model routing (one model per job, no lock-in)

Model-provider lock-in does not age well - the best/cheapest model rotates every
few months. So you can assign a **different model from any provider to each phase**
of the work and let the agent optimize cost without compromising quality:

```yaml
models:
  planner:  { provider: anthropic, model: claude-opus-4-8 }      # strong reasoning
  executor: { provider: openai,    model: gpt-5.5 }              # balanced main model
  cheap:    { provider: groq,      model: llama-3.3-70b-versatile }  # fast & cheap
  verifier: { provider: gemini,    model: gemini-2.5-flash }
routing:
  strategy: auto        # cheap where safe + auto-escalate when the cheap model struggles
  plan: planner
  execute: executor
  subagent: cheap
  verify: verifier
  escalate: planner     # jump to this strong model on repeated failures
```

- **Cheap where safe** (sub-agents, verifier), **your model for execution**, a
  **strong model for planning**, and **automatic escalation** to a stronger model
  when the cheaper one keeps failing - so you pay for the expensive model only on the
  hard steps. Cost is tracked per model actually used.
- Inspect the active routing with `euron-agent models` or `/models`.
- Full guide: **[docs/MULTI_MODEL.md](docs/MULTI_MODEL.md)**.

---

## Installation

### CLI (pip)

```bash
pip install euron-coding-agent          # add "[anthropic]" for native Claude, "[mcp]" for MCP
euron-agent                             # interactive chat in the current folder
euron-agent update                      # update to the latest version anytime
```

Recommended (isolated, avoids dependency/locking issues, especially on Windows):

```bash
python -m venv euron-env
# Windows: euron-env\Scripts\activate    |  macOS/Linux: source euron-env/bin/activate
pip install euron-coding-agent
euron-agent
```

You never need to configure model-specific quirks - the client auto-adapts to
each provider's API (for example, models that require `max_completion_tokens`
instead of `max_tokens`, or that reject a custom temperature, are handled
automatically).

Set a provider and key right inside the chat - no files needed:

```
you: /provider openai      # or gemini, anthropic, groq, ollama, ...
you: /key                  # paste your API key (stored in ~/.euron-agent)
you: create a FastAPI hello-world app with a test
```

### VS Code extension

Install "Euron Coding Agent" from the Marketplace (or Open VSX for
Cursor/VSCodium/Windsurf). On first run it auto-installs its Python backend
(needs Python 3.9+). Click the robot icon, pick a provider (server icon), paste a
key (key icon), and start chatting. Diffs, approvals, plan mode, and image attach
are all in the panel.

Requires Python 3.9+ on your machine for the managed backend.

---

## Quick start (CLI)

```bash
euron-agent                                   # chat (remembers provider/key)
euron-agent run "add a /health route" -y      # one-shot, auto-approve
euron-agent run "summarize the repo" --json    # headless JSON for CI/scripts
euron-agent --team-name auth-sprint "Plan and implement auth with tests"
euron-agent schedule create "PR summary" --cron "0 9 * * MON-FRI" \
    --prompt "List open PRs and their review status" --workspace .
euron-agent schedule daemon                   # fire due schedules
euron-agent serve --host 0.0.0.0 --port 8000  # self-host (prints a bearer token)
```

In-chat slash commands: `/provider /key /model /models /effort /plan /execute /review
/security /scan /secfix /test /testall /ship /deploy /deploys /monitor /heal /audit
/doctor /compact /init /skills /search /usage /undo /reset /yes /help /exit` - plus any
custom command you drop in `.euron/commands/`.

**Full command reference (every CLI subcommand and slash command, with
descriptions): [docs/COMMANDS.md](docs/COMMANDS.md).**

---

## Features in depth

### Auto-onboarding (zero setup)
The first time you run the agent in a project, it scaffolds a `.euron/` wrapper
automatically - no commands, nothing to remember:
- `.euron/AGENTS.md` - project memory/rules, pre-filled with the detected stack
  and build/test/lint commands (auto-loaded into context every run).
- `.euron/PROJECT.md` - a project overview the agent maintains as it learns.
- `.euron/skills/explore-codebase/SKILL.md` - a starter skill for understanding
  and safely changing the repo.

It detects Python, Node, Go, Rust, Java/Gradle, Ruby, PHP, and more. Re-run any
time with `euron-agent onboard` (CLI), `/onboard` (chat), or "Onboard Project"
(extension). Disable with `--no-onboard` or `agent.auto_onboard: false`.

### Agentic loop and tools
Native tool-calling loop. 28 tools: `read_file`, `write_file`, `edit_file`,
`multi_edit`, `create_file`, `delete_file`, `glob`, `search_text`, `run_command`
(streaming output), background processes (`bash_background`/`process_*`),
`git_status`/`git_diff`/`git_commit`/`git_branch`/`git_push`/`open_pr`, git
worktrees (`worktree_add/list/remove`), `web_search`, `web_fetch`, plus meta tools
`todo_write`, `spawn_agent`, `update_plan`, `use_skill`. All workspace-sandboxed.

### Plan mode and execute mode
Two ways to run, switchable mid-session:
- **Execute mode** (default) - the agent reads, edits, runs commands, and verifies
  to get the task done, asking for approval per your permission rules.
- **Plan mode** - read-only research; it proposes a step-by-step plan and waits for
  your approval before changing anything. Toggle with `/plan`, return with
  `/execute` (or the extension toggle).

### Drag & drop context - files, folders, and images
Reference or drag-and-drop a path into chat and the agent reads it automatically -
no special syntax needed:
- **Files** of any reasonable length (long files are smartly truncated head+tail).
- **Folders** - read recursively (bounded), so "summarize `./src`" just works.
- **Images** (`.png/.jpg/.gif/.webp/...`) - sent as multimodal blocks to vision
  models so the agent can actually *see* the screenshot/diagram you dropped.

Quoted paths (drag-drop with spaces), absolute or workspace-relative paths, bare
filenames, and `@mentions` are all detected. You can still attach images explicitly
in the extension.

### Security audit & autonomous testing
Built-in commands that turn the agent into a security reviewer and test engineer:
- `/security` - audit the code for vulnerabilities (injection, authz, secrets,
  unsafe deserialization, SSRF, path traversal, dependency risks) with severity-
  ranked findings and concrete fixes.
- `/scan` - fast pass: `secret_scan` (hard-coded credentials, masked in output) +
  `dependency_audit` (pip-audit / npm audit / cargo audit / govulncheck).
- `/secfix` - **autonomous remediation loop**: audit → plan → fix (highest severity
  first, with approval) → re-scan and re-test to verify.
- `/test [target]` - write meaningful tests for the given file/module and run them.
- `/testall` - build and run a **complete test suite** across the project, then
  report coverage gaps. Also `euron-agent security|scan|secfix|test [--all]` for
  headless/CI use.

### Ship the full loop: prompt → deployed → monitored → self-healing
Most agents stop at a pull request. This one closes the whole loop from one sentence,
on infra **you** control, with safety gates - the open white space in the market (no
CLI agent ships first-class deploy + production monitoring).

```bash
euron-agent ship "build a FastAPI todo API with a Neon database and put it live"
euron-agent deploy --check        # which targets are ready (CLI + token)?
euron-agent monitor               # errors / uptime / providers
euron-agent heal https://my-app.run.app   # health-check → auto-rollback → error→PR
```

- **`ship`** runs: plan → build → **test** → **security-scan** (`secret_scan` +
  `dependency_audit`, block on High) → **deploy** → wire **monitoring** → report the URL.
- **Deploy targets** (headless, credential-by-env): **Cloud Run, Cloudflare/Pages,
  Vercel, Netlify, Render, Fly, Railway, Docker, Kubernetes/Helm, AWS (SAM/App Runner),
  Azure Container Apps**, plus **Neon / Supabase** databases.
- **Spend gate:** free-tier targets deploy directly; **billable targets are blocked
  unless you opt in** (`deploy.allow_billable`). Secrets are read from env and never
  printed; every deploy/rollback is in the audit log.
- **Self-healing** (policy: rollback auto, fixes as PRs): a failed health check
  triggers an **automatic rollback** to the last known-good release; production errors
  become a **fix + regression test in a PR** for you to merge - never auto-deployed.
- Run `heal` on a `schedule` cron for continuous monitor-and-remediate.
- Full guide: **[docs/DEPLOY.md](docs/DEPLOY.md)**.

### Token-friendly code intelligence
- `repo_map` tool - a compact symbol/outline map (per-file classes, functions,
  methods + line numbers, language-agnostic). The agent reads the map first to
  locate code, then `read_file`s only the ranges it needs instead of whole files.
- **Model routing / auto-downshift** (`router: {cheap, heavy}`) - sub-agents and the
  verifier run on the cheap model automatically; reserve the premium model for the
  main reasoning loop.
- **Prompt caching** - on Anthropic, the large static system prompt is marked
  ephemeral so it is served from cache instead of re-billed every step.

### Agent-of-agent: budgets, verifier, self-heal
- **Nested sub-agents** share a call + token budget across the whole tree
  (`subagent_max_calls`, `subagent_token_budget`) so recursive delegation can't run
  away.
- **Verifier/critic** (`verify_edits: true`) - after a turn that changed files, a
  reviewer sub-agent adversarially checks the diff and posts an APPROVE / NEEDS-WORK
  verdict with concrete issues.
- **Self-heal** (`self_heal: N`) - a failed test/build command nudges the agent to
  diagnose, fix, and re-run, up to N attempts.

### Audit log & sandbox (secure by default)
- **Tamper-evident audit log** - every tool action is appended to
  `.euron/audit/audit.log` as a SHA-256 hash-chained record. Run `/audit` (or
  `euron-agent audit`) to view recent actions and cryptographically verify the chain
  is intact - any edit/insert/delete is detected.
- **Sandbox / egress policy** (`sandbox:` in config) - `deny_commands` (regex),
  `allow_commands` (deny-by-default allowlist), and `block_network` are enforced
  before any shell command runs - **even in dangerous/YOLO mode**, so autonomous
  runs stay contained.

### Doctor & CI
- `euron-agent doctor` (or `/doctor`) - environment self-check: Python version,
  installed version, active provider + key reachability, optional tools
  (git/rg/gh/pip-audit), and writable data/workspace dirs, with fix hints.
- `euron-agent init-ci` - scaffold `.github/workflows/euron-agent.yml` that runs the
  agent headlessly to scan for secrets/vulnerable deps and review pull requests.

### Multi-agent teams
A coordinator breaks work into subtasks and delegates to specialist sub-agents
(own context/tools). Team state persists across sessions:
```bash
euron-agent --team-name auth-sprint "Plan and implement user authentication"
euron-agent --team-name auth-sprint        # resume later
euron-agent team                           # list teams
```

### Scheduled agents
Cron-driven automations that survive restarts (daily PR summaries, weekly
dependency checks, health reports):
```bash
euron-agent schedule create "deps" --cron "0 8 * * MON" --prompt "Check for outdated deps" --workspace /repo
euron-agent schedule list
euron-agent schedule daemon       # run the scheduler (independent of any chat)
```

### Notifications
Push results to Slack, Discord, Telegram, Google Chat, WhatsApp (Twilio), or
Linear (`notifications:` in config) - useful with scheduled and headless runs.

### MCP, plugins, skills, rules, commands - make it your own
- **MCP servers** - connect any Model Context Protocol server; tools appear as
  `mcp__server__tool`. Config under `mcp.servers`.
- **Plugins** - install bundles of skills + commands + MCP from a folder or
  `.zip`: `euron-agent plugin add <dir|url>`.
- **Skills** - `.euron/skills/<name>/SKILL.md` (loaded on demand via `use_skill`).
- **Rules / memory** - `AGENTS.md` (or `EURON.md`/`CLAUDE.md`) auto-loaded into context.
- **Custom commands** - `.euron/commands/<name>.md` with `$ARGUMENTS` becomes `/name`.
- **Hooks** - run shell commands on PreToolUse (blocking) / PostToolUse / Stop.
- **Permissions** - `allow`/`ask`/`deny` by tool + glob, with an Always option.

### Safety and control
Approval and diffs on every mutation/command, per-turn checkpoints and undo,
cancel/stop, `.gitignore`-aware ignores, `.env` never read, command timeouts,
sandboxed paths, and bearer-token auth for remote serving.

### Live status & summary (CLI)
While the agent works, the CLI shows a live status line - a spinner, a rotating
verb, elapsed time, live token count and cost, and tool / sub-agent counters with
the current activity - while still streaming the response tokens. When a task
finishes it prints a summary: how long it took, total tokens and cost, and the
steps taken (every tool and sub-agent, in order).

### Memory & context optimization
The agent actively manages context so long sessions stay fast and within the
model's window:
- Automatic compaction - older tool outputs are trimmed (and, with `/compact`,
  LLM-summarized) once the conversation exceeds `agent.max_context_tokens`.
- Bounded tool results - very large tool outputs are capped (head + tail) in
  history, so a single big file read or command never blows up memory.
- On-demand retrieval - the model reads/searches files as needed (ripgrep +
  agentic search) instead of holding the whole repo in context.
- Live token accounting - you always see how many tokens the session has used
  (`/usage` and the live status line).

### Context and cost
`@file` mentions, automatic compaction and `/compact` summarization, token and
cost tracking, extended thinking / reasoning effort presets.

---

## Configuration

Three layers (later wins): built-in provider profiles, then `config.yaml`, then
`~/.euron-agent/config.json` (set via `/provider`, `/key`), then CLI flags.

```bash
euron-agent init        # scaffold config.yaml + .env (optional - chat works without)
```

See [backend/config.example.yaml](backend/config.example.yaml) for every option:
providers, agent knobs (max_steps, thinking, fallback_models, ...), permissions,
hooks, mcp.servers, web, and notifications.

User data lives in `~/.euron-agent/`: config.json (provider/keys), sessions/
(history), permissions.json, plugins/, skills/, commands/, schedules.json.

---

## What is included vs. not

Included (parity with, or beyond, Claude Code / Cursor / Cline): agentic loop,
multi-file edits, plan mode, multi-agent teams, scheduled agents, MCP, plugins,
skills, rules/memory, custom commands, hooks, permissions, approvals + diffs,
checkpoints/undo, cancel, named sessions (resume/dashboard/search), web
search/fetch, multimodal images, cost/usage, thinking/effort, git + CI/PR tools,
worktree isolation, code review, notifications (Slack/Discord/Telegram/Google
Chat/WhatsApp/Linear), headless JSON, self-host + auth, every model.

Intentionally not included (different product surface, or would break
model-agnosticism):

- Cursor Tab (proprietary inline-completion model)
- A hosted cloud service with managed background agents or mobile apps
- A managed PR-review SaaS (we ship the local `/review` equivalent)
- Computer-use GUI control and browser automation
- Vector-DB / embeddings indexing (we use ripgrep + agentic search, like Claude Code)

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and
[PROJECT_GUIDE.md](PROJECT_GUIDE.md) for a file-by-file tour.

---

## Self-hosting / cloud

```bash
euron-agent serve --host 0.0.0.0 --port 8000     # prints a bearer token
# point the extension at it: euronAgent.serverUrl = ws://HOST:8000/ws ; euronAgent.token = <token>
```
REST `POST /agent/run` (with `Authorization: Bearer ...`) for CI/CD. WebSocket
`/ws` for streaming + approvals.

## Develop and test

```bash
cd backend && pip install -e ".[dev]" && pytest -v     # 60 tests
cd extension && npm install && npm run esbuild && npx vsce package
```

## Publishing (maintainers)

Tag a release and CI publishes everything; see [PUBLISHING.md](PUBLISHING.md) and
`.github/workflows/`.
```bash
git tag v1.0.2 && git push origin v1.0.2     # PyPI + Marketplace + Open VSX
```

## Roadmap / ideas

Ideas to make the CLI experience even better (contributions welcome):
- Syntax-highlighted diffs and markdown in the terminal; a split diff view.
- `@file` path autocomplete in chat (alongside the `/` command popup).
- A TUI dashboard (sessions, running schedules, teams) in one screen.
- Cost budgets and alerts ("stop at $1"), and a per-session cost ceiling.
- Session branching / forking and a richer transcript search.
- Inline edit acceptance ("accept/reject this hunk") during approvals.
- Themes and an output-style picker; a status line you can customize.
- Voice input and spoken summaries; desktop notifications on completion.
- A `doctor` command that checks Python, keys, providers, and MCP servers.

## Contributing

Issues and PRs welcome. The agent is built to be extended - add a tool in
`backend/euron_agent/tools.py`, a provider in `config.py`, or a skill/plugin with
no code at all. Run `pytest` before submitting.

## License

Apache License 2.0. Copyright 2026 Euron Engage Sphere Technology Private Limited.
See [LICENSE](LICENSE) and [NOTICE](NOTICE).
