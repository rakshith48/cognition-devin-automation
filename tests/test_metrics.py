"""Metrics aggregation — success means PR-created, not session-exited.
This is the truthfulness test for the VP-Eng question."""
from __future__ import annotations


def _make_row(**kwargs):
    """Helper to construct SessionRow with sensible defaults for tests."""
    from app.db.sessions import SessionRow
    defaults = dict(
        id=1, work_key="k", devin_session_id="d", devin_url="u",
        trigger_type="cve_issue", trigger_ref="r", issue_number=1,
        label="devin-security", parent_devin_session_id=None,
        prompt_snapshot="p", status="completed", raw_status="exit",
        pr_url=None, pr_urls_json=None, acus_consumed=2.0,
        started_at="2026-05-26", completed_at=None, last_polled_at=None,
        error_message=None, fix_attempt_number=0,
    )
    defaults.update(kwargs)
    return SessionRow(**defaults)


def test_success_rate_counts_only_pr_completions():
    """A session that 'completed' but produced no PR is NOT a success."""
    from app import metrics
    rows = [
        _make_row(id=1, status="completed", pr_url="https://x/pull/1"),
        _make_row(id=2, status="completed", pr_url=None),  # exited, no PR
        _make_row(id=3, status="failed"),
    ]
    m = metrics.compute_dashboard_metrics(rows)
    # 3 terminal, 1 PR — success_rate = 1/3
    assert m["pr_created"] == 1
    assert m["completed_without_pr"] == 1
    assert m["failed_sessions"] == 1
    assert m["success_rate"] == round(1 / 3, 3)


def test_active_sessions_excluded_from_success_rate():
    """A still-running session should not affect success rate either way."""
    from app import metrics
    rows = [
        _make_row(id=1, status="running"),
        _make_row(id=2, status="completed", pr_url="https://x/pull/1"),
    ]
    m = metrics.compute_dashboard_metrics(rows)
    # 1 terminal, 1 PR → 100%
    assert m["success_rate"] == 1.0
    assert m["active_sessions"] == 1


def test_active_session_with_pr_does_not_blow_success_rate_past_100():
    """Regression: an active session (e.g. needs_attention) that opened a
    PR used to inflate the success_rate numerator without inflating the
    terminal denominator — producing values like 200%. Numerator must be
    a subset of denominator."""
    from app import metrics
    rows = [
        # Terminal + PR: real success.
        _make_row(id=1, status="completed", pr_url="https://x/pull/1"),
        # Still active (needs_attention), has a PR open — does NOT count.
        _make_row(id=2, status="needs_attention", pr_url="https://x/pull/2"),
    ]
    m = metrics.compute_dashboard_metrics(rows)
    assert m["pr_created"] == 2, "lifetime PR count includes both"
    assert m["success_rate"] == 1.0, "success rate stays bounded at 100%"
    assert m["success_rate"] <= 1.0


def test_needs_human_bucket():
    """blocked / needs_attention / timeout all need a human."""
    from app import metrics
    rows = [
        _make_row(id=1, status="blocked"),
        _make_row(id=2, status="needs_attention"),
        _make_row(id=3, status="timeout"),
        _make_row(id=4, status="running"),
    ]
    m = metrics.compute_dashboard_metrics(rows)
    assert m["needs_human"] == 3


def test_empty_db_zero_success_rate():
    from app import metrics
    m = metrics.compute_dashboard_metrics([])
    assert m["total_sessions"] == 0
    assert m["success_rate"] == 0.0
    assert m["estimated_dollars_saved"] == 0


def test_by_label_breakdown():
    from app import metrics
    rows = [
        _make_row(id=1, label="devin-security"),
        _make_row(id=2, label="devin-security"),
        _make_row(id=3, label="devin-quality"),
        _make_row(id=4, label=None),  # excluded
    ]
    m = metrics.compute_dashboard_metrics(rows)
    assert m["by_label"] == {"devin-security": 2, "devin-quality": 1}
