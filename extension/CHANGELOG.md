# Changelog

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
