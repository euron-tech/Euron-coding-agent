# Changelog

## 1.4.0

The full loop: one sentence → built → tested → security-scanned → deployed to a live
URL → monitored → self-healing. No CLI agent ships first-class deploy + production
monitoring — this does, on infra you control, with safety gates.

- **`ship`** (`euron-agent ship "<what>"` / `/ship`): plan → build → test →
  security-scan (`secret_scan` + `dependency_audit`) → deploy → wire monitoring →
  report the live URL.
- **`deploy`** (`/deploy`, `euron-agent deploy [--check]`): headless deployment to
  Cloud Run, Cloudflare Workers/Pages, Vercel, Netlify, Render, Fly, Railway, Docker,
  Kubernetes/Helm, AWS (SAM/App Runner), Azure Container Apps — plus Neon and Supabase
  database provisioning. Each via its CLI + a credential env var. `--check` shows which
  targets are ready.
- **Spend gate (guardrails):** free-tier targets deploy directly; billable targets are
  blocked unless `deploy.allow_billable: true` (or set to ask via
  `deploy.free_tier_only: false`). Credentials are read from env vars only and redacted
  from output; every deploy/rollback is recorded in the tamper-evident audit log.
- **Monitoring** (`monitor` / `/monitor`): Sentry / OpenTelemetry / Datadog / uptime
  (UptimeRobot, Better Stack) wiring + a `/health` scaffold + live error/uptime status.
- **Self-healing** (`heal` / `/heal`): policy = rollback auto, fixes as PRs. A failed
  health check triggers an automatic rollback to the last known-good release; each
  production error becomes a fix + regression test opened as a PR (never auto-merged or
  auto-deployed). Pair with `schedule` for continuous monitor-and-remediate.
- New tools the agent can call: `deploy_check`, `deploy`, `rollback`, `provision_db`,
  `monitor_status`, `health_check`. New config blocks: `deploy:`, `monitoring:`,
  `selfheal:`. See docs/DEPLOY.md.

## 1.3.0

Multi-model routing — one model per job, across providers, no lock-in.

- Assign a different model (from any provider) to each phase of the work via
  `models:` and `routing:` in `config.yaml`:
  - `plan` → a strong reasoning model
  - `execute` → your balanced main model
  - `subagent` → a cheap/fast model for delegated work
  - `verify` → a cheap model for the post-edit critic
  - `escalate` → the strong model to auto-jump to when quality slips
- Each role can target a different provider (OpenAI + Anthropic + Groq + Gemini in
  one config), each using its own API key.
- Cost strategy: `strategy: auto` (default) runs cheap models where safe and
  **automatically escalates** to the stronger `escalate` model after
  `escalate_after` consecutive failed steps — pay for the expensive model only on
  the hard parts. `strategy: fixed` disables escalation.
- `euron-agent models` and `/models` show the routing table: which model + price
  runs each phase.
- Cost is now tracked per model actually used (accurate blended `/usage`).
- Back-compatible: the older `router: {cheap, heavy}` / `subagent_model:` keys still
  work and seed `cheap`/`heavy` roles automatically.
- See docs/MULTI_MODEL.md.

## 1.2.0

Agent-of-agent, token-friendly, and security features:

- **repo_map tool** — a compact symbol/outline map of the codebase (per-file
  classes/functions/methods with line numbers). The agent reads outlines first and
  `read_file`s only the ranges it needs, cutting token usage on large repos.
- **secret_scan** and **dependency_audit** tools — find hard-coded credentials
  (masked in output) and known-vulnerable dependencies (pip-audit / npm audit /
  cargo audit / govulncheck).
- **/scan** (fast secret + dep scan) and **/secfix** (autonomous audit → fix →
  verify remediation loop) commands, plus `euron-agent scan` / `secfix`.
- **Tamper-evident audit log** — every tool action is appended to
  `.euron/audit/audit.log` as a SHA-256 hash-chained record. `/audit` or
  `euron-agent audit` shows and cryptographically verifies it.
- **Sandbox / egress policy** (`sandbox:` config) — deny-by-default command rules,
  `block_network`, and an allowlist enforced before any shell command runs, even
  in dangerous mode.
- **Agent-of-agent budgets** — nested sub-agents share a call + token budget
  (`subagent_max_calls`, `subagent_token_budget`) so recursive delegation can't run
  away.
- **Model routing / auto-downshift** (`router: {cheap, heavy}`) — sub-agents and the
  verifier use the cheap model automatically.
- **Verifier/critic agent** (`verify_edits: true`) — after a turn that changed
  files, a reviewer sub-agent adversarially checks the diff and reports a verdict.
- **Self-heal** (`self_heal: N`) — a failed test/build command nudges the agent to
  diagnose, fix, and re-run, up to N attempts.
- **euron-agent doctor** — environment self-check (Python, version, provider/key,
  tools, writable dirs). Also `/doctor`.
- **euron-agent init-ci** — scaffold a GitHub Actions workflow that runs the agent
  to scan and review pull requests headlessly.
- **Prompt caching** for Anthropic — the large static system prompt is marked
  ephemeral so it is reused from cache instead of re-billed every step.

## 1.1.0

- Drag-and-drop context: reference or drop a **file, folder, or image** path into
  chat and the agent reads and uses it automatically. Text files of any reasonable
  length (smart head+tail truncation when huge), folders read recursively
  (bounded), and images converted to multimodal blocks for vision models. Detects
  quoted paths, absolute/workspace-relative paths, bare filenames, and `@mentions`.
  Fixes images and dropped files/folders not being picked up.
- Security audit: `/security` (and `euron-agent security`) reviews the code for
  vulnerabilities (injection, authz, secrets, SSRF, path traversal, unsafe
  deserialization, dependency risks) with severity-ranked findings and fixes.
- Autonomous testing: `/test [target]` writes and runs tests for a file/module;
  `/testall` (and `euron-agent test --all`) builds and runs a comprehensive test
  suite for the whole project and reports coverage gaps.
- Plan mode and execute mode are now both first-class and switchable mid-session
  via `/plan` and `/execute` (execute is the default).
- More playful live status verbs (Spelunking, Conjuring, Untangling, Percolating…).

## 1.0.8

- Auto-onboarding: scaffolds a `.euron/` wrapper (memory AGENTS.md, PROJECT.md,
  and an explore-codebase skill) automatically on first run, pre-filled with the
  detected stack and build/test/lint commands. Re-run via `euron-agent onboard`,
  `/onboard`, or the "Onboard Project" command. Disable with `--no-onboard` /
  `agent.auto_onboard: false`.

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
