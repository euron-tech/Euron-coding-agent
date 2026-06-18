"""Self-healing — keep a deployment alive.

Two cooperating loops (policy: rollback is autonomous, code fixes are PRs):

  * FAST loop (deterministic, autonomous): after a deploy, poll the app's health
    endpoint; if it's unhealthy, automatically roll back to the last known-good
    release. This only ever reverts to a previous good state, so it is always safe
    to run without asking.
  * SLOW loop (generative, gated): pull current production errors (see monitor.py),
    have the model write a patch + regression test, and open a PR for a human to
    merge. That part is driven by the `heal` command's prompt, not this module — here
    we provide the safe, deterministic primitives it builds on.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

from . import deploy


def http_health(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Single GET. Healthy = HTTP 2xx/3xx. Never raises."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "euron-agent-health"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            code = getattr(r, "status", 200)
            return (200 <= code < 400), f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return (200 <= e.code < 400), f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {type(e).__name__}: {e}"


def check_health(url: str, path: str = "/health", attempts: int = 1,
                 delay: float = 0.0, _sleep=time.sleep) -> tuple[bool, str]:
    """Poll `url`+`path` up to `attempts` times. Returns (healthy, detail)."""
    full = url.rstrip("/") + (path if path.startswith("/") else "/" + path)
    last = ""
    for i in range(max(1, attempts)):
        ok, detail = http_health(full)
        last = detail
        if ok:
            return True, f"healthy ({detail}) at {full}"
        if i < attempts - 1 and delay:
            _sleep(delay)
    return False, f"unhealthy ({last}) at {full}"


def rollback(ctx, target_name: str, params: dict | None = None) -> tuple[bool, str]:
    """Build and RUN the rollback command for a target (autonomous, safe).

    Returns (ok, output). Rolling back only restores a prior good release.
    """
    ok, cmd = deploy.rollback_command(target_name, params)
    if not ok:
        return False, cmd
    from .tools import run_command  # lazy to avoid import cycle

    outcome = run_command(ctx, cmd)
    return outcome.ok, outcome.output


def heal_once(ctx, url: str, target_name: str, params: dict | None = None,
              path: str = "/health", attempts: int = 3, delay: float = 5.0) -> dict:
    """Fast-loop self-heal: check health; if unhealthy, auto-rollback. Returns a
    report dict (no exceptions). The generative error→PR step is handled by the
    `heal` command prompt, not here."""
    healthy, detail = check_health(url, path, attempts=attempts, delay=delay)
    report = {"url": url, "target": target_name, "healthy": healthy, "detail": detail,
              "action": "none", "rollback_ok": None, "rollback_output": ""}
    if healthy:
        return report
    t = deploy.target(target_name)
    if not t or not t.rollback_cmd:
        report["action"] = "no-rollback-available"
        return report
    report["action"] = "rollback"
    rb_ok, rb_out = rollback(ctx, target_name, params)
    report["rollback_ok"] = rb_ok
    report["rollback_output"] = rb_out[:500]
    # Re-check after rollback.
    healthy2, detail2 = check_health(url, path, attempts=attempts, delay=delay)
    report["healthy_after_rollback"] = healthy2
    report["detail_after_rollback"] = detail2
    return report
