# Auto-deploy, monitoring & self-healing — the full loop

Most coding agents stop at a pull request. Euron Coding Agent closes the whole loop
from one English sentence: **build → test → security-scan → deploy to a live URL →
monitor → self-heal** — on infrastructure *you* control, with safety gates at every
risky step. No agent on the market ships first-class deploy + production monitoring;
this is that.

## TL;DR

```bash
# One sentence, end to end:
euron-agent ship "build a FastAPI todo API with a Neon database and put it live"

# Just deploy what's already here:
euron-agent deploy "to cloudrun"
euron-agent deploy --check          # which targets are ready (CLI + token)?

# After it's live:
euron-agent monitor                 # errors / uptime / providers
euron-agent heal https://my-app.run.app   # health-check → auto-rollback → error→PR
```
…or `/ship`, `/deploy`, `/deploys`, `/monitor`, `/heal` inside a chat session.

## What `ship` does (the pipeline)

1. **Plan** — restate the goal, detect the stack, pick a deploy target (`deploy_check`) and a DB if needed.
2. **Build** — make it run; generate any missing config (Dockerfile, `fly.toml`, `wrangler.toml`, …) and a `/health` endpoint.
3. **Test** — write and run tests, fix failures.
4. **Security** — `secret_scan` + `dependency_audit`; fix High/Critical before shipping.
5. **Deploy** — provision a DB if needed, then deploy (free-tier preferred; billable needs approval); capture the URL.
6. **Monitor** — ensure `/health`, wire Sentry/OpenTelemetry/uptime when their tokens are present, report status.

Every step is visible and the risky ones pause for approval.

## Deploy targets

All headless (a CLI + a credential env var). Run `euron-agent deploy --check` to see which are ready right now.

| Target | Kind | CLI | Credential env | Free tier |
|---|---|---|---|---|
| **cloudrun** | cloud | `gcloud` | `GOOGLE_APPLICATION_CREDENTIALS` | ✅ always-free (default) |
| **cloudflare** / **cfpages** | edge/static | `wrangler` | `CLOUDFLARE_API_TOKEN` | ✅ generous |
| **vercel** | paas | `vercel` | `VERCEL_TOKEN` | ✅ (non-commercial) |
| **netlify** | static | `netlify` | `NETLIFY_AUTH_TOKEN` | ✅ |
| **render** | paas | `render` | `RENDER_API_KEY` | ✅ (idles) |
| **fly** | container | `fly` | `FLY_API_TOKEN` | ⚠ billable |
| **railway** | paas | `railway` | `RAILWAY_TOKEN` | ⚠ trial credit |
| **docker** | container | `docker` | `DOCKER_TOKEN` | ✅ (image push) |
| **helm** / **kubernetes** | container | `helm`/`kubectl` | `KUBECONFIG` | ⚠ cluster cost |
| **aws-sam** / **aws-apprunner** | cloud | `sam`/`aws` | `AWS_ACCESS_KEY_ID` | ⚠ billable |
| **azure-aca** | cloud | `az` | `AZURE_CLIENT_ID` | ✅ always-free tier |
| **neon** (DB) | db | `neonctl` | `NEON_API_KEY` | ✅ |
| **supabase** (DB) | db | `supabase` | `SUPABASE_ACCESS_TOKEN` | ✅ |

## Safety: the spend gate & secrets

- **Free-tier by default.** Free targets deploy directly; **billable targets are
  blocked** unless you opt in. Configure in `config.yaml`:
  ```yaml
  deploy:
    default_target: cloudrun
    free_tier_only: true      # block anything that may cost money (default)
    allow_billable: false     # set true to permit fly/railway/aws/k8s
    cost_ceiling_usd: 0
  ```
  `free_tier_only: false` (without `allow_billable`) makes billable targets **ask**
  for approval instead of running.
- **Secrets are read from env vars only** and never printed; deploy commands
  reference `$VERCEL_TOKEN`-style variables, and any credential value is redacted
  from output/audit. Every deploy/rollback is recorded in the tamper-evident
  [audit log](COMMANDS.md).

## Self-healing (`heal`) — policy: rollback auto, fixes as PRs

- **Fast loop (autonomous, safe):** `health_check` the live URL; if it's unhealthy,
  **auto-rollback** to the last known-good release (`vercel rollback`,
  `helm rollback`, `kubectl rollout undo`, Cloud Run traffic shift, …). Rolling back
  only restores a prior good state, so it never needs to ask.
- **Slow loop (generative, gated):** pull current production errors (`monitor_status`
  → Sentry/Datadog/uptime), find the root cause, write a fix **plus a regression
  test**, and **open a PR** — never auto-merged or auto-deployed. A human reviews and
  merges; the merge re-enters the pipeline.

Run it on demand (`euron-agent heal <url>`) or on a schedule via the existing
`euron-agent schedule` cron system to get continuous monitoring + remediation.

## Monitoring

`euron-agent monitor` reports which providers are configured and pulls live status.
Set any of these and `ship`/`monitor` will use them automatically:

- **Sentry** — `SENTRY_DSN` (instrument) + `SENTRY_AUTH_TOKEN` + `SENTRY_ORG` + `SENTRY_PROJECT` (read issues).
- **OpenTelemetry** — `OTEL_EXPORTER_OTLP_ENDPOINT` (+ `OTEL_EXPORTER_OTLP_HEADERS`).
- **Uptime** — `UPTIMEROBOT_API_KEY` or `BETTERSTACK_TOKEN` (auto-registers a check on the new URL).
