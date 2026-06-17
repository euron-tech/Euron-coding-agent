# Project Explanation Guide — Euron Coding Agent

A guided, **file-by-file** tour in the order you should read it. For each file you
get: **what's inside**, **what it means**, **architectural talking points** (for a
discussion/presentation), and a **one-line summary**.

> Mental model to keep in your head the whole time:
> **One agent brain (`AgentSession`) that talks to the outside world only through
> an `AgentIO` interface.** The terminal (CLI) and the VS Code extension are just
> two different implementations of that interface. Everything below serves that idea.

**Reading order (touch them top to bottom):**

| # | File | One-line role |
|---|------|---------------|
| 1 | `backend/euron_agent/config.py` | Defines configuration & the list of LLM providers |
| 2 | `backend/euron_agent/events.py` | The event vocabulary + the `AgentIO` contract |
| 3 | `backend/euron_agent/llm.py` | Provider-agnostic LLM client (OpenAI + Anthropic) |
| 4 | `backend/euron_agent/tool_schemas.py` | The list of tools the model is allowed to call |
| 5 | `backend/euron_agent/tools.py` | The actual tool implementations + sandbox + diffs |
| 6 | `backend/euron_agent/prompts.py` | The system prompt (the agent's "operating rules") |
| 7 | `backend/euron_agent/loop.py` | **The agentic loop — the heart of the project** |
| 8 | `backend/euron_agent/settings.py` | Remembers your provider/key between CLI runs |
| 9 | `backend/euron_agent/cli.py` | The terminal front-end (an `AgentIO`) |
| 10 | `backend/euron_agent/server.py` | The web/WebSocket front-end (an `AgentIO`) |
| 11 | `backend/euron_agent/__main__.py` + `__init__.py` | Entry point & version |
| 12 | `backend/pyproject.toml` + `config.example.yaml` + `.env.example` | Packaging & config templates |
| 13 | `extension/src/extension.ts` | The VS Code bridge (backend lifecycle + secrets + UI) |
| 14 | `extension/media/main.js` + `style.css` | The chat webview UI |
| 15 | `extension/package.json` | The VS Code extension manifest |
| 16 | `extension/esbuild.js` + `.vscodeignore` + `tsconfig.json` | Extension build/packaging |
| 17 | `.github/workflows/*` + `PUBLISHING.md` | CI/CD and release |

---

## Phase A — The foundations (the contracts everything else depends on)

### 1. `backend/euron_agent/config.py`

**Read it for:** how the app knows *which LLM to call, with what key, and how the
agent should behave.*

**What's inside**
- `load_dotenv()` at import — pulls a `.env` file into environment variables.
- Three dataclasses that model all configuration:
  - `ProviderConfig` — one LLM endpoint: `type` (`openai`/`anthropic`),
    `base_url`, `api_key_env`, resolved `api_key`, `model`, `temperature`,
    `max_tokens`.
  - `AgentConfig` — behaviour knobs: `max_steps`, `stream`,
    `auto_approve_reads/writes/commands`, `max_file_bytes`, `max_command_seconds`.
  - `Config` — the resolved bundle: the chosen `provider`, the `agent` config,
    the `ignore` globs, and `all_providers`.
- `BUILTIN_PROVIDERS` — ready-made profiles for `euri`, `openai`, `openrouter`,
  `ollama`, `anthropic`, `custom`.
- `load_config(config_path, provider, model, api_key, base_url)` — the one public
  function. It merges: **built-ins ← `config.yaml` ← runtime overrides**, resolves
  each key from its `api_key_env`, picks the active provider, and returns a `Config`.

**What it means / why it exists**
This is the single source of truth for "what model are we talking to and how."
`BUILTIN_PROVIDERS` is the reason the app works with **no config file at all** —
the extension or CLI just names a provider and passes a key. The `api_key`/
`base_url` parameters let a caller (the VS Code extension) inject secrets at
runtime so nothing sensitive has to live on disk.

**Architectural talking points**
- "Layered config with clear precedence" — defaults, then file, then explicit
  overrides. Predictable and testable.
- "Provider-agnostic by data, not by code" — adding OpenRouter/Together/Groq is a
  few lines of YAML, not a code change, because they're all OpenAI-compatible.

**Summary:** Defines and resolves all configuration and the catalogue of LLM
providers into one `Config` object the rest of the app consumes.

---

### 2. `backend/euron_agent/events.py`

**Read it for:** the **language** the agent uses to talk to any UI, and the
**interface** that decouples the brain from the terminal/editor.

**What's inside**
- Small constructor functions that each return a plain dict:
  `status`, `token`, `assistant_message`, `tool_start`, `tool_result`, `diff`,
  `approval_request`, `done`, `error`.
- `ApprovalDecision` dataclass — `approved: bool`, optional `feedback`.
- `AgentIO` (abstract base class) with three methods:
  - `on_token(text)` — receive a streamed chunk (must be thread-safe).
  - `emit(event)` — receive a structured event (async).
  - `request_approval(request) -> ApprovalDecision` — ask a human and block
    until answered (async).

**What it means / why it exists**
This is **the seam of the whole architecture**. The agent loop never imports
`rich` or `WebSocket`; it only calls `AgentIO`. Swap the implementation and the
same brain runs in a terminal, in VS Code, or (later) anywhere else. The event
functions returning dicts means they serialize straight to JSON over a socket.

**Architectural talking points**
- "Ports-and-adapters / dependency inversion" — the core depends on an
  abstraction; the transports depend on the core.
- "One protocol, two renderers" — the CLI and the webview render the *same*
  events, so they can never drift in capability.

**Summary:** Declares the event protocol and the `AgentIO` abstraction that
isolates the agent's logic from how it's displayed.

---

## Phase B — The agent's capabilities (what it can think with and do)

### 3. `backend/euron_agent/llm.py`

**Read it for:** how we call *any* LLM with one uniform interface and get back
normalized text + tool calls.

**What's inside**
- Normalized types: `ToolCall(id, name, arguments)`, `LLMResponse(content,
  tool_calls)`, and `LLMError`.
- `build_client(provider)` — factory returning the right client by `type`.
- `OpenAICompatClient` — wraps the `openai` SDK with a configurable `base_url`,
  so it speaks to **any OpenAI-compatible server** (Euri, OpenAI, OpenRouter,
  Ollama, vLLM, LM Studio…). `_chat_stream` accumulates streamed **text deltas**
  *and* the **partial tool-call fragments** into complete `ToolCall`s.
- `AnthropicClient` — converts our OpenAI-format history into Anthropic's block
  format (`_to_anthropic_messages` pulls the system prompt out and folds tool
  results into user turns; `_to_anthropic_tools` rewrites the schemas), then
  streams and collects `tool_use` blocks.
- `_safe_json_loads` — tolerates the malformed JSON that small/local models
  sometimes emit for tool arguments.

**What it means / why it exists**
The agent keeps its conversation in **one format (OpenAI's)** regardless of
provider. Each client is responsible for translating to/from its own API and
returning a uniform `LLMResponse`. That's what makes the project truly
"plug any model in."

**Architectural talking points**
- "Adapter pattern" — two adapters behind one `build_client` factory.
- "The loop is provider-blind" — all provider quirks are contained here.
- "Streaming is first-class" — tokens flow out as they're generated for a smooth
  UX, while tool calls are reassembled from fragments.

**Summary:** A provider-agnostic LLM layer that turns "call the model" into one
method returning normalized text and tool calls.

---

### 4. `backend/euron_agent/tool_schemas.py`

**Read it for:** the **menu of actions** advertised to the model, and which of
them are dangerous.

**What's inside**
- `MUTATING_TOOLS` — the set that changes the world and therefore needs approval:
  `write_file`, `edit_file`, `create_file`, `delete_file`, `run_command`.
- `_fn(...)` — a tiny helper that builds one OpenAI function schema.
- `TOOL_SCHEMAS` — the full list of tool definitions (name, description,
  JSON-schema parameters) sent to the LLM: `list_files`, `read_file`,
  `search_text`, plus the five mutating ones.

**What it means / why it exists**
This is the **contract between the model and our code**: the descriptions here are
effectively prompt engineering — they tell the model when and how to use each
tool. `MUTATING_TOOLS` is the single place that decides what gets gated behind a
human approval.

**Architectural talking points**
- "Capabilities are declared data" — to add a tool you add a schema; the loop and
  both UIs pick it up automatically.
- "Safety boundary lives here" — read vs. write/execute is one explicit set.

**Summary:** Declares the tools the model may call and flags the ones that require
human approval.

---

### 5. `backend/euron_agent/tools.py`

**Read it for:** the **hands** of the agent — the real file/search/shell
operations, sandboxed, with diffs for review.

**What's inside**
- `ToolOutcome(output, ok, diff, is_new)` — uniform return type for every tool.
- `ToolContext` — holds the workspace root + agent config + ignore globs and
  enforces the **sandbox**: `resolve()` rejects any path that escapes the root;
  `is_ignored()` blocks `.env`, `.git`, `node_modules`, etc.
- Read-only tools: `list_files`, `read_file` (size-capped, optional line range,
  line-numbered output), `search_text` (uses `ripgrep` if installed, else a pure
  Python walk).
- Mutating tools: `write_file`, `edit_file` (**exact, unique search/replace** —
  fails loudly if `old_string` is missing or ambiguous), `create_file`,
  `delete_file`, `run_command` (shell, timeout-bounded).
- `_unified()` builds a unified diff; `_prepare_write()` computes the diff
  *without writing* (for the approval preview); `preview_for()` produces the human
  preview; `execute()` is the dispatch entry point used by the loop.

**What it means / why it exists**
This is where the agent actually affects your machine — so it's also where all the
**safety** lives: path sandboxing, ignore rules, size/time caps, and diffs so a
human can see exactly what will change *before* it happens.

**Architectural talking points**
- "Every dangerous action produces a previewable diff" — approval is informed, not
  blind.
- "Surgical edits over full rewrites" — `edit_file` keeps small models reliable and
  token-cheap.
- "Defense in depth" — sandbox + ignore list + caps + approval gate.

**Summary:** Implements the sandboxed file, search, and shell tools and the diffs
that make changes reviewable.

---

### 6. `backend/euron_agent/prompts.py`

**Read it for:** the agent's **personality and discipline** — the rules that make
even a small model behave well.

**What's inside**
- `system_prompt(workspace_path, file_tree)` — returns the system message: the
  workspace root, a snapshot of the file tree, and a numbered set of operating
  rules (plan first, ground yourself before editing, edit surgically, one step at
  a time, respect rejections, verify, **stop when done**).

**What it means / why it exists**
The loop and tools give the model *ability*; this gives it *judgement*. The
"plan-first, read-before-write, one-step-at-a-time" discipline is the single
biggest lever for quality on small/cheap models.

**Architectural talking points**
- "Behaviour is configurable text" — tuning the agent often means editing this one
  function, not the code.
- "Stop condition is explicit" — 'no more tool calls = done' is taught here and
  enforced in the loop.

**Summary:** The system prompt that defines how the agent plans, edits, and knows
when it's finished.

---

## Phase C — The brain

### 7. `backend/euron_agent/loop.py`  ⭐ the heart

**Read it for:** **how everything above is orchestrated** into an actual agent.

**What's inside**
- `AgentSession(workspace, config, io)` — one conversation. In `__init__` it
  builds the LLM `client`, a `ToolContext`, and an empty `messages` history.
- `run(task)` — seeds the system prompt on the first turn, appends the user task,
  and drives `_agent_loop`, catching/reporting errors.
- `_agent_loop()` — the iteration, up to `config.agent.max_steps`:
  1. Call the LLM off the event loop (`asyncio.to_thread`), streaming tokens via
     `_stream_token → io.on_token`.
  2. **No tool calls → the turn is finished** (`done`, return).
  3. Otherwise record the assistant message + its tool calls and run each one.
- `_handle_tool_call(tc)` — emits `tool_start`; for a `MUTATING_TOOLS` call that
  isn't auto-approved, builds a preview and calls `io.request_approval`. On
  **reject**, feeds the rejection (plus any note) back to the model as the tool
  result so it can adapt; on **approve**, runs `execute()`, emits the `diff` and
  `tool_result`, and appends the result to history.
- `_append_tool_result`, `_auto_approved`, `_ensure_system` — small helpers. The
  history is kept in **OpenAI message format for all providers** (the client
  translates).

**What it means / why it exists**
This is the classic agent loop: **think → act → observe → repeat until done.** It
is deliberately transport-agnostic (only touches `AgentIO`) and provider-agnostic
(only touches `LLMResponse`). Approval gating is woven directly into the act step.

**Architectural talking points**
- "It's a state machine over a message list" — each turn appends assistant/tool
  messages; the model decides the next action from the growing transcript.
- "Human-in-the-loop is structural" — rejection isn't an error, it's feedback the
  model sees and reacts to.
- "Bounded by `max_steps`" — a safety cap against runaway loops.
- "Blocking LLM calls run in a thread" so streaming and approvals stay responsive.

**Summary:** The agentic loop that repeatedly calls the model, executes its tool
calls (with approval), and feeds results back until the task is done.

---

## Phase D — The two front-ends (and CLI memory)

### 8. `backend/euron_agent/settings.py`

**Read it for:** how the CLI **remembers** your provider, model, and API keys
between runs.

**What's inside**
- `SETTINGS_DIR = ~/.euron-agent`, `SETTINGS_FILE = config.json`.
- `load()` / `save()` (saves with user-only permissions where supported).
- `set_active_provider`, `set_provider_field`, `provider_overrides`.

**What it means / why it exists**
Powers the Claude-CLI-style experience: set your key once with `/key` and it
persists, so future runs are just `euron-agent` → type a task.

**Architectural talking points**
- "Settings vs. secrets" — convenient for a CLI; for shared machines, env vars or
  the extension's encrypted SecretStorage are the safer paths.

**Summary:** A tiny JSON store under your home folder that persists CLI provider
and key choices.

---

### 9. `backend/euron_agent/cli.py`

**Read it for:** the **terminal application** — and a concrete example of an
`AgentIO` implementation.

**What's inside**
- `_force_utf8()` + a `rich` `Console` — fixes Windows code-page crashes on
  box-drawing/emoji.
- `TerminalIO(AgentIO)` — renders the event stream in the terminal: streams
  tokens, colors diffs, and prompts `y/n/feedback` for approvals.
- `resolve_config(args)` — merges CLI flags with the persisted user settings into
  a `Config`. `_key_missing()` detects an unconfigured provider; `_reload()`
  rebuilds the live client after a settings change.
- The chat REPL: `_chat()` plus `_handle_command()` for the slash commands
  (`/provider`, `/key`, `/model`, `/baseurl`, `/config`, `/providers`, `/reset`,
  `/yes`, `/help`, `/exit`).
- Sub-commands: `run`, `chat`, `serve`, `providers`, `init` (with embedded config
  templates). `build_parser()` makes the command optional so a bare `euron-agent`
  drops into chat.

**What it means / why it exists**
Proves the architecture: the terminal is "just another `AgentIO`," so the full
agent runs without VS Code. It's also the fastest way to test/iterate on the brain.

**Architectural talking points**
- "Same brain, different skin" — `TerminalIO` and the server's `WebSocketIO` are
  interchangeable.
- "Configuration is in-session" — you set everything from the prompt, like the
  Claude CLI, and it's remembered via `settings.py`.

**Summary:** The terminal front-end and REPL — an `AgentIO` that lets you run and
configure the agent entirely from the command line.

---

### 10. `backend/euron_agent/server.py`

**Read it for:** the **web front-end** the VS Code extension talks to.

**What's inside**
- `app = FastAPI(...)`.
- `WebSocketIO(AgentIO)` — implements the interface over a socket: `on_token`
  hops back onto the event loop with `run_coroutine_threadsafe`;
  `request_approval` parks an `asyncio.Future` that an incoming `approval` message
  resolves.
- `ws_endpoint` — the `/ws` message router: `init` (build session with injected
  key) / `run` (launch the loop as a background task so approvals can arrive
  concurrently) / `approval` / `ping`.
- REST: `POST /agent/run` (one-shot, non-interactive via `BufferIO`),
  `GET /health`, `GET /providers`.
- `_free_port()` + `serve()` — picks a free port and prints
  `EURON_AGENT_LISTENING http://…` so the extension can discover it.

**What it means / why it exists**
This is the boundary between the TypeScript world and the Python brain. The
WebSocket carries the streaming + approval protocol; the dynamic port avoids
collisions when the extension auto-starts the backend.

**Architectural talking points**
- "Streaming + interactive approval over one socket" — the `run`-as-a-task detail
  is what lets the server receive an `approval` while a task is mid-flight.
- "Secrets injected per-connection" — the `init` message carries the key, so the
  server never needs a config file.

**Summary:** A FastAPI server exposing the agent over a streaming WebSocket (plus a
REST fallback) for the VS Code extension.

---

### 11. `backend/euron_agent/__main__.py` & `__init__.py`

**What's inside**
- `__init__.py` — the package version (`__version__`).
- `__main__.py` — makes `python -m euron_agent ...` work by calling `cli.main()`;
  this is exactly how the VS Code extension launches the server.

**Summary:** Tiny glue: the module entry point used by the extension, and the
version marker.

---

## Phase E — Packaging & templates

### 12. `backend/pyproject.toml`, `config.example.yaml`, `.env.example`, `requirements.txt`

**What's inside**
- `pyproject.toml` — packaging metadata: distribution name **`euron-coding-agent`**
  (on PyPI), the import package `euron_agent`, the console script `euron-agent`,
  dependencies, and the optional `anthropic` extra.
- `config.example.yaml` — a fully commented config with every provider example
  (Euri, OpenAI, OpenRouter, Ollama, vLLM, Anthropic).
- `.env.example` — the API-key environment variables to copy into `.env`.

**What it means / why it exists**
This is what turns the code into an installable product: `pip install
euron-coding-agent` gives you the `euron-agent` CLI and the importable backend the
extension provisions.

**Summary:** The packaging manifest and config templates that make the backend a
pip-installable tool.

---

## Phase F — The VS Code extension

### 13. `extension/src/extension.ts`

**Read it for:** the **bridge** — how VS Code starts the Python backend, stores
keys, and relays the chat.

**What's inside**
- `PROVIDERS` + `secretKeyFor` — the provider menu and how keys are namespaced.
- `configureProvider()` — QuickPick a provider, InputBox the key, store the key in
  `context.secrets` (encrypted **SecretStorage**) and the active provider in
  `globalState`.
- `buildInitPayload()` — assembles the `init` message (provider + key + optional
  base_url/model); prompts to set a key if missing.
- `BackendManager` — the lifecycle owner: `detectPython` (≥3.9), `provision`
  (create a private venv in global storage, `pip install euron-coding-agent`,
  re-install on version change), `startManaged` (spawn `python -m euron_agent
  serve --port 0` and read the announced port), `getWsUrl` (or use a
  developer-supplied `serverUrl`).
- `ChatViewProvider` — hosts the webview, renders its HTML (CSP + nonce), and
  relays messages between the webview and the WebSocket.

**What it means / why it exists**
This is the "zero-setup" magic: the user installs an extension and it quietly
stands up a Python backend and manages secrets — no terminal, no `.env`.

**Architectural talking points**
- "The extension is a thin client" — all intelligence stays in Python; TS just
  manages lifecycle, secrets, and message relay.
- "Keys never touch disk in plaintext" — SecretStorage + per-connection injection.
- "Dynamic port + auto-provision" — robust first-run experience.

**Summary:** The VS Code host code that provisions/launches the backend, secures
API keys, and bridges the webview to the agent over WebSocket.

---

### 14. `extension/media/main.js` & `style.css`

**What's inside**
- `main.js` — the webview script: sends the user's task to the host, and renders
  the incoming event stream — user/assistant bubbles, streamed tokens, tool lines,
  colored **diff** blocks, and **approval cards** with Approve/Reject + feedback.
- `style.css` — uses VS Code theme variables so the panel matches light/dark.

**What it means / why it exists**
The visual half of the same event protocol the CLI renders in text. The approval
card is where the human-in-the-loop happens in the editor.

**Summary:** The chat UI that renders streamed output, diffs, and approval prompts
inside VS Code.

---

### 15. `extension/package.json`

**What's inside**
- The **manifest**: `publisher` (`Euron`), the activity-bar view + webview, the
  commands (Open Chat, Select Provider, Set API Key, Restart Backend), the
  toolbar menus, and the user **settings** (`model`, `pythonPath`,
  `backendVersion`, `serverUrl`). Scripts wire `esbuild` and `vsce`.

**What it means / why it exists**
VS Code reads this to know what to show, what commands exist, and what's
configurable. It's also the Marketplace listing's source of truth.

**Summary:** The extension manifest — its identity, UI surface, commands, and
settings.

---

### 16. `extension/esbuild.js`, `.vscodeignore`, `tsconfig.json`

**What's inside**
- `esbuild.js` — bundles `src/extension.ts` (and `ws`) into a single
  `out/extension.js` so the published `.vsix` needs no `node_modules`.
- `.vscodeignore` — excludes source/dev files from the package.
- `tsconfig.json` — TypeScript compiler settings.

**Summary:** The build tooling that turns the TypeScript source into one
shippable bundle.

---

## Phase G — Distribution

### 17. `.github/workflows/release.yml`, `ci.yml`, and `PUBLISHING.md`

**What's inside**
- `ci.yml` — on every push/PR: builds & smoke-tests the backend across Python
  3.9/3.11/3.12 and packages the extension.
- `release.yml` — on a `v*` tag: publishes the backend to **PyPI**, then the
  extension to the **VS Code Marketplace** and **Open VSX**, and attaches the
  `.vsix` to a GitHub release.
- `PUBLISHING.md` — the one-time human setup (accounts, tokens, secrets).

**What it means / why it exists**
This is how a commit becomes something the world can install. Tag a version and
both halves ship automatically.

**Summary:** CI that builds/tests every change and a tagged-release pipeline that
publishes to PyPI, the Marketplace, and Open VSX.

---

## How to give the 60-second version (for a meeting)

1. "It's an agentic coding assistant with the brain in **Python** and a thin **VS
   Code** front-end — and it also runs from the **CLI**."
2. "The core is one **agent loop** (`loop.py`) that thinks, calls **tools**
   (`tools.py`), and repeats — pausing for **approval** on anything that writes."
3. "It's **provider-agnostic** (`llm.py` + `config.py`): Euri, OpenAI, Anthropic,
   Ollama, or any self-hosted OpenAI-compatible model."
4. "The brain talks to the world only through one interface (`events.py`'s
   `AgentIO`), so the **terminal** and the **editor** are just two skins."
5. "It's **packaged and shipped**: the backend on PyPI, the extension on the
   Marketplace, wired through GitHub Actions."

> Want the deeper system view (diagrams, event-protocol table, implemented-vs-not
> list)? That's in `ARCHITECTURE.md`. The user-facing intro is `README.md`.
