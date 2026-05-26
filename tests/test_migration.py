"""Migration regression tests.

Lock in the behaviors that previously bit us:
- The backfill computes the SAME canonical work_key as runtime code, so
  a migrated row deduplicates against new webhook events.
- Historical duplicates (multiple rows with the same trigger_ref+label)
  don't crash CREATE UNIQUE INDEX — losers get a deterministic suffix.
"""
from __future__ import annotations

import sqlite3


def _seed_legacy_row(c: sqlite3.Connection, *,
                     trigger_ref: str, label: str | None, issue_number: int | None,
                     devin_session_id: str | None = None) -> int:
    """Insert a row with empty-string work_key — the same state a row
    has right before the backfill step runs against it. Empty string
    satisfies the NOT NULL constraint while still being picked up by
    the backfill's `work_key IS NULL OR work_key = ''` filter."""
    cur = c.execute(
        "INSERT INTO sessions (work_key, trigger_type, trigger_ref, issue_number, "
        "label, devin_session_id, status, fix_attempt_number) "
        "VALUES ('', ?, ?, ?, ?, ?, ?, 0)",
        ("cve_issue", trigger_ref, issue_number, label, devin_session_id, "completed"),
    )
    return cur.lastrowid


def test_backfill_matches_runtime_work_key_format(db_path, monkeypatch):
    """A migrated row's work_key must match what handlers compute via
    make_work_key for the same issue — otherwise a fresh webhook for that
    issue spawns a duplicate paid Devin session."""
    from app import db
    from app.db.connection import _backfill_missing_work_keys
    from app.db.sessions import make_work_key

    issue_url = "https://github.com/owner/repo/issues/42"

    with db.conn() as c:
        c.execute("DROP INDEX IF EXISTS uq_sessions_work_key")
        pk = _seed_legacy_row(c, trigger_ref=issue_url,
                               label="devin-security", issue_number=42)
        _backfill_missing_work_keys(c)
        c.execute("CREATE UNIQUE INDEX uq_sessions_work_key ON sessions(work_key)")

    row = db.sessions.get(pk)
    expected = make_work_key("owner/repo", 42, "devin-security")
    assert row.work_key == expected, (
        f"Backfill must match runtime: expected {expected}, got {row.work_key}"
    )


def test_backfill_dedups_collisions_with_suffix(db_path):
    """Two historical rows for the same (repo, issue_number, label) must
    NOT crash CREATE UNIQUE INDEX. Older wins canonical, newer gets a
    dup: suffix so both rows coexist and the index can build.

    Mirrors the real migration: drop the UNIQUE INDEX (in a real run it
    wouldn't exist yet on a pre-migration DB), seed the historical rows,
    backfill, then re-create the index. The recreate is the moment that
    would crash without dedup."""
    from app import db
    from app.db.connection import _backfill_missing_work_keys

    issue_url = "https://github.com/owner/repo/issues/7"

    with db.conn() as c:
        c.execute("DROP INDEX IF EXISTS uq_sessions_work_key")
        pk1 = _seed_legacy_row(c, trigger_ref=issue_url, label="devin-security",
                                issue_number=7, devin_session_id="devin-A")
        pk2 = _seed_legacy_row(c, trigger_ref=issue_url, label="devin-security",
                                issue_number=7, devin_session_id="devin-B")
        _backfill_missing_work_keys(c)
        c.execute("CREATE UNIQUE INDEX uq_sessions_work_key ON sessions(work_key)")

    r1 = db.sessions.get(pk1)
    r2 = db.sessions.get(pk2)
    canonical = "issue:owner/repo:7:devin-security"
    assert r1.work_key == canonical, f"Older row should win canonical, got {r1.work_key}"
    assert r2.work_key == f"{canonical}:dup:{pk2}", (
        f"Newer row should be suffixed, got {r2.work_key}"
    )


def test_backfill_falls_back_for_unparseable_trigger_ref(db_path):
    """Rows with non-parseable trigger_ref (or no label) get 'legacy:<id>'
    rather than crashing or sharing a key."""
    from app import db
    from app.db.connection import _backfill_missing_work_keys

    with db.conn() as c:
        c.execute("DROP INDEX IF EXISTS uq_sessions_work_key")
        pk = _seed_legacy_row(c, trigger_ref="not-a-github-url",
                               label=None, issue_number=None)
        _backfill_missing_work_keys(c)
        c.execute("CREATE UNIQUE INDEX uq_sessions_work_key ON sessions(work_key)")

    row = db.sessions.get(pk)
    assert row.work_key == f"legacy:{pk}"


def test_backfill_respects_existing_work_keys(db_path):
    """If a row already has a non-canonical work_key, the backfill of
    OTHER rows must not steal that key."""
    from app import db
    from app.db.connection import _backfill_missing_work_keys

    issue_url = "https://github.com/owner/repo/issues/3"

    # Existing row already at the canonical key (e.g. assigned by
    # runtime before the migration ran).
    existing_pk = db.sessions.try_reserve(
        work_key="issue:owner/repo:3:devin-security",
        trigger_type="cve_issue",
        trigger_ref=issue_url,
        issue_number=3, label="devin-security",
    )
    with db.conn() as c:
        c.execute("DROP INDEX IF EXISTS uq_sessions_work_key")
        # Legacy row that would collide.
        legacy_pk = _seed_legacy_row(c, trigger_ref=issue_url,
                                      label="devin-security", issue_number=3)
        _backfill_missing_work_keys(c)
        c.execute("CREATE UNIQUE INDEX uq_sessions_work_key ON sessions(work_key)")

    legacy = db.sessions.get(legacy_pk)
    assert legacy.work_key == f"issue:owner/repo:3:devin-security:dup:{legacy_pk}", (
        f"Backfill must not steal an existing work_key; got {legacy.work_key}"
    )
    existing = db.sessions.get(existing_pk)
    assert existing.work_key == "issue:owner/repo:3:devin-security"
