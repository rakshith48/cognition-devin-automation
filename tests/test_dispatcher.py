"""Label-based dispatcher — gate label is mandatory; sub-label routes to
the right handler; CI-failure routing checks the right preconditions."""
from __future__ import annotations

from app import dispatcher


def _issue(*, number=1, url="https://x/issues/1", labels=(), body=""):
    return {
        "number": number,
        "html_url": url,
        "labels": [{"name": n} for n in labels],
        "body": body,
        "repository_url": "https://api.github.com/repos/owner/repo",
    }


def test_issue_without_gate_label_skipped():
    res = dispatcher.dispatch("issues", "opened", {"issue": _issue(labels=["random"])})
    assert res == "skipped:no_gate_label"


def test_issue_with_gate_only_uses_generic_handler():
    res = dispatcher.dispatch("issues", "opened",
                              {"issue": _issue(labels=["devin-remediate"])})
    # generic-remediation is a stub; just verify the routing path
    assert res.startswith("stub:would_spawn_generic_session")


def test_workflow_run_non_failure_skipped():
    res = dispatcher.dispatch(
        "workflow_run", "completed",
        {"workflow_run": {"id": 1, "conclusion": "success"}},
    )
    assert res.startswith("skipped:workflow_conclusion")


def test_workflow_run_non_completed_skipped():
    res = dispatcher.dispatch(
        "workflow_run", "requested",
        {"workflow_run": {"id": 1, "conclusion": "failure"}},
    )
    assert res.startswith("skipped:workflow_action")


def test_unhandled_event_returns_skip():
    res = dispatcher.dispatch("issue_comment", "created", {})
    assert res.startswith("skipped:unhandled_event")


def test_ping_event_returns_ok():
    """GitHub sends a ping when webhook is first registered."""
    res = dispatcher.dispatch("ping", None, {})
    assert res == "ok:ping"
