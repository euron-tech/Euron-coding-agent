# Euron Coding Agent

A lightweight, **Cline/OpenCode-style agentic coding assistant** with the agent
brain written entirely in **Python** and a thin **VS Code** front-end. It also
runs **fully from the CLI** — no editor required.

It is **provider-agnostic**: point it at Euron/Euri, OpenAI, OpenRouter,
Anthropic, or any **self-hosted / OpenAI-compatible** server (Ollama, vLLM, LM
Studio, llama.cpp) by editing one config file.

```
VS Code webview ──postMessage──> Extension host ──WebSocket──> Python FastAPI
                                                                     │
                                                            agent loop (tool calling)
                                                                     │
                                              read · search · edit · write · run command
```

## Why it behaves well even with small models

- **Native tool-calling agent loop** (plan → read → edit → run → verify), not a
  one-shot "return JSON" hack. The model iterates on real tool results.
- **Surgical `edit_file`** (exact search/replace, like Claude Code) so small
  models don't have to regurgitate whole files.
- **Tight, plan-first system prompt** and only-what's-needed context.
- **Approval gating with diffs** — nothing is written or executed without your OK.

## Claude-Code-style capabilities (model-agnostic)

- **Plan mode** · **sub-agents** (`spawn_agent`) · live **TODO checklist**
- **MCP** — connect any Model Context Protocol server (`pip install
  "euron-coding-agent[mcp]"`); tools appear as `mcp__server__tool`
- **Web** `web_search` + `web_fetch` · **git** tools · **background** processes
- `glob`, `multi_edit`, streaming command output, `@file` mentions
- **Undo/checkpoints**, **cancel/stop**, **persistent history**, **`/compact`**
- **Cloud/self-host**: `serve --host 0.0.0.0 --token …` with bearer auth
- Works with **any** OpenAI-compatible or Anthropic model; capabilities degrade
  gracefully when a model lacks them.

---

## 1. Backend (Python)

```bash
cd backend
python -m venv venv
# Windows PowerShell:
venv\Scripts\Activate.ps1
# (bash/macOS/Linux: source venv/bin/activate)

pip install -r requirements.txt        # add: pip install anthropic  (only for Anthropic)
pip install -e .                        # installs the `euron-agent` command

euron-agent init                        # creates config.yaml + .env in this folder
```

Edit **`config.yaml`** to choose a provider and **`.env`** to add the API key.
Examples for Euri, OpenAI, OpenRouter, Ollama, vLLM and Anthropic are all in
`config.example.yaml`.

### Use it from the terminal

```bash
euron-agent providers                   # list configured providers
euron-agent run "add a /health route to app.py" --workspace .
euron-agent chat                        # interactive REPL (keeps context)
euron-agent run "..." --provider ollama --model qwen2.5-coder:7b
euron-agent run "..." -y                # auto-approve edits & commands
```

### Or run it as a server (for VS Code / cURL)

```bash
euron-agent serve --port 8000
# REST smoke test (auto-approve so it actually writes):
curl -X POST http://127.0.0.1:8000/agent/run \
  -H "Content-Type: application/json" \
  -d '{"task":"create hello.py that prints hi","workspace_path":"/abs/path","auto_approve":true}'
```

---

## 2. VS Code extension

**End users** don't do any of the backend setup above — they just install the
extension. On first run it auto-creates a private Python venv, `pip install`s
`euron-agent` from PyPI, and manages the server itself. They pick a provider
(server icon), paste a key (key icon, stored in SecretStorage), and go.

**To develop the extension:**

```bash
cd extension
npm install
npm run esbuild        # bundle src -> out/extension.js
```

Open the `extension/` folder in VS Code and press **F5** to launch an Extension
Development Host. Click the **Euron Agent** icon in the activity bar. Use the
toolbar buttons to **Select Provider** and **Set API Key**.

To develop against your local backend instead of a PyPI install, run
`euron-agent serve --port 8000` and set `euronAgent.serverUrl` to
`ws://127.0.0.1:8000/ws`.

| Setting | Meaning |
|---|---|
| `euronAgent.model` | Override the model for the active provider. |
| `euronAgent.pythonPath` | Python 3.9+ to use. Empty = auto-detect. |
| `euronAgent.backendVersion` | Pin the PyPI backend version. Empty = latest. |
| `euronAgent.serverUrl` | Advanced: connect to a backend you run yourself. |

## Distribution

To publish so anyone can install it, see **[PUBLISHING.md](PUBLISHING.md)** —
PyPI + VS Code Marketplace + Open VSX, automated via GitHub Actions on a version
tag.

---

## 3. Project layout

```
backend/
  euron_agent/
    config.py        # layered config + provider profiles
    llm.py           # OpenAI-compatible + Anthropic clients (one interface)
    tools.py         # sandboxed file/search/command tools + diff generation
    tool_schemas.py  # function-calling schemas
    prompts.py       # system prompt
    loop.py          # the agentic loop (streaming + approval gating)
    events.py        # event protocol + AgentIO interface
    server.py        # FastAPI: /ws (streaming) + /agent/run (REST)
    cli.py           # run / chat / serve / providers / init
  config.example.yaml
extension/
  src/extension.ts   # webview + backend connection/auto-start bridge
  media/             # chat UI (main.js, style.css)
```

## Safety

Edits and shell commands require explicit approval (toggle per-provider in
config). All file access is sandboxed to the workspace root; `.env` and ignored
paths are never read or written; commands have a timeout. See
`backend/config.example.yaml` → `agent:` and `ignore:`.

## Roadmap (post-MVP)

Git-diff checkpoints/undo · per-project memory · `@file` mentions · multi-root ·
test-runner tool · RAG over the codebase · model picker in the UI.
