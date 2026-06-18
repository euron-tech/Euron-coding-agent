# Euron Coding Agent

An open-source, model-agnostic agentic coding assistant for VS Code. It plans,
reads your code, makes edits, runs commands, reviews diffs, and coordinates teams
of sub-agents - with your approval and a diff at every step.

Works with Anthropic, OpenAI, Google Gemini, OpenRouter, Groq, Cerebras, DeepSeek,
Together, Mistral, xAI, Vercel AI Gateway, Euron/Euri, Ollama, LM Studio, or any
OpenAI-compatible / self-hosted endpoint. Bring your own key; nothing is sent to
us.

## Features

- Agentic loop: plan, read, search, edit, run, verify - iterating on real results
  (native tool-calling, not a one-shot prompt).
- Approval and diffs - no file is written and no command runs without your OK.
- Plan mode, sub-agents, skills, MCP servers, plugins, project memory (AGENTS.md),
  custom commands, hooks, and fine-grained permissions.
- Web search and fetch, multimodal image input, token and cost tracking, extended
  thinking, model fallback chains, code review, and a Fix Diagnostics command.
- Keys stay local - stored in VS Code SecretStorage, never in plaintext files.
- Zero manual setup - the extension installs and manages its Python backend for
  you in a private environment.

The CLI (pip install euron-coding-agent) adds multi-agent teams, scheduled agents
(cron), messaging notifications (Slack, Discord, Telegram, Google Chat, WhatsApp,
Linear), and headless JSON output.

## Getting started

1. Install the extension and open the Euron Agent view in the activity bar.
2. Click the server icon to pick a provider; click the key icon to paste your API
   key (skip for local Ollama).
3. Type a task, for example: "add a /health route to app.py and a test for it."
4. Review the proposed diffs and click Approve.

Requirement: Python 3.9+ must be available on your machine (used to run the agent
backend). The extension auto-detects it and sets everything else up.

## Settings

| Setting | Description |
|---|---|
| `euronAgent.model` | Override the model for the active provider. |
| `euronAgent.effort` | Reasoning effort: low, medium, high. |
| `euronAgent.pythonPath` | Python 3.9+ to use. Empty = auto-detect. |
| `euronAgent.backendVersion` | Pin the backend version from PyPI. Empty = latest. |
| `euronAgent.serverUrl` | Advanced: connect to a backend you run yourself. |
| `euronAgent.token` | Bearer token for a remote/self-hosted backend. |
| `euronAgent.autoDiagnostics` | Offer to fix new errors after a task. |

## Privacy

Your prompts and the relevant file contents are sent directly to the LLM provider
you configure. This extension runs no server of its own and collects no telemetry.
Review your provider's data policy before sending proprietary code.

## License

Apache License 2.0. Copyright 2026 Euron Engage Sphere Technology Private Limited.
