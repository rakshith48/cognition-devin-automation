"""Runtime configuration. All env vars and defaults live here.

Fail-fast on missing required vars at import time — better than discovering
a misconfigured webhook secret when the first event arrives at 2am.
"""
from __future__ import annotations

import os


def _required(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _int_env(key: str, default: int) -> int:
    raw = os.environ.get(key)
    return int(raw) if raw else default


# ---------- Devin ----------
DEVIN_API_KEY = os.environ.get("DEVIN_API_KEY", "")
DEVIN_ORG_ID = os.environ.get("DEVIN_ORG_ID", "")
DEVIN_BASE_URL = os.environ.get("DEVIN_BASE_URL", "https://api.devin.ai/v3")
DEVIN_MODE = os.environ.get("DEVIN_MODE", "normal")  # "normal" or "fast"
DEVIN_MAX_ACU_PER_SESSION = _int_env("DEVIN_MAX_ACU_PER_SESSION", 50)

# ---------- GitHub ----------
GH_WEBHOOK_SECRET = os.environ.get("GH_WEBHOOK_SECRET", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
FORK_URL = os.environ.get("FORK_URL", "https://github.com/rakshith48/superset")
FORK_REPO = os.environ.get("FORK_REPO", "rakshith48/superset")  # owner/name

# ---------- Safety rails ----------
MAX_CONCURRENT_SESSIONS = _int_env("MAX_CONCURRENT_SESSIONS", 3)
MAX_SESSIONS_PER_DAY = _int_env("MAX_SESSIONS_PER_DAY", 25)
SESSION_TIMEOUT_SECONDS = _int_env("SESSION_TIMEOUT_SECONDS", 45 * 60)
MAX_FIX_ATTEMPTS = _int_env("MAX_FIX_ATTEMPTS", 3)
POLL_INTERVAL_SECONDS = _int_env("POLL_INTERVAL_SECONDS", 30)

# ---------- Cost-to-engineering translation ----------
# Used for the dashboard "hours/dollars saved" derivation.
# These are calibration constants — defensible but not precise.
HOURS_SAVED_PER_COMPLETED_SESSION = float(os.environ.get("HOURS_SAVED_PER_COMPLETED_SESSION", "4"))
ENGINEER_HOURLY_RATE_USD = float(os.environ.get("ENGINEER_HOURLY_RATE_USD", "150"))

# ---------- Routing labels ----------
GATE_LABEL = "devin-remediate"  # presence required for ANY action
SECURITY_LABEL = "devin-security"
QUALITY_LABEL = "devin-quality"
CI_FIX_LABEL = "devin-ci-fix"

# ---------- Admin endpoints ----------
# Two-layer defense: routes only mount if enabled, AND each call requires a token.
# In production both should be off unless an operator is actively using them.
ENABLE_ADMIN_ROUTES = os.environ.get("ENABLE_ADMIN_ROUTES", "false").lower() == "true"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def validate_for_runtime() -> list[str]:
    """Return list of misconfiguration messages. Empty list = OK."""
    problems = []
    if not DEVIN_API_KEY:
        problems.append("DEVIN_API_KEY not set")
    if not DEVIN_ORG_ID:
        problems.append("DEVIN_ORG_ID not set")
    if not GH_WEBHOOK_SECRET:
        problems.append("GH_WEBHOOK_SECRET not set (webhooks will be rejected)")
    if not GH_TOKEN:
        problems.append("GH_TOKEN not set (cannot fetch PR/workflow data)")
    return problems
