"""Fire a webhook for a real GitHub issue against the local orchestrator.

Why this exists: the README's quickstart needs a way to trigger the full
pipeline without standing up ngrok and registering a webhook on GitHub
first. This script fetches a real issue from the fork via the GitHub
REST API, then POSTs it to /webhook/github with a valid HMAC signature
— exercising the same code path a real GitHub-delivered webhook would.

Usage:
    python scripts/simulate_issue.py <issue_number>           # default: localhost:8000
    python scripts/simulate_issue.py 1 --api http://host:8000

Requires env (loaded from .env if present): FORK_REPO, GH_TOKEN,
GH_WEBHOOK_SECRET. Reads with python-dotenv if installed, else os.environ
directly.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import httpx

# Best-effort .env load — avoids forcing python-dotenv as a dep.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:  # noqa: BLE001
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def fetch_issue(repo: str, number: int, token: str) -> dict:
    r = httpx.get(
        f"https://api.github.com/repos/{repo}/issues/{number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def post_webhook(api_url: str, payload: dict, secret: str, delivery_id: str) -> httpx.Response:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return httpx.post(
        f"{api_url.rstrip('/')}/webhook/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": sig,
        },
        timeout=15.0,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue_number", type=int, help="Issue number on FORK_REPO")
    parser.add_argument("--api", default="http://localhost:8000",
                        help="Orchestrator base URL (default: %(default)s)")
    parser.add_argument("--delivery-id", default=None,
                        help="Override X-GitHub-Delivery (default: sim-<issue>-<epoch>)")
    args = parser.parse_args()

    repo = os.environ.get("FORK_REPO")
    token = os.environ.get("GH_TOKEN")
    secret = os.environ.get("GH_WEBHOOK_SECRET")
    for name, val in (("FORK_REPO", repo), ("GH_TOKEN", token),
                      ("GH_WEBHOOK_SECRET", secret)):
        if not val:
            print(f"ERROR: {name} not set in env (.env or shell)", file=sys.stderr)
            return 2

    print(f"→ Fetching {repo}#{args.issue_number} from GitHub...")
    issue = fetch_issue(repo, args.issue_number, token)
    print(f"  title: {issue.get('title')}")
    print(f"  labels: {[lbl['name'] for lbl in issue.get('labels', [])]}")

    payload = {"action": "opened", "issue": issue}
    import time
    delivery_id = args.delivery_id or f"sim-{args.issue_number}-{int(time.time())}"

    print(f"→ POST {args.api}/webhook/github (delivery_id={delivery_id})...")
    resp = post_webhook(args.api, payload, secret, delivery_id)
    print(f"  {resp.status_code} {resp.text}")

    return 0 if resp.status_code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
