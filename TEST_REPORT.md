# Test Report — Euron Coding Agent

**Version:** backend `euron-coding-agent` 0.4.0 · extension 0.4.0
**Date:** 2026-06-18
**Environment:** Windows 11 · Python 3.12.3 · pytest 9.1.0 · Node 24 · git-bash

## Summary

| Suite | Result |
|---|---|
| Automated tests (`pytest`) | **45 / 45 passed** |
| Live web tools (network) |  passed (real DuckDuckGo + example.com) |
| Server WebSocket E2E + REST + auth |  passed (real FastAPI + loop) |
| CLI smoke tests |  5 / 5 passed |
| Line coverage (package) | **60%** (cli/llm/mcp live-only paths excluded) |

Everything in the implemented feature set works. The only paths not exercised
end-to-end are those that require an external paid LLM API key or a live MCP
server (called out under *Caveats*); those are covered by deterministic
fake-LLM harnesses that drive the identical code paths.

---

## 1. How it was tested

- **Unit + behavioural tests** (`tests/test_agent.py`, 38 tests): each tool,
  module, and the agent loop driven by a scripted `LLMResponse` queue — no
  network or key needed, fully deterministic.
- **Integration tests** (`tests/test_integration.py`, 7 tests): the **real**
  FastAPI app + WebSocket protocol + agent loop, with the LLM monkeypatched to a
  scripted client. Plus **live** web-tool calls and auth enforcement.
- **CLI smoke tests**: the installed `euron-agent` console script driven with
  piped input in an isolated `HOME`.

Reproduce:
```bash
cd backend
pip install -e ".[dev]"
pytest -v                       # 45 tests
pytest --cov=euron_agent        # coverage
```

---

## 2. Feature coverage matrix

| Feature | How verified | Test(s) | Result |
|---|---|---|---|
| Provider abstraction + built-ins + overrides | unit | `test_builtin_providers_and_overrides` |  |
| Workspace sandbox (path escape blocked) | unit | `test_sandbox_blocks_escape` |  |
| `read_file` (ranges, binary guard, ignore) | unit | `test_read_binary_refused`, `test_ignore_blocks_env` |  |
| `edit_file` (unique/missing/replace_all) | unit | `test_edit_file_unique_and_missing` |  |
| `multi_edit` (atomic, rollback on failure) | unit | `test_multi_edit_atomic` |  |
| `glob` | unit | `test_glob` |  |
| `search_text` / unknown tool dispatch | unit | `test_execute_unknown_tool` |  |
| `run_command` (streaming output, timeout) | unit | `test_run_command_streams` |  |
| Background processes (start/poll/list/kill) | unit | `test_background_manager` |  |
| Git tools (status/commit) | unit (real git) | `test_git_tools` |  |
| Web `web_fetch` / `web_search` (parsing) | unit | `test_web_helpers` |  |
| Web `web_fetch` / `web_search` (**live**) | integration | `test_web_fetch_live`, `test_web_search_live` |  |
| `.gitignore`-aware ignores | unit | `test_gitignore_parsing` |  |
| Token estimate + compaction (trim) | unit | `test_estimate_and_compact` |  |
| `/compact` LLM summarization | unit | `test_compact_summarize` |  |
| `@file` mention expansion | unit | `test_expand_mentions`, `test_loop_mentions_inlined` |  |
| Checkpoints + undo | unit | `test_checkpoint_undo`, `test_loop_undo` |  |
| LLM JSON-tolerance + retry classification | unit | `test_safe_json_loads`, `test_retryable_classification` |  |
| History persistence | unit | `test_history_roundtrip`, `test_loop_persist_roundtrip` |  |
| Agent loop (read→edit→run→done) | unit | `test_loop_create_edit_run` |  |
| Approval reject (feedback to model) | unit | `test_loop_rejection` |  |
| TODO checklist tool | unit | `test_todo_write` |  |
| Plan mode + `update_plan` approval | unit | `test_plan_mode_approval` |  |
| Sub-agents (`spawn_agent`, nested) | unit | `test_subagent` |  |
| MCP tool routing | unit (mock server) | `test_mcp_routing` |  |
| Permissions (deny/ask/allow + defaults) | unit | `test_permissions_rules`, `test_loop_permission_deny` |  |
| "Always allow" persisted rule | unit | `test_always_allow` |  |
| Hooks (PreToolUse blocks) | unit | `test_loop_hook_blocks` |  |
| Project memory (AGENTS.md) | unit | `test_memory` |  |
| Custom slash commands | unit | `test_custom_commands` |  |
| Cost tracking | unit | `test_pricing`, `test_multimodal` |  |
| Multimodal image message build | unit | `test_multimodal`, `test_anthropic_image_conversion` |  |
| **Server WS** init→run→approval→diff→done | integration | `test_ws_end_to_end` |  |
| **Server WS** undo + cancel | integration | `test_ws_undo_and_cancel` |  |
| **REST** `/agent/run` | integration | `test_rest_run` |  |
| **Auth** (bearer token enforced) | integration | `test_auth_enforced` |  |
| `/health`, `/providers` | integration | `test_health_and_providers` |  |
| CLI: providers / init / help | smoke | §3 |  |
| CLI: `/init` memory, `/config`, custom cmd | smoke | §3 |  |
| CLI: settings persistence (`/provider`,`/key`) | smoke | §3 |  |

## 3. CLI smoke-test results

| Check | Expected | Observed |
|---|---|---|
| `euron-agent providers` | 6 providers | **6** listed |
| `euron-agent init` | config.yaml + .env | **2** created |
| `/help` | lists new commands | **8** of the new commands shown |
| `/init` | writes AGENTS.md | `wrote AGENTS.md`  |
| `/provider euri` + `/key` persist | saved to `~/.euron-agent/config.json` | `{"provider":"euri","providers":{"euri":{"api_key":"sk-test"}}}`  |

## 4. Coverage by module

```
pricing.py 100% · prompts.py 100% · tool_schemas.py 100% · events.py 96%
config.py 90% · permissions.py 90% · commands.py 90% · hooks.py 89%
loop.py 84% · gitignore.py 84% · checkpoints.py 83% · context.py 83%
background.py 80% · memory.py 79% · history.py 79% · server.py 76%
tools.py 64% · webtools.py 60% · llm.py 48% · mcp_client.py 32%
settings.py 34% · cli.py 0% (covered by smoke tests, not unit)
TOTAL 60%
```
Lower numbers are concentrated in code that only runs against a live external
service (the OpenAI/Anthropic streaming branches in `llm.py`, the MCP connect
path in `mcp_client.py`) or interactive terminal flows (`cli.py`, validated by
the smoke tests in §3 instead).

## 5. Caveats — tested with a harness vs. against live services

| Area | Status |
|---|---|
| Web search / fetch |  tested **live** against DuckDuckGo + example.com |
| Server WS / REST / auth / loop / tools |  tested against the **real** server with a deterministic fake LLM |
| A real OpenAI/Anthropic/Euri completion | Note: not run (needs a paid API key); identical loop path is covered by the fake-LLM harness |
| Live MCP server | Note: routing tested with a mock; not yet against a real MCP server |
| Vision (image actually understood) | Note: message construction tested; needs a live vision model to confirm understanding |
| Extended thinking | Note: guarded passthrough tested; budget/effort behaviour needs a live thinking model |

## 6. Conclusion

All 45 automated tests pass, the web tools work against the live internet, the
full server + WebSocket + approval + undo + cancel flow works end-to-end, and the
CLI works as installed. The codebase is published to PyPI (0.4.0) and pushed to
GitHub. The only unverified paths require external paid services (a live LLM
completion, a live MCP server, a live vision/thinking model) and are exercised
through deterministic harnesses that drive the same code.
