"""Workflow stage derivation — pure function, no DB needed."""
from __future__ import annotations

from app.db.sessions import SessionRow
from app.stage import compute_stage


def _cve(**overrides) -> SessionRow:
    defaults = dict(
        id=1, work_key="k", devin_session_id="devin-parent", devin_url="u",
        trigger_type="cve_issue", trigger_ref="r", issue_number=1,
        label="devin-security", parent_devin_session_id=None,
        prompt_snapshot="p", status="running", raw_status="running",
        pr_url=None, pr_urls_json=None, acus_consumed=0,
        started_at="2026-05-26", completed_at=None, last_polled_at=None,
        error_message=None, fix_attempt_number=0,
    )
    defaults.update(overrides)
    return SessionRow(**defaults)


def _child(*, parent="devin-parent", attempt=1, status="running") -> SessionRow:
    return SessionRow(
        id=100 + attempt, work_key=f"ci_fix:repo:{attempt}",
        devin_session_id=f"devin-child-{attempt}",
        devin_url=f"https://x/{attempt}",
        trigger_type="ci_failure", trigger_ref="r", issue_number=1,
        label="devin-ci-fix", parent_devin_session_id=parent,
        prompt_snapshot="p", status=status, raw_status=status,
        pr_url=None, pr_urls_json=None, acus_consumed=0,
        started_at="2026-05-26", completed_at=None, last_polled_at=None,
        error_message=None, fix_attempt_number=attempt,
    )


# ---------- CVE parent stages ----------

def test_reserving_is_detected():
    assert compute_stage(_cve(status="reserving"), []) == "detected"


def test_running_without_pr_is_devin_running():
    assert compute_stage(_cve(status="running", pr_url=None), []) == "devin running"


def test_running_with_pr_no_active_children_is_pr_open():
    row = _cve(status="running", pr_url="https://x/pull/1")
    assert compute_stage(row, [row]) == "PR open"


def test_running_with_pr_and_active_child_is_ci_fix_running():
    parent = _cve(status="running", pr_url="https://x/pull/1")
    fix = _child(attempt=1, status="running")
    assert compute_stage(parent, [parent, fix]) == "ci fix running"


def test_completed_with_pr_is_verified():
    row = _cve(status="completed", pr_url="https://x/pull/1")
    assert compute_stage(row, [row]) == "verified"


def test_needs_attention_is_needs_human():
    assert compute_stage(_cve(status="needs_attention"), []) == "needs human"


def test_blocked_is_also_needs_human():
    assert compute_stage(_cve(status="blocked"), []) == "needs human"


def test_failed_and_timeout_collapse_to_failed():
    assert compute_stage(_cve(status="failed"), []) == "failed"
    assert compute_stage(_cve(status="timeout"), []) == "failed"


def test_cancelled_passes_through():
    assert compute_stage(_cve(status="cancelled"), []) == "cancelled"


def test_finished_child_doesnt_count_as_active():
    """A completed child should NOT keep the parent in 'ci fix running'."""
    parent = _cve(status="completed", pr_url="https://x/pull/1")
    fix = _child(attempt=1, status="completed")
    assert compute_stage(parent, [parent, fix]) == "verified"


# ---------- CI-fix child stages ----------

def test_child_shows_attempt_number_and_status():
    fix = _child(attempt=2, status="running")
    assert compute_stage(fix, [fix]) == "fix #2: running"


def test_child_with_underscored_status_normalizes_to_spaces():
    fix = _child(attempt=1, status="needs_attention")
    assert compute_stage(fix, [fix]) == "fix #1: needs attention"
