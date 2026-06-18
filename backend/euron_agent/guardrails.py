"""Guardrails for autonomous deploy / self-heal.

Two deterministic safety layers used by the deploy + ship + heal flows:

  * spend gate — block anything that may create billable cloud infra unless the user
    opted in (`deploy.allow_billable`), defaulting to free-tier-only. Irreversible /
    money-spending steps stay behind an explicit approval.
  * secret redaction — mask any known credential value (from the env vars our deploy
    targets use) before it could be echoed to the user, logged, or audited.

Graduated trust: a health-gated rollback (reverting to a known-good state) is always
safe to automate; anything that *creates* state or *spends money* is gated.
"""
from __future__ import annotations

import os
import re

from . import deploy

# Env vars that hold deploy/monitoring credentials — their values get redacted.
_CRED_ENVS = [
    "VERCEL_TOKEN", "NETLIFY_AUTH_TOKEN", "CLOUDFLARE_API_TOKEN", "RENDER_API_KEY",
    "RAILWAY_TOKEN", "RAILWAY_API_TOKEN", "FLY_API_TOKEN", "NEON_API_KEY",
    "SUPABASE_ACCESS_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AZURE_CLIENT_SECRET", "DOCKER_TOKEN", "DOCKER_PASSWORD",
    "SENTRY_AUTH_TOKEN", "SENTRY_DSN", "DD_API_KEY", "DD_APP_KEY",
    "UPTIMEROBOT_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
]


def spend_gate(deploy_cfg: dict, target_name: str) -> tuple[str, str]:
    """Return (decision, reason). decision is 'allow' | 'ask' | 'deny'.

    Free-tier targets → allow. Billable targets → 'ask' (approval) unless the user
    set deploy.allow_billable: true, and 'deny' if deploy.free_tier_only is set.
    """
    dep = deploy_cfg or {}
    t = deploy.target(target_name)
    if t is None:
        return "deny", f"unknown deploy target '{target_name}'"
    if not t.billable and t.free_tier:
        return "allow", "free-tier target"
    # Billable / no-free-tier target.
    if dep.get("free_tier_only", True) and not dep.get("allow_billable", False):
        return "deny", (f"{target_name} may create billable infrastructure; blocked by "
                        "deploy.free_tier_only (set deploy.allow_billable: true to permit)")
    if dep.get("allow_billable", False):
        return "allow", "billable allowed by deploy.allow_billable"
    return "ask", f"{target_name} may incur cost — confirm before provisioning"


def cost_ceiling(deploy_cfg: dict) -> float | None:
    v = (deploy_cfg or {}).get("cost_ceiling_usd")
    return float(v) if v is not None else None


def redact(text: str) -> str:
    """Mask known credential values and common secret token shapes in `text`."""
    if not text:
        return text
    out = text
    for env in _CRED_ENVS:
        val = os.getenv(env)
        if val and len(val) >= 6:
            out = out.replace(val, f"$***{env}***")
    # Generic token shapes (in case a value isn't from a known env var).
    out = re.sub(r"\b(pypi-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                 r"sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b", "***redacted***", out)
    return out
