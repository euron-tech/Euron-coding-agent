"""Auto-deployment — one sentence to a live URL, across many providers.

A registry of deploy *targets*, each described declaratively: the CLI it needs, the
env var(s) that hold its credential, the headless deploy/rollback commands, the
config file(s) it expects, and whether it has a real free tier. The agent (or the
`deploy` / `ship` commands) uses this to:

  * `readiness()`  — which targets are usable right now (CLI installed + token set),
  * `suggest()`    — pick a sensible default target for a detected stack,
  * `deploy_command()` / `rollback_command()` — build the exact shell command,
  * `config_template()` — scaffold the platform config file.

Nothing here runs a command itself — building the command is separated from running
it so the loop can route execution through the normal approval + sandbox + audit
gates. Credentials are referenced by env-var name only and never read into strings.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

# kinds: edge | static | paas | container | cloud | db
@dataclass
class Target:
    name: str
    kind: str
    cli: str                      # executable that must be installed
    token_envs: list[str]         # env vars holding the credential (any present = ok)
    deploy_cmd: str               # headless, non-interactive
    rollback_cmd: str = ""        # "" = no first-class rollback
    config_files: list[str] = field(default_factory=list)
    free_tier: bool = True
    billable: bool = False        # may create billable infra even on free plans
    note: str = ""
    health_supported: bool = True


# Ordered roughly by agent-friendliness (one-command source->URL first).
TARGETS: dict[str, Target] = {
    # --- Edge / serverless (true one-command, perpetual free tier) ----------- #
    "cloudrun": Target(
        "cloudrun", "cloud", "gcloud", ["GOOGLE_APPLICATION_CREDENTIALS"],
        "gcloud run deploy {service} --source . --region {region} --quiet "
        "--allow-unauthenticated --format json",
        "gcloud run services update-traffic {service} --to-revisions {revision}=100 "
        "--region {region} --quiet",
        ["Dockerfile (optional — buildpacks used otherwise)"],
        free_tier=True, note="Always-free 2M req/mo; source->URL in one command."),
    "cloudflare": Target(
        "cloudflare", "edge", "wrangler", ["CLOUDFLARE_API_TOKEN"],
        "npx wrangler deploy",
        "npx wrangler rollback",
        ["wrangler.toml"],
        free_tier=True, note="100k req/day free, no cold start, commercial OK."),
    "cfpages": Target(
        "cfpages", "static", "wrangler", ["CLOUDFLARE_API_TOKEN"],
        "npx wrangler pages deploy {dist}",
        "", ["wrangler.toml (optional)"],
        free_tier=True, note="Static/JAMstack on Cloudflare Pages."),
    # --- PaaS / static (low friction) --------------------------------------- #
    "vercel": Target(
        "vercel", "paas", "vercel", ["VERCEL_TOKEN"],
        "vercel deploy --prod --yes --token $VERCEL_TOKEN",
        "vercel rollback {deployment} --yes --token $VERCEL_TOKEN",
        ["vercel.json"],
        free_tier=True, note="Hobby tier is non-commercial; great for Next.js/front-end."),
    "netlify": Target(
        "netlify", "static", "netlify", ["NETLIFY_AUTH_TOKEN"],
        "netlify deploy --prod --dir {dist}",
        "", ["netlify.toml"],
        free_tier=True, note="Generous free static hosting."),
    "fly": Target(
        "fly", "container", "fly", ["FLY_API_TOKEN"],
        "fly deploy --remote-only",
        "fly releases rollback {version}",
        ["fly.toml"],
        free_tier=False, billable=True, note="No free tier (trial only); needs a card."),
    "railway": Target(
        "railway", "paas", "railway", ["RAILWAY_TOKEN", "RAILWAY_API_TOKEN"],
        "railway up --ci",
        "", ["railway.json"],
        free_tier=False, billable=True, note="Trial credit only (~$5)."),
    "render": Target(
        "render", "paas", "render", ["RENDER_API_KEY"],
        "render deploys create {service} --confirm --wait --output json",
        "render rollback {service} --confirm",
        ["render.yaml"],
        free_tier=True, note="Free web service spins down when idle. Deploy hooks also work."),
    # --- Containers / orchestration ----------------------------------------- #
    "docker": Target(
        "docker", "container", "docker", ["DOCKER_TOKEN", "DOCKER_PASSWORD"],
        "docker buildx build --platform linux/amd64 -t {image} --push .",
        "", ["Dockerfile", ".dockerignore"],
        free_tier=True, note="Builds & pushes an image to a registry; pair with a runtime."),
    "helm": Target(
        "helm", "container", "helm", ["KUBECONFIG"],
        "helm upgrade --install {release} {chart} -f values.yaml --atomic --timeout 5m",
        "helm rollback {release} {revision}",
        ["Chart.yaml", "values.yaml"],
        free_tier=True, billable=True,
        note="--atomic auto-rolls-back on failed deploy (self-healing primitive)."),
    "kubernetes": Target(
        "kubernetes", "container", "kubectl", ["KUBECONFIG"],
        "kubectl apply -f {manifest}",
        "kubectl rollout undo deployment/{deployment}",
        ["*.yaml"],
        free_tier=True, billable=True, note="Cluster costs apply."),
    # --- Big cloud ---------------------------------------------------------- #
    "aws-sam": Target(
        "aws-sam", "cloud", "sam", ["AWS_ACCESS_KEY_ID"],
        "sam deploy --no-confirm-changeset --resolve-s3 --capabilities CAPABILITY_IAM",
        "", ["template.yaml", "samconfig.toml"],
        free_tier=True, billable=True, note="Smoothest AWS path after first --guided run."),
    "aws-apprunner": Target(
        "aws-apprunner", "cloud", "aws", ["AWS_ACCESS_KEY_ID"],
        "aws apprunner create-service --cli-input-json file://apprunner.json",
        "", ["apprunner.json"],
        free_tier=False, billable=True, note="Consumption-billed container service."),
    "azure-aca": Target(
        "azure-aca", "cloud", "az", ["AZURE_CLIENT_ID"],
        "az containerapp up --name {service} --source .",
        "", ["app.yaml (optional)"],
        free_tier=True, billable=True, note="Always-free 2M req/mo; source->URL."),
    # --- Databases (provisioned before the app) ----------------------------- #
    "neon": Target(
        "neon", "db", "neonctl", ["NEON_API_KEY"],
        "neonctl projects create --name {name} --output json",
        "", [], free_tier=True,
        note="Serverless Postgres, instant branches, generous free tier."),
    "supabase": Target(
        "supabase", "db", "supabase", ["SUPABASE_ACCESS_TOKEN"],
        "supabase db push",
        "", ["supabase/config.toml"], free_tier=True,
        note="Full backend: Postgres + auth + storage + edge functions."),
}

# Default target preference order for an autodetected app (free + one-command first).
_DEFAULT_ORDER = ["cloudrun", "cloudflare", "vercel", "netlify", "render", "fly",
                  "railway", "azure-aca", "aws-sam", "helm"]


def target(name: str) -> Target | None:
    return TARGETS.get(name)


def has_token(t: Target) -> bool:
    return any(os.getenv(e) for e in t.token_envs)


def cli_installed(t: Target) -> bool:
    return shutil.which(t.cli) is not None


def readiness() -> list[dict]:
    """One row per target: is the CLI installed, is a token set, is it usable now."""
    rows = []
    for t in TARGETS.values():
        rows.append({
            "name": t.name, "kind": t.kind, "cli": t.cli,
            "cli_ok": cli_installed(t),
            "token_env": " or ".join(t.token_envs),
            "token_ok": has_token(t),
            "ready": cli_installed(t) and has_token(t),
            "free_tier": t.free_tier, "billable": t.billable, "note": t.note,
        })
    return rows


def suggest(detected_stack: str = "", prefer_ready: bool = True) -> str | None:
    """Pick a default deploy target. Prefer one that's ready (CLI+token), else the
    most agent-friendly free option for the order list."""
    order = list(_DEFAULT_ORDER)
    s = (detected_stack or "").lower()
    if any(k in s for k in ("next", "react", "vite", "static", "astro", "vue")):
        order = ["vercel", "netlify", "cloudflare", "cfpages"] + order
    if prefer_ready:
        for name in order:
            t = TARGETS.get(name)
            if t and cli_installed(t) and has_token(t):
                return name
    for name in order:
        if name in TARGETS:
            return name
    return None


def _fill(cmd: str, params: dict) -> str:
    out = cmd
    for k, v in (params or {}).items():
        out = out.replace("{" + k + "}", str(v))
    return out


def deploy_command(name: str, params: dict | None = None) -> tuple[bool, str]:
    """Return (ok, command-or-error). Does NOT run it."""
    t = TARGETS.get(name)
    if not t:
        return False, f"Unknown deploy target '{name}'. Known: {', '.join(TARGETS)}"
    if not cli_installed(t):
        return False, f"'{t.cli}' CLI not installed for target {name}."
    if not has_token(t):
        return False, (f"No credential for {name}: set one of "
                       f"{', '.join(t.token_envs)} in the environment.")
    return True, _fill(t.deploy_cmd, params or {})


def rollback_command(name: str, params: dict | None = None) -> tuple[bool, str]:
    t = TARGETS.get(name)
    if not t:
        return False, f"Unknown target '{name}'."
    if not t.rollback_cmd:
        return False, f"{name} has no first-class rollback command."
    return True, _fill(t.rollback_cmd, params or {})


# ---- config-file scaffolds (minimal, safe starting points) ----------------- #
_CONFIGS = {
    "fly.toml": (
        'app = "{app}"\nprimary_region = "{region}"\n\n'
        '[build]\n\n[http_service]\n  internal_port = {port}\n  force_https = true\n'
        '  auto_stop_machines = true\n  auto_start_machines = true\n  min_machines_running = 0\n'),
    "render.yaml": (
        "services:\n  - type: web\n    name: {app}\n    runtime: docker\n"
        "    plan: free\n    healthCheckPath: /health\n"),
    "vercel.json": '{\n  "version": 2\n}\n',
    "wrangler.toml": (
        'name = "{app}"\nmain = "{entry}"\ncompatibility_date = "2024-01-01"\n'),
    "Dockerfile.python": (
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\n"
        "EXPOSE {port}\nCMD [\"python\", \"{entry}\"]\n"),
    "Dockerfile.node": (
        "FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci --omit=dev\n"
        "COPY . .\nEXPOSE {port}\nCMD [\"node\", \"{entry}\"]\n"),
}


def config_template(kind: str, params: dict | None = None) -> str | None:
    tpl = _CONFIGS.get(kind)
    if tpl is None:
        return None
    p = {"app": "my-app", "region": "iad", "port": 8080, "entry": "main.py"}
    p.update(params or {})
    return _fill(tpl, p)
