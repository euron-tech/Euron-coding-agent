# Euron Coding Agent — Command Reference

Every command, with a description. Two surfaces:

- **CLI subcommands** — `euron-agent <command> [options]` in your shell.
- **In-chat slash commands** — typed at the `you:` prompt during an interactive
  session (`euron-agent` with no arguments).

Global CLI flags (must come **before** the subcommand): `--config <path>`,
`--provider <name>`, `--model <id>`, `--workspace <dir>`, `--team-name <name>`,
`--dangerous`, `--no-onboard`, `--version/-V`.

---

## CLI subcommands

| Command | Description |
|---|---|
| `euron-agent` | Start an interactive chat session in the current folder (remembers provider/key). |
| `euron-agent run "<task>"` | Run a single task and exit. `--yes/-y` auto-approves; `--json` streams events as JSON for CI/scripting (headless, auto-approve). |
| `euron-agent chat` | Interactive REPL. `--session <id>` opens a specific saved session; `--resume` continues the latest. |
| `euron-agent serve` | Run the HTTP / WebSocket server (self-host). `--host`, `--port` (0 = auto), `--token`, `--no-auth`, `--reload`. Prints a bearer token. |
| `euron-agent providers` | List the configured provider profiles and which keys are set. |
| `euron-agent models` | Show the multi-model routing table — which model (and price) runs each phase (plan/execute/subagent/verify/escalate). See [MULTI_MODEL.md](MULTI_MODEL.md). |
| `euron-agent init` | Scaffold a starter `config.yaml` and `.env` in the current folder. |
| `euron-agent update` | Update the installed `euron-coding-agent` package to the latest version. |
| `euron-agent version` | Print the installed version. |
| `euron-agent onboard` | (Re)scaffold the `.euron/` wrapper — memory (`AGENTS.md`), `PROJECT.md`, and a starter skill — for this repo. |
| `euron-agent security` | Run a thorough security audit of the codebase (severity-ranked findings + fixes). `--yes` to auto-approve. |
| `euron-agent scan` | Fast risk scan: `secret_scan` (hard-coded credentials) + `dependency_audit` (vulnerable deps). `--yes` to auto-approve. |
| `euron-agent secfix` | Autonomous security remediation loop: audit → plan → fix → re-scan & re-test to verify. `--yes` to auto-approve. |
| `euron-agent test` | Write and run tests for the recent/changed code. `--all` builds a full project test suite; `--yes` to auto-approve. |
| `euron-agent ship "<what>"` | End-to-end: plan → build → test → security-scan → deploy to a live URL → wire monitoring. See [DEPLOY.md](DEPLOY.md). |
| `euron-agent deploy ["<target/notes>"]` | Deploy the project to a live URL in one prompt. `--check` just lists targets + readiness (CLI + token). |
| `euron-agent monitor` | Show monitoring status — configured providers (Sentry/OTel/Datadog/uptime) and current errors/uptime. |
| `euron-agent heal ["<url>"]` | Self-heal: health-check the deployed URL, auto-rollback if unhealthy, and open a PR for each production error (never auto-merged). |
| `euron-agent doctor` | Environment self-check: Python version, installed version, provider + key reachability, optional tools (git/rg/gh/pip-audit), writable dirs. |
| `euron-agent audit` | Show recent entries from the tamper-evident action log and cryptographically verify the hash chain. `--lines N` (default 25). |
| `euron-agent init-ci` | Write a GitHub Actions workflow (`.github/workflows/euron-agent.yml`) that scans + reviews pull requests headlessly. `--force` overwrites. |
| `euron-agent plugin <add\|list\|remove> [src]` | Manage plugins (skills/commands/MCP bundles) from a directory or `.zip` URL. |
| `euron-agent sessions` | List saved sessions (dashboard). `--all` covers every workspace. |
| `euron-agent team` | List multi-agent teams and their persistent state. |
| `euron-agent schedule <create\|list\|remove\|run\|daemon>` | Cron-driven scheduled agents. `--cron "0 9 * * MON-FRI"`, `--prompt`, `--workspace`. |

---

## In-chat slash commands

### Setup & configuration
| Command | Description |
|---|---|
| `/provider <name>` | Switch the active provider (openai, anthropic, gemini, groq, ollama, …). |
| `/key` | Paste/set the API key for the active provider (stored in `~/.euron-agent`). |
| `/model <id>` | Set the model id for the active provider. |
| `/baseurl <url>` | Set a custom base URL (self-hosted / proxy endpoints). |
| `/config` | Show current provider, model, base URL, and key status. |
| `/providers` | List all known provider profiles. |
| `/models` | Show the multi-model routing table (which model runs each phase + price). |
| `/effort <low\|medium\|high>` | Set reasoning effort for capable models. |

### Modes
| Command | Description |
|---|---|
| `/plan` | Plan mode for the next task — research read-only, propose a plan, then you approve before any change. |
| `/execute` | Execute mode (default) — carry out the next task directly. |

### Code work & review
| Command | Description |
|---|---|
| `/review` | Review the current uncommitted git changes for bugs, security, and improvements (like a code review). |
| `/security` | Full security audit of the codebase, prioritized by severity with concrete fixes. |
| `/scan` | Fast secret + dependency vulnerability scan. |
| `/secfix` | Autonomous security remediation: audit → fix (with approval) → re-scan & re-test. |
| `/test [target]` | Write tests for the code (or the named target) and run them. |
| `/testall` | Build and run a comprehensive test suite for the whole project, then report coverage gaps. |
| `/ship [what]` | End-to-end: plan → build → test → security-scan → deploy → monitor. |
| `/deploy [target]` | Deploy the project to a live URL in one prompt. |
| `/deploys` | List deploy targets and which are ready (CLI installed + token set). |
| `/monitor` | Show monitoring status (errors / uptime / configured providers). |
| `/heal [url]` | Self-heal: health-check, auto-rollback if unhealthy, open PRs for errors. |

### Project, memory & skills
| Command | Description |
|---|---|
| `/init` | Create an `AGENTS.md` project-memory file. |
| `/onboard` | Scaffold the `.euron/` wrapper (memory + skill + project doc) for this repo. |
| `/skills` | List available skills (`.euron/skills/<name>/SKILL.md`). |
| `/search <text>` | Search your past sessions. |

### Safety, audit & diagnostics
| Command | Description |
|---|---|
| `/audit` | Show and verify the tamper-evident action audit log (`.euron/audit/`). |
| `/doctor` | Run an environment self-check. |
| `/yes` | Toggle auto-approve for edits & commands. |
| `/dangerous` | Toggle DANGEROUS (YOLO) mode — run everything, never ask. Sandbox/egress rules still apply. |

### Session control
| Command | Description |
|---|---|
| `/usage` | Show tokens, cost, and tool usage this session. |
| `/compact` | Summarize the conversation to free up context. |
| `/undo` | Revert the file changes from the last task. |
| `/reset` | Clear the conversation context. |
| `/help` | Show the in-chat help (Ctrl+C during a task = stop). |
| `/exit` | Quit the session. |

---

## Tools the agent can call

Beyond the commands above, the model drives these tools autonomously (gated by your
approval rules). Notable additions in 1.2.0:

| Tool | Description |
|---|---|
| `repo_map` | Compact per-file symbol/outline map (classes, functions, methods + line numbers) so the agent locates code without reading whole files. |
| `secret_scan` | Scan the workspace for hard-coded secrets/credentials; findings are masked. |
| `dependency_audit` | Audit dependencies for known vulnerabilities (pip-audit / npm audit / cargo audit / govulncheck). |

See [the README](../README.md) for the full tool list and configuration
(`router`, `sandbox`, `verify_edits`, `self_heal`, `subagent_max_calls`, …).
