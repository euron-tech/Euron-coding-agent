# Architecture & Codebase Guide

This document is the deep-dive map of the **Euron Coding Agent** codebase — what
each file does, how a request flows end to end, and **what is and isn't
implemented**. (`README.md` is the user-facing intro; this is the engineering
reference.)

---

## 1. The big picture

Two deployable units that share one agent brain:

```
┌─────────────────────────────────────────────────────────────────────┐
│  VS Code extension (TypeScript)          │  CLI (Python, same brain)  │
│  webview chat ⇄ extension host ⇄ ws      │  terminal ⇄ AgentSession   │
└───────────────────────┬──────────────────────────────┬──────────────┘
                        │ WebSocket / in-process         │
                        ▼                                ▼
                 ┌──────────────────────────────────────────────┐
                 │            Python backend (euron_agent)        │
                 │                                                │
                 │  AgentSession.run()  ── the agentic loop ──┐   │
                 │        │                                   │   │
                 │        ▼                                   │   │
                 │   LLM client (OpenAI-compat | Anthropic)   │   │
                 │        │  tool calls                       │   │
                 │        ▼                                   │   │
                 │   tools.py (read/edit/run, sandboxed) ◀────┘   │
                 │        │  approval gate (AgentIO)              │
                 └────────┼───────────────────────────────────────┘
                          ▼
                   your workspace files / shell
```

The **key design choice**: the agent loop is transport-agnostic. It only talks
to the world through an `AgentIO` interface (emit events, request approval).
The CLI implements `AgentIO` with the terminal; the server implements it over a
WebSocket. The exact same loop, tools, and prompts run in both.

---

## 2. Repository layout

```
backend/
  euron_agent/
    __init__.py        # package version
    __main__.py        # enables `python -m euron_agent ...` (used by the extension)
    config.py          # layered config + built-in provider profiles
    events.py          # event constructors + AgentIO interface + ApprovalDecision
    llm.py             # provider-agnostic LLM clients (OpenAI-compat + Anthropic)
    prompts.py         # the system prompt
    tool_schemas.py    # function-calling schemas + which tools need approval
    tools.py           # sandboxed tool implementations + diff/preview generation
    loop.py            # AgentSession — the agentic loop (plan mode, subagents, MCP)
    context.py         # token estimate, @mention expansion, compaction, summarize
    checkpoints.py     # per-turn file snapshots → undo
    history.py         # persist conversation per workspace
    settings.py        # ~/.euron-agent user settings (CLI)
    webtools.py        # web_search (pluggable) + web_fetch
    background.py      # long-running background process manager
    mcp_client.py      # MCP server client → external tools (optional)
    server.py          # FastAPI: /ws (streaming+approval) + REST + dynamic port + auth
    cli.py             # run / chat / serve / providers / init  (+ TerminalIO)
  pyproject.toml       # packaging → PyPI dist "euron-coding-agent", CLI "euron-agent"
  requirements.txt
  config.example.yaml  # copyable config with every provider example
  .env.example

extension/
  src/extension.ts     # backend lifecycle + secrets + webview/ws bridge
  media/main.js        # webview chat UI (streaming, diffs, approval cards)
  media/style.css      # theme-aware styling
  media/icon.png/.svg  # marketplace + sidebar icons
  esbuild.js           # bundles src → out/extension.js (single file)
  package.json         # commands, settings, view, publisher
  .vscodeignore

.github/workflows/     # ci.yml (build/test), release.yml (PyPI + Marketplace + OpenVSX)
PUBLISHING.md          # one-time publish setup (tokens/accounts)
ARCHITECTURE.md        # this file
```

---

## 3. Request lifecycle (end to end)

### In the VS Code extension
1. User types a task and hits Run. `media/main.js` posts `{command:'run', text}`
   to the extension host.
2. `ChatViewProvider.runTask` (extension.ts) builds an `init` payload via
   `buildInitPayload` — reads the active provider (globalState) and its API key
   (SecretStorage), plus optional base_url/model.
3. `BackendManager.getWsUrl()` ensures a backend is running: detect Python →
   create a private venv in the extension's global storage → `pip install
   euron-coding-agent` → spawn `python -m euron_agent serve --port 0` → parse the
   `EURON_AGENT_LISTENING http://127.0.0.1:<port>` line it prints → derive the ws URL.
4. The host opens a WebSocket and sends `init` then `run`.
5. **Backend** (`server.ws_endpoint`): `init` builds a `Config` (with the
   injected key) and an `AgentSession`. `run` launches `session.run(task)` as a
   background task so approval messages can still be received concurrently.
6. The loop streams `token`/`tool_start`/`diff`/`approval_request`/... events
   back over the socket. The host relays each to the webview, which renders them.
7. On `approval_request`, the webview shows a diff + Approve/Reject; the user's
   answer goes host → ws → `WebSocketIO.resolve_approval`, unblocking the loop.

### In the CLI
`euron-agent run "..."` → `cli._run_task` builds the `Config` and a `TerminalIO`,
constructs `AgentSession`, and calls `run`. Same loop; approvals are answered at
a terminal prompt; tokens print live.

---

## 4. Backend, module by module

### `config.py`
- Dataclasses `ProviderConfig`, `AgentConfig`, `Config`.
- `BUILTIN_PROVIDERS` — ready-made profiles for `euri`, `openai`, `openrouter`,
  `ollama`, `anthropic`, `custom`. **This is why a fresh install with no config
  file works**: the extension just passes a provider name + key.
- `load_config(config_path, provider, model, api_key, base_url)` — merges
  built-ins ← `config.yaml` ← explicit overrides. `api_key`/`base_url` let the
  extension inject secrets at runtime so nothing must live on disk.

### `llm.py`
- Normalized types: `ToolCall`, `LLMResponse(content, tool_calls)`.
- `build_client(provider)` → `OpenAICompatClient` or `AnthropicClient`.
- **`OpenAICompatClient`** — wraps the `openai` SDK with a configurable
  `base_url`, so it speaks to *any* OpenAI-compatible server. `_chat_stream`
  accumulates streamed content deltas **and** partial tool-call deltas into
  complete `ToolCall`s.
- **`AnthropicClient`** — converts the OpenAI-format message history to
  Anthropic's blocks (`_to_anthropic_messages`: system extracted, tool results
  folded into user turns) and tools (`_to_anthropic_tools`), streams text, and
  collects tool_use blocks. The loop stays provider-neutral because conversion
  happens here.
- `_safe_json_loads` tolerates the malformed JSON small/local models sometimes
  emit for tool arguments.

### `tools.py`
- `ToolContext` holds the workspace root + agent config + ignore globs and
  enforces the **sandbox**: `resolve()` rejects any path escaping the root;
  `is_ignored()` blocks `.env`, `.git`, `node_modules`, etc.
- Read tools: `list_files`, `read_file` (size-capped, optional line range,
  line-numbered), `search_text` (uses `rg` if present, else a Python walk).
- Mutating tools: `write_file`, `edit_file` (exact unique search/replace — fails
  loudly if `old_string` isn't found or isn't unique), `create_file`,
  `delete_file`, `run_command` (shell, timeout-bounded).
- Every mutating tool returns a unified **diff** (`_unified`) used by the
  approval UI. `preview_for()` builds that diff *without* writing, so the user
  sees the change before approving. `execute()` is the dispatch entry point.

### `tool_schemas.py`
- `TOOL_SCHEMAS` — the OpenAI function-calling schemas advertised to the model.
- `MUTATING_TOOLS` — the set gated behind approval (`write/edit/create/delete_file`,
  `run_command`).

### `prompts.py`
- `system_prompt(workspace, file_tree)` — a tight, **plan-first** prompt with
  explicit tool-use discipline (ground yourself before editing, edit surgically,
  one step at a time, stop when done). This discipline is what makes small models
  behave well on focused tasks.

### `loop.py` — `AgentSession`
The core. `run(task)`:
1. Seeds the system prompt (with a file-tree snapshot) on first turn; appends the
   user task. History is kept in **OpenAI message format** for all providers.
2. `_agent_loop` iterates up to `max_steps`:
   - Calls the LLM off the event loop (`asyncio.to_thread`), streaming tokens via
     `_stream_token` → `io.on_token`.
   - **No tool calls → the turn is done** (`done` event, return).
   - Otherwise records the assistant message + tool calls, then runs each call.
3. `_handle_tool_call`: for `MUTATING_TOOLS` not auto-approved, builds a preview
   and calls `io.request_approval`. On reject, feeds the rejection (and any user
   note) back to the model as the tool result so it can adapt. On approve,
   `execute()`s the tool, emits `diff`/`tool_result`, and appends the result.

### `events.py`
- Event constructors (`status`, `token`, `assistant_message`, `tool_start`,
  `tool_result`, `diff`, `approval_request`, `done`, `error`) — plain dicts that
  serialize straight to JSON.
- `AgentIO` (abstract): `on_token` (sync, thread-safe), `emit` (async),
  `request_approval` (async → `ApprovalDecision`). The seam between the loop and
  any transport.

### `server.py`
- `WebSocketIO` implements `AgentIO` over a socket; `on_token` hops back to the
  event loop with `run_coroutine_threadsafe`; `request_approval` parks a future
  resolved by an incoming `approval` message.
- `ws_endpoint` message router: `init` / `run` (spawned as a task) / `approval` /
  `ping`.
- REST: `POST /agent/run` (one-shot, non-interactive via `BufferIO`), `GET
  /health`, `GET /providers`.
- `serve(port=0)` picks a free port and prints `EURON_AGENT_LISTENING ...` for
  the extension to discover.

### `cli.py`
- `TerminalIO` implements `AgentIO` for the terminal: streams tokens, renders
  diffs with color, prompts `y/n/feedback` for approvals.
- Commands: `run`, `chat` (persistent session/memory across turns), `serve`,
  `providers`, `init`. `_force_utf8()` avoids Windows code-page crashes on
  box-drawing/emoji.

---

## 5. Extension, module by module (`src/extension.ts`)

- **`PROVIDERS`** — the provider menu metadata (which need a key, which is custom).
- **`configureProvider`** — QuickPick provider → InputBox key → store key in
  `context.secrets` and the active provider in `globalState`. Custom also stores
  base_url + model.
- **`buildInitPayload`** — assembles the `init` message; if a key-requiring
  provider has no stored key, it prompts to set one.
- **`BackendManager`** — the lifecycle owner:
  - `detectPython` tries the configured path, then `python3`/`python`/`py -3`,
    requiring ≥3.9.
  - `provision` creates a venv under global storage and `pip install`s
    `euron-coding-agent` (re-installs when the extension version or pinned
    version changes; tracked via a marker file), with a progress notification.
  - `startManaged` spawns the server with `--port 0` and resolves the ws URL from
    the announced port. `getWsUrl` short-circuits to `serverUrl` if you point it
    at a backend you run yourself.
- **`ChatViewProvider`** — the webview host: renders the HTML (CSP + nonce),
  relays webview↔ws messages, manages connect/retry.
- **`media/main.js`** — renders the event stream: user/assistant bubbles,
  streamed tokens, tool lines, diff blocks (colored), and approval cards with
  Approve/Reject + optional feedback. **`media/style.css`** uses VS Code theme
  variables so it matches light/dark.

---

## 6. The event & WebSocket protocol

Client → server: `init`, `run`, `approval`, `ping`.
Server → client (one JSON object per event):

| type | meaning |
|---|---|
| `status` | progress note (“thinking…”, “connected”) |
| `token` | a streamed chunk of assistant text |
| `assistant_message` | a completed assistant message |
| `tool_start` | a tool call is beginning (name + args) |
| `diff` | unified diff for a file mutation |
| `tool_result` | tool output + ok/fail |
| `approval_request` | needs human decision (+ diff/command preview) |
| `done` | turn finished |
| `error` | something failed |

---

## 7. Configuration & providers

Precedence (low → high): built-in profiles → `config.yaml` → env/`.env` →
explicit CLI/extension overrides. Any OpenAI-compatible endpoint works by setting
`base_url`; `type: anthropic` switches to the native client. See
`config.example.yaml`.

---

## 8. Distribution

- **Backend** → PyPI as `euron-coding-agent` (import package `euron_agent`, CLI
  `euron-agent`). Built with `python -m build`.
- **Extension** → bundled by `esbuild.js` into one file, packaged with `vsce`,
  published to the VS Code Marketplace (publisher `Euron`) and Open VSX.
- **CI/CD** → `.github/workflows/release.yml` publishes both on a `v*` tag;
  `ci.yml` builds/tests on push. See `PUBLISHING.md`.

---

## 9. What's implemented ✅ vs not yet ❌

### Implemented
- ✅ Agentic loop with **native tool-calling** and multi-step iteration.
- ✅ Provider-agnostic LLM layer (OpenAI-compatible + Anthropic), **streaming**,
  with **retry/backoff** and **token-usage** accounting.
- ✅ Tools: list, read (ranged, binary-guarded), search (ripgrep), write,
  **surgical edit**, create, delete, run command — all **workspace-sandboxed**.
- ✅ **Approval gating with diffs** for every mutation/command, plus an
  **auto-approve** toggle (CLI `/yes`, extension toolbar).
- ✅ **Cancel/stop** mid-task and **undo** (per-turn checkpoint/revert).
- ✅ **Streaming command output** (live stdout/stderr).
- ✅ **Context management**: token estimate + automatic compaction of old tool
  output when over the window budget; **@file mentions** inline file contents.
- ✅ **Persistent history** per workspace across restarts (CLI `--resume`,
  extension `persistHistory`).
- ✅ **.gitignore-aware** ignores in addition to configured globs.
- ✅ **Multi-root workspace** selection.
- ✅ Two front-ends sharing one brain: VS Code webview + terminal CLI.
- ✅ Zero-setup backend (auto venv + PyPI install) and **SecretStorage** keys.
- ✅ **Cloud/self-host ready**: bearer-token auth + bind to any host
  (`serve --host 0.0.0.0 --token …`).
- ✅ A real **pytest** suite (`backend/tests/`); packaging + CI/CD to PyPI,
  Marketplace, Open VSX.

**0.3.0 — Claude-Code-style capabilities (model-agnostic):**
- ✅ **Plan mode** — read-only research → `update_plan` (approve) → execute.
- ✅ **Sub-agents** — `spawn_agent` delegates focused sub-tasks (own context,
  optional cheaper `subagent_model`), depth-bounded.
- ✅ **TODO checklist** — `todo_write`, streamed to CLI + webview.
- ✅ **MCP client** — connect stdio/SSE MCP servers; tools exposed as
  `mcp__server__tool` (optional `[mcp]` extra).
- ✅ **Web tools** — `web_search` (DuckDuckGo / Tavily / Brave / SerpAPI) +
  `web_fetch`.
- ✅ **More tools** — `glob`, `multi_edit` (atomic), background processes,
  `git_status`/`git_diff`/`git_commit`.
- ✅ **`/compact`** — LLM summarization of older turns.

### Roadmap (0.4.0 — Phases 3–4)
- ◻ **Project memory** (`AGENTS.md`/`EURON.md` auto-loaded) and **custom slash
  commands** (`.euron/commands/*.md`).
- ◻ **Hooks** (PreToolUse/PostToolUse/Stop) and a **permissions engine**
  (allow/ask/deny by tool+glob) replacing the coarse auto-approve flags.
- ◻ **Extended thinking** (provider-native where available) surfaced in the UI.
- ◻ **Multimodal image input** to vision models; **cost** estimation (pricing).
- ◻ **Richer webview** (markdown + highlighted diffs, `@file` autocomplete,
  in-panel model picker), and an editor-native diagnostics loop.

### Intentionally out of scope (to stay light & model-agnostic)
- ❌ Anthropic-locked features as hard deps (prompt caching, server-side web
  search, computer-use, citations) — we ship provider-neutral equivalents.
- ❌ Vector-DB / embeddings RAG as a core dependency (retrieval = ripgrep +
  agentic search, like Claude Code).
- ❌ Hosted cloud service / accounts / billing; telemetry; non-VS-Code IDEs.

---

## 10. Extending it

- **Add a tool**: implement it in `tools.py`, register it in `TOOL_FUNCS`, add a
  schema in `tool_schemas.py` (and to `MUTATING_TOOLS` if it writes/executes).
  The loop and both front-ends pick it up automatically.
- **Add a provider**: add a profile to `BUILTIN_PROVIDERS` (config.py) and, if
  it's not OpenAI-compatible, a client in `llm.py` selected by `build_client`.
  For the extension menu, add an entry to `PROVIDERS` in `extension.ts`.
- **Change agent behavior**: edit `prompts.py` (discipline) or `AgentConfig`
  knobs (`max_steps`, auto-approve flags, size/time caps) in `config.py`.
