"""Poller's PR merge detection: when Devin reports pr_state='merged',
mark our session completed AND terminate the Devin session (so it stops
burning ACUs after the work is done)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class _StubCreated:
    devin_session_id: str
    devin_url: str


class _StubDevin:
    """Programmable stub that returns a scripted SessionDetails."""

    def __init__(self, remote_details):
        self._remote = remote_details
        self.terminated: list[str] = []

    def create_session(self, **kwargs):
        return _StubCreated(devin_session_id="devin-x", devin_url="https://x/devin-x")

    def get_session(self, session_id: str):
        return self._remote

    def terminate_session(self, devin_session_id: str) -> None:
        self.terminated.append(devin_session_id)


def _details(status, pr_state):
    from app.devin.types import PullRequestRef, SessionDetails
    return SessionDetails(
        devin_session_id="devin-x",
        status=status, raw_status=status,
        status_detail=None, acus_consumed=2.5,
        pull_requests=[PullRequestRef(
            pr_url="https://github.com/x/y/pull/99", pr_state=pr_state
        )],
    )


def _seed_running_session_with_pr(db, devin_id="devin-x"):
    pk = db.sessions.try_reserve(
        work_key="issue:owner/repo:1:devin-security",
        trigger_type="cve_issue", trigger_ref="https://x/issues/1",
        issue_number=1, label="devin-security",
    )
    db.sessions.update(
        pk,
        devin_session_id=devin_id,
        devin_url=f"https://app.devin.ai/sessions/{devin_id}",
        status="running",
        pr_url="https://github.com/x/y/pull/99",
        prompt_snapshot="upgrade something",
    )
    return pk


@pytest.fixture
def patched_devin(monkeypatch, db_path):
    def _install(remote):
        s = _StubDevin(remote)
        from app.devin import factory
        monkeypatch.setattr(factory, "get_client", lambda: s)
        return s
    return _install


def test_merged_pr_marks_session_completed_and_terminates_devin(patched_devin):
    """The pivotal happy-path transition: PR merged → session done."""
    from app import db, orchestrator
    stub = patched_devin(_details(status="running", pr_state="merged"))
    pk = _seed_running_session_with_pr(db)

    summary = orchestrator.poll_once()
    row = db.sessions.get(pk)

    assert row.status == "completed", f"Expected completed, got {row.status}"
    assert row.completed_at is not None
    assert "devin-x" in stub.terminated, \
        "Devin session must be terminated to stop ACU bleed after merge"
    assert summary.get("merged") == 1


def test_open_pr_keeps_session_running(patched_devin):
    """An open PR is NOT a completion signal — session stays active so we
    keep polling for ACU updates and potential CI failures."""
    from app import db, orchestrator
    stub = patched_devin(_details(status="running", pr_state="open"))
    pk = _seed_running_session_with_pr(db)

    orchestrator.poll_once()
    row = db.sessions.get(pk)

    assert row.status == "running"
    assert row.completed_at is None
    assert stub.terminated == []


def test_closed_pr_marks_cancelled(patched_devin):
    """PR closed without merge — work was rejected. Mark cancelled with
    reason; don't claim completion."""
    from app import db, orchestrator
    patched_devin(_details(status="running", pr_state="closed"))
    pk = _seed_running_session_with_pr(db)

    orchestrator.poll_once()
    row = db.sessions.get(pk)

    assert row.status == "cancelled"
    assert row.error_message == "pr_closed_without_merge"
    assert row.completed_at is not None


def test_merge_detection_is_idempotent_after_terminal(patched_devin):
    """If the session is already terminal (e.g. previously processed merge),
    a re-poll doesn't re-terminate or rewrite completed_at."""
    from app import db, orchestrator
    stub = patched_devin(_details(status="completed", pr_state="merged"))
    pk = _seed_running_session_with_pr(db)
    # Pre-mark as completed (simulating a prior tick already processed it)
    db.sessions.update(pk, status="completed", completed_at="2026-05-26T00:00:00+00:00")

    orchestrator.poll_once()
    row = db.sessions.get(pk)

    # completed_at NOT bumped (still old timestamp)
    assert row.completed_at == "2026-05-26T00:00:00+00:00"
    # Terminate NOT called again (status is already terminal)
    assert stub.terminated == []
