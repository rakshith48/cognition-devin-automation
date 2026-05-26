"""Minimal GitHub REST client for the CI-fix path.

Scoped narrowly: we only need a few read endpoints (PR details, PR commits,
workflow run logs). Anything else and we'd pull in PyGithub, but that's
weight we don't need.

Lazy singleton — same lifecycle pattern as devin.factory.
"""
from __future__ import annotations

import logging
import threading
import zipfile
from io import BytesIO

import httpx

from app import settings

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"


_client: httpx.Client | None = None
_lock = threading.Lock()


def _http() -> httpx.Client:
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            if not settings.GH_TOKEN:
                raise RuntimeError("GH_TOKEN not configured")
            _client = httpx.Client(
                base_url=_GITHUB_API,
                headers={
                    "Authorization": f"Bearer {settings.GH_TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )
    return _client


def reset() -> None:
    """Called on app shutdown to release sockets."""
    global _client
    if _client is not None:
        _client.close()
        _client = None


def get_pr(repo: str, number: int) -> dict | None:
    """Returns the PR object or None on 404. Other errors propagate."""
    resp = _http().get(f"/repos/{repo}/pulls/{number}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def list_pr_commits(repo: str, number: int) -> list[dict]:
    """Returns up to the most recent 250 commits on the PR's branch."""
    resp = _http().get(
        f"/repos/{repo}/pulls/{number}/commits", params={"per_page": 250}
    )
    resp.raise_for_status()
    return resp.json()


def has_non_devin_commits(
    commits: list[dict], devin_logins: frozenset[str] | None = None
) -> bool:
    """Heuristic: a commit counts as 'human' if its committer login isn't
    a known Devin bot identity. GitHub's commit object exposes both
    `author` and `committer` GitHub users; we check committer because
    that's who actually pushed the change. The author falls back to
    catching commits where committer is null (e.g. web edits).

    The bot login set is configurable via DEVIN_BOT_LOGINS so operators
    on enterprise installs with a different app slug can extend it
    without code changes. This is a third-layer defense — the branch
    name prefix and tracked-parent-session checks happen first.
    """
    allowed = devin_logins if devin_logins is not None else settings.DEVIN_BOT_LOGINS
    for c in commits:
        committer = (c.get("committer") or {}).get("login")
        author = (c.get("author") or {}).get("login")
        if committer and committer not in allowed:
            return True
        if author and author not in allowed:
            return True
    return False


def post_pr_comment(repo: str, pr_number: int, body: str) -> None:
    """Post a comment on a PR. Uses the issues endpoint because GitHub
    treats PR-comments-on-conversation as issue comments (vs review
    comments which attach to a diff line)."""
    resp = _http().post(
        f"/repos/{repo}/issues/{pr_number}/comments",
        json={"body": body},
    )
    resp.raise_for_status()


def get_workflow_run_failing_job_logs(
    repo: str, run_id: int, *, tail_lines: int = 200
) -> str:
    """Fetch logs for the FIRST failing job in a workflow run.

    GitHub returns logs as a zipfile containing one .txt per job step. We
    pick the failing job, concatenate its step logs, and return the tail.
    """
    jobs_resp = _http().get(f"/repos/{repo}/actions/runs/{run_id}/jobs")
    jobs_resp.raise_for_status()
    jobs = jobs_resp.json().get("jobs", [])
    failing = [j for j in jobs if j.get("conclusion") == "failure"]
    if not failing:
        return "(no failing job found in this workflow run)"

    job_id = failing[0]["id"]
    logs_resp = _http().get(f"/repos/{repo}/actions/jobs/{job_id}/logs",
                            follow_redirects=True)
    if logs_resp.status_code == 410:
        return "(logs expired and are no longer available)"
    logs_resp.raise_for_status()

    body = logs_resp.content
    # Job logs endpoint returns plain text directly, NOT a zip. The runs/
    # endpoint is the one that returns a zip — but we're hitting jobs/.
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        # Defensive: if GH ever returns a zip, handle it.
        try:
            with zipfile.ZipFile(BytesIO(body)) as zf:
                text = "\n".join(
                    zf.read(name).decode("utf-8", errors="replace")
                    for name in zf.namelist()[:5]
                )
        except Exception:
            return "(failed to decode logs)"

    lines = text.splitlines()
    return "\n".join(lines[-tail_lines:])
