"""Runtime monitoring & observability wiring.

The missing pillar in the field: once an app is deployed, give it eyes. This module
helps the agent (1) instrument the app with error tracking + telemetry, (2) make sure
a health endpoint exists, (3) register an uptime check, and (4) report current status
back into the terminal. Everything is credential-by-env-var and network calls are
best-effort (skipped cleanly when a token isn't set).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Health-endpoint snippets the agent can drop into a project that lacks one.
HEALTH_SNIPPETS = {
    "fastapi": (
        '@app.get("/health")\n'
        'def health():\n'
        '    return {"status": "ok"}\n'),
    "flask": (
        '@app.get("/health")\n'
        'def health():\n'
        '    return {"status": "ok"}, 200\n'),
    "express": (
        "app.get('/health', (req, res) => res.json({ status: 'ok' }));\n"),
    "next": (
        "// app/health/route.ts\n"
        "export function GET() { return Response.json({ status: 'ok' }); }\n"),
}


def instrument_env() -> dict[str, str]:
    """Env vars to inject into the deployed service for monitoring, based on what the
    user has configured locally. Only includes a var if its source is present."""
    out: dict[str, str] = {}
    if os.getenv("SENTRY_DSN"):
        out["SENTRY_DSN"] = os.environ["SENTRY_DSN"]
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        out["OTEL_EXPORTER_OTLP_ENDPOINT"] = os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]
        if os.getenv("OTEL_EXPORTER_OTLP_HEADERS"):
            out["OTEL_EXPORTER_OTLP_HEADERS"] = os.environ["OTEL_EXPORTER_OTLP_HEADERS"]
    return out


def configured_providers() -> dict[str, bool]:
    return {
        "sentry": bool(os.getenv("SENTRY_DSN") or os.getenv("SENTRY_AUTH_TOKEN")),
        "opentelemetry": bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")),
        "datadog": bool(os.getenv("DD_API_KEY")),
        "uptimerobot": bool(os.getenv("UPTIMEROBOT_API_KEY")),
        "betterstack": bool(os.getenv("BETTERSTACK_TOKEN")),
    }


def register_uptime_command(url: str) -> tuple[bool, str]:
    """Build a headless command to register an uptime monitor for `url`."""
    if os.getenv("UPTIMEROBOT_API_KEY"):
        return True, (
            'curl -s -X POST https://api.uptimerobot.com/v3/monitors '
            '-H "Authorization: Bearer $UPTIMEROBOT_API_KEY" '
            '-H "Content-Type: application/json" '
            f'-d \'{{"type":"http","url":"{url}","interval":300,"friendlyName":"euron"}}\'')
    if os.getenv("BETTERSTACK_TOKEN"):
        return True, (
            'curl -s -X POST https://uptime.betterstack.com/api/v2/monitors '
            '-H "Authorization: Bearer $BETTERSTACK_TOKEN" '
            '-H "Content-Type: application/json" '
            f'-d \'{{"monitor_type":"status","url":"{url}"}}\'')
    return False, ("No uptime provider configured. Set UPTIMEROBOT_API_KEY or "
                   "BETTERSTACK_TOKEN to auto-register a check.")


def _sentry_unresolved() -> tuple[int, list[str]] | None:
    """Best-effort: count unresolved Sentry issues. Returns None if not configured
    or the call fails (never raises)."""
    token = os.getenv("SENTRY_AUTH_TOKEN")
    org = os.getenv("SENTRY_ORG")
    project = os.getenv("SENTRY_PROJECT")
    if not (token and org and project):
        return None
    url = (f"https://sentry.io/api/0/projects/{org}/{project}/issues/"
           "?query=is:unresolved&statsPeriod=24h")
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            data = json.loads(r.read().decode("utf-8"))
        titles = [i.get("title", "?") for i in data[:5]]
        return len(data), titles
    except (urllib.error.URLError, ValueError, TimeoutError, Exception):  # noqa: BLE001
        return None


def status() -> str:
    """Human-readable monitoring status for the `monitor` command."""
    prov = configured_providers()
    lines = ["Monitoring status", ""]
    for name, on in prov.items():
        lines.append(f"  {'✔' if on else '○'} {name}: {'configured' if on else 'not set'}")
    sentry = _sentry_unresolved()
    if sentry is not None:
        n, titles = sentry
        lines += ["", f"Sentry: {n} unresolved issue(s) in the last 24h:"]
        lines += [f"  - {t}" for t in titles] or ["  (none)"]
    elif prov["sentry"]:
        lines += ["", "Sentry: set SENTRY_AUTH_TOKEN + SENTRY_ORG + SENTRY_PROJECT to "
                  "pull live issues."]
    if not any(prov.values()):
        lines += ["", "No monitoring configured yet. `euron-agent ship` wires Sentry/OTel "
                  "+ an uptime check automatically when their tokens are present."]
    return "\n".join(lines)
