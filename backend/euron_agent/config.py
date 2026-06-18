"""Configuration loading and resolution.

Config is layered, in order of precedence (later wins):
  1. built-in defaults
  2. config.yaml (next to the package, the cwd, or $EURON_AGENT_CONFIG)
  3. environment variables (.env is loaded automatically)
  4. explicit overrides passed on the CLI / API call
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()  # pull .env into os.environ if present


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class ProviderConfig:
    name: str
    type: str = "openai"  # "openai" (OpenAI-compatible) or "anthropic"
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key: Optional[str] = None  # resolved from api_key_env at load time
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: int = 4096
    extra_headers: dict = field(default_factory=dict)


@dataclass
class AgentConfig:
    max_steps: int = 30
    stream: bool = True
    auto_approve_reads: bool = True
    auto_approve_writes: bool = False
    auto_approve_commands: bool = False
    max_file_bytes: int = 120_000
    max_command_seconds: int = 60
    # context-window management
    max_context_tokens: int = 24_000  # compact history above this estimate
    compact_history: bool = True
    # resilience
    retry_attempts: int = 3
    retry_backoff: float = 1.5
    # ignore .gitignore-listed paths in addition to the configured globs
    use_gitignore: bool = True
    # auto-scaffold a .euron/ wrapper (memory + skill + project doc) on first run
    auto_onboard: bool = True
    # tamper-evident append-only audit log of tool actions (.euron/audit/)
    audit_log: bool = True
    # post-edit verifier (critic) sub-agent that reviews the turn's diff
    verify_edits: bool = False
    # self-heal: auto-retry a failed test/command via a fix-it sub-agent (N times)
    self_heal: int = 0
    # nested sub-agent budgets (agent-of-agent guardrails)
    subagent_max_calls: int = 12
    subagent_token_budget: int = 400_000
    # extended thinking / reasoning (best-effort, provider-dependent)
    thinking: bool = False
    reasoning_effort: Optional[str] = None  # "low" | "medium" | "high"
    # try these models (same provider) in order if the primary call keeps failing
    fallback_models: list = field(default_factory=list)


@dataclass
class Config:
    provider: ProviderConfig
    agent: AgentConfig
    ignore: list[str] = field(default_factory=list)
    all_providers: dict[str, ProviderConfig] = field(default_factory=dict)
    mcp_servers: dict = field(default_factory=dict)
    web: dict = field(default_factory=dict)
    subagent_model: Optional[str] = None
    permissions: dict = field(default_factory=dict)
    hooks: dict = field(default_factory=dict)
    notifications: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)  # model -> {input, output} per 1M tokens
    # model routing: {"cheap": "<model>", "heavy": "<model>"} — cheap is used for
    # sub-agents / verifier / self-heal when set (auto-downshift).
    router: dict = field(default_factory=dict)
    # multi-model: named model roles, each may target a different provider, e.g.
    # {"planner": {"provider": "anthropic", "model": "claude-opus-4-8"}, ...}
    models: dict = field(default_factory=dict)
    # phase -> role mapping + strategy: {"strategy": "auto", "plan": "planner",
    # "execute": "executor", "subagent": "cheap", "escalate": "planner", ...}
    routing: dict = field(default_factory=dict)
    # run_command sandbox/egress policy: deny_commands (regex), allow_commands
    # (allowlist; if non-empty everything else is denied), block_network (bool).
    sandbox: dict = field(default_factory=dict)
    # auto-deploy policy: {"default_target": "cloudrun", "free_tier_only": true,
    # "allow_billable": false, "cost_ceiling_usd": 0}
    deploy: dict = field(default_factory=dict)
    # monitoring providers / endpoints (mostly read from env; see monitor.py)
    monitoring: dict = field(default_factory=dict)
    # self-heal policy: {"strategy": "rollback-auto-fix-pr", "auto_merge_low_risk": false}
    selfheal: dict = field(default_factory=dict)


DEFAULT_IGNORE = [
    ".git/**", "node_modules/**", "__pycache__/**", ".venv/**", "venv/**",
    "dist/**", "build/**", "*.lock", ".env", ".env.*",
]

# Built-in provider profiles so the extension (and a fresh install with no
# config.yaml) works out of the box: the user just picks a provider and supplies
# a key. Any of these can be overridden by a user-defined provider of the same
# name in config.yaml.
BUILTIN_PROVIDERS: dict[str, dict] = {
    "euri": {
        "type": "openai",
        "base_url": "https://api.euron.one/api/v1",
        "api_key_env": "EURI_API_KEY",
        "model": "gpt-4.1-mini",
    },
    "openai": {
        "type": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
    "openrouter": {
        "type": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "openai/gpt-4o-mini",
    },
    "ollama": {
        "type": "openai",
        "base_url": "http://localhost:11434/v1",
        "api_key_env": None,
        "model": "qwen2.5-coder:7b",
    },
    "anthropic": {
        "type": "anthropic",
        "base_url": None,
        "api_key_env": "ANTHROPIC_API_KEY",
        "model": "claude-sonnet-4-6",
    },
    "gemini": {
        "type": "openai",  # Google's OpenAI-compatible endpoint
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "model": "gemini-2.0-flash",
    },
    "groq": {
        "type": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
    },
    "cerebras": {
        "type": "openai",
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "model": "llama-3.3-70b",
    },
    "deepseek": {
        "type": "openai",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
    },
    "together": {
        "type": "openai",
        "base_url": "https://api.together.xyz/v1",
        "api_key_env": "TOGETHER_API_KEY",
        "model": "moonshotai/Kimi-K2-Instruct",
    },
    "mistral": {
        "type": "openai",
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
        "model": "mistral-large-latest",
    },
    "xai": {
        "type": "openai",
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "model": "grok-2-latest",
    },
    "vercel": {
        "type": "openai",  # Vercel AI Gateway
        "base_url": "https://ai-gateway.vercel.sh/v1",
        "api_key_env": "AI_GATEWAY_API_KEY",
        "model": "anthropic/claude-sonnet-4.5",
    },
    "lmstudio": {
        "type": "openai",
        "base_url": "http://localhost:1234/v1",
        "api_key_env": None,
        "model": "local-model",
    },
    # Generic OpenAI-compatible endpoint; base_url/model supplied at runtime.
    # Covers AWS Bedrock / Azure / GCP Vertex via an OpenAI-compatible proxy.
    "custom": {
        "type": "openai",
        "base_url": "http://localhost:8001/v1",
        "api_key_env": None,
        "model": "local-model",
    },
}


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _candidate_paths(explicit: Optional[str]) -> list[Path]:
    paths = []
    if explicit:
        paths.append(Path(explicit))
    env = os.getenv("EURON_AGENT_CONFIG")
    if env:
        paths.append(Path(env))
    paths.append(Path.cwd() / "config.yaml")
    paths.append(Path(__file__).resolve().parent.parent / "config.yaml")
    return paths


def _find_config_file(explicit: Optional[str]) -> Optional[Path]:
    for p in _candidate_paths(explicit):
        if p and p.is_file():
            return p
    return None


def _provider_from_dict(name: str, d: dict) -> ProviderConfig:
    api_key_env = d.get("api_key_env")
    api_key = os.getenv(api_key_env) if api_key_env else None
    return ProviderConfig(
        name=name,
        type=d.get("type", "openai"),
        base_url=d.get("base_url"),
        api_key_env=api_key_env,
        api_key=api_key,
        model=d.get("model", "gpt-4o-mini"),
        temperature=float(d.get("temperature", 0.2)),
        max_tokens=int(d.get("max_tokens", 4096)),
        extra_headers=d.get("extra_headers", {}) or {},
    )


def load_config(
    config_path: Optional[str] = None,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Config:
    """Load configuration, applying optional overrides.

    `api_key` / `base_url` let a caller (e.g. the VS Code extension) inject a
    secret and endpoint at runtime so nothing has to live in a file.
    """
    raw: dict[str, Any] = {}
    cfg_file = _find_config_file(config_path)
    if cfg_file:
        raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}

    # Start from the built-in profiles, then let config.yaml override/extend them.
    merged: dict[str, dict] = {k: dict(v) for k, v in BUILTIN_PROVIDERS.items()}
    for name, d in (raw.get("providers", {}) or {}).items():
        merged[name] = {**merged.get(name, {}), **d}
    all_providers = {name: _provider_from_dict(name, d) for name, d in merged.items()}

    active = provider or raw.get("active") or "openai"
    if active not in all_providers:
        raise ValueError(
            f"Provider '{active}' not found. Available: {', '.join(all_providers)}"
        )
    selected = all_providers[active]
    overrides: dict[str, Any] = {}
    if model:
        overrides["model"] = model
    if api_key:  # non-empty only
        overrides["api_key"] = api_key
    if base_url:
        overrides["base_url"] = base_url
    if overrides:
        selected = replace(selected, **overrides)

    agent_raw = raw.get("agent", {}) or {}
    agent = AgentConfig(
        max_steps=int(agent_raw.get("max_steps", 30)),
        stream=bool(agent_raw.get("stream", True)),
        auto_approve_reads=bool(agent_raw.get("auto_approve_reads", True)),
        auto_approve_writes=bool(agent_raw.get("auto_approve_writes", False)),
        auto_approve_commands=bool(agent_raw.get("auto_approve_commands", False)),
        max_file_bytes=int(agent_raw.get("max_file_bytes", 120_000)),
        max_command_seconds=int(agent_raw.get("max_command_seconds", 60)),
        max_context_tokens=int(agent_raw.get("max_context_tokens", 24_000)),
        compact_history=bool(agent_raw.get("compact_history", True)),
        retry_attempts=int(agent_raw.get("retry_attempts", 3)),
        retry_backoff=float(agent_raw.get("retry_backoff", 1.5)),
        use_gitignore=bool(agent_raw.get("use_gitignore", True)),
        auto_onboard=bool(agent_raw.get("auto_onboard", True)),
        audit_log=bool(agent_raw.get("audit_log", True)),
        verify_edits=bool(agent_raw.get("verify_edits", False)),
        self_heal=int(agent_raw.get("self_heal", 0)),
        subagent_max_calls=int(agent_raw.get("subagent_max_calls", 12)),
        subagent_token_budget=int(agent_raw.get("subagent_token_budget", 400_000)),
        thinking=bool(agent_raw.get("thinking", False)),
        reasoning_effort=agent_raw.get("reasoning_effort"),
        fallback_models=list(agent_raw.get("fallback_models", []) or []),
    )

    ignore = raw.get("ignore") or DEFAULT_IGNORE

    mcp_servers = (raw.get("mcp", {}) or {}).get("servers", {}) or {}

    web = dict(raw.get("web", {}) or {})
    if not web.get("provider"):
        # auto-detect a search backend from env (TAVILY/BRAVE/SERPAPI), else keyless
        for env_key, name in (
            ("TAVILY_API_KEY", "tavily"),
            ("BRAVE_API_KEY", "brave"),
            ("SERPAPI_API_KEY", "serpapi"),
        ):
            if os.getenv(env_key):
                web = {"provider": name, "api_key": os.environ[env_key]}
                break
        else:
            web = {"provider": "duckduckgo", "api_key": ""}
    elif web.get("api_key_env"):
        web["api_key"] = os.getenv(web["api_key_env"], "")

    return Config(
        provider=selected,
        agent=agent,
        ignore=list(ignore),
        all_providers=all_providers,
        mcp_servers=mcp_servers,
        web=web,
        subagent_model=raw.get("subagent_model"),
        permissions=raw.get("permissions", {}) or {},
        hooks=raw.get("hooks", {}) or {},
        notifications=raw.get("notifications", {}) or {},
        pricing=raw.get("pricing", {}) or {},
        router=raw.get("router", {}) or {},
        models=raw.get("models", {}) or {},
        routing=raw.get("routing", {}) or {},
        sandbox=raw.get("sandbox", {}) or {},
        deploy=raw.get("deploy", {}) or {},
        monitoring=raw.get("monitoring", {}) or {},
        selfheal=raw.get("selfheal", {}) or {},
    )
