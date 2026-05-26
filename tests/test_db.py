"""Database boundary tests: webhook dedup, atomic work_key reservation."""
from __future__ import annotations


def test_webhook_dedup_returns_false_on_duplicate(db_path):
    from app import db
    assert db.webhook_events.record("dlv-1", "issues", "opened", {"a": 1}) is True
    assert db.webhook_events.record("dlv-1", "issues", "opened", {"a": 1}) is False


def test_webhook_dedup_distinct_deliveries(db_path):
    from app import db
    assert db.webhook_events.record("dlv-A", "issues", "opened", {}) is True
    assert db.webhook_events.record("dlv-B", "issues", "opened", {}) is True


def test_webhook_mark_processed(db_path):
    from app import db
    db.webhook_events.record("dlv-1", "issues", "opened", {})
    db.webhook_events.mark_processed("dlv-1", "created_session:foo")
    row = db.webhook_events.get("dlv-1")
    assert row["handler_result"] == "created_session:foo"
    assert row["processed_at"] is not None


def test_session_reservation_first_wins(db_path):
    from app import db
    wk = "issue:owner/repo:42:devin-security"
    pk1 = db.sessions.try_reserve(
        work_key=wk, trigger_type="cve_issue",
        trigger_ref="https://x/issues/42", issue_number=42, label="devin-security",
    )
    pk2 = db.sessions.try_reserve(
        work_key=wk, trigger_type="cve_issue",
        trigger_ref="https://x/issues/42", issue_number=42, label="devin-security",
    )
    assert pk1 is not None and pk1 > 0
    assert pk2 is None, "Second reservation must lose on UNIQUE(work_key)"


def test_reservation_inserts_with_status_reserving(db_path):
    """Reservation lifecycle starts in 'reserving' — promoted to 'pending'
    only after the Devin call succeeds."""
    from app import db
    pk = db.sessions.try_reserve(
        work_key="k1", trigger_type="cve_issue", trigger_ref="r", label="devin-security",
    )
    row = db.sessions.get(pk)
    assert row.status == "reserving"
    assert row.devin_session_id is None


def test_reservation_keeps_active_session_visible(db_path):
    """A reservation row counts as active for concurrency cap purposes."""
    from app import db
    db.sessions.try_reserve(
        work_key="k1", trigger_type="cve_issue", trigger_ref="r", label="x",
    )
    assert db.sessions.count_active() == 1


def test_make_work_key_format():
    """The semantic key must collapse opened/labeled/reopened/redelivery
    of the same issue into one identity."""
    from app.db.sessions import make_work_key
    assert make_work_key("owner/repo", 42, "devin-security") == \
        "issue:owner/repo:42:devin-security"


def test_update_rejects_unknown_columns(db_path):
    """SQL-injection defense: column allow-list refuses unknown kwargs."""
    import pytest

    from app import db
    pk = db.sessions.try_reserve(
        work_key="k", trigger_type="cve_issue", trigger_ref="r", label="x",
    )
    with pytest.raises(ValueError, match="evil_col"):
        db.sessions.update(pk, evil_col="DROP TABLE")


def test_mark_failed_transitions_to_terminal(db_path):
    from app import db
    pk = db.sessions.try_reserve(
        work_key="k", trigger_type="cve_issue", trigger_ref="r", label="x",
    )
    db.sessions.mark_failed(pk, "test_reason")
    row = db.sessions.get(pk)
    assert row.status == "failed"
    assert row.error_message == "test_reason"
    assert row.status in db.TERMINAL_STATUSES
