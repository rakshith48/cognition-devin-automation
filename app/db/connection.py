"""SQLite connection management and schema bootstrap.

Connection-per-call with WAL mode. Simple enough at our scale and avoids the
threadsafety footgun of sharing a sqlite3 connection across FastAPI's worker
threadpool.

init_db runs an idempotent migration sequence so an existing db file from
an earlier schema picks up new columns without losing data.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DB_PATH", "/data/automation.db"))


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=5000")
    try:
        yield c
    finally:
        c.close()


def _column_exists(c: sqlite3.Connection, table: str, column: str) -> bool:
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _column_is_not_null(c: sqlite3.Connection, table: str, column: str) -> bool:
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    for r in rows:
        if r["name"] == column:
            return bool(r["notnull"])
    return False


def _rebuild_sessions_table(c: sqlite3.Connection) -> None:
    """SQLite has no ALTER COLUMN. To relax NOT NULL constraints on
    devin_session_id/devin_url/prompt_snapshot (required for the reservation
    pattern), we rebuild the table: copy → drop old → rename new.
    """
    logger.info("Migration: rebuilding sessions table to relax NOT NULL columns")
    c.executescript("""
        CREATE TABLE sessions_new (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            work_key                TEXT NOT NULL,
            devin_session_id        TEXT,
            devin_url               TEXT,
            trigger_type            TEXT NOT NULL,
            trigger_ref             TEXT NOT NULL,
            issue_number            INTEGER,
            label                   TEXT,
            parent_devin_session_id TEXT,
            prompt_snapshot         TEXT,
            status                  TEXT NOT NULL,
            raw_status              TEXT,
            pr_url                  TEXT,
            pr_urls_json            TEXT,
            acus_consumed           REAL DEFAULT 0,
            started_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at            TIMESTAMP,
            last_polled_at          TIMESTAMP,
            error_message           TEXT,
            fix_attempt_number      INTEGER NOT NULL DEFAULT 0
        );

        INSERT INTO sessions_new (
            id, work_key, devin_session_id, devin_url, trigger_type, trigger_ref,
            issue_number, label, parent_devin_session_id, prompt_snapshot,
            status, raw_status, pr_url, pr_urls_json, acus_consumed,
            started_at, completed_at, last_polled_at, error_message,
            fix_attempt_number
        )
        SELECT
            id, work_key, devin_session_id, devin_url, trigger_type, trigger_ref,
            issue_number, label, parent_devin_session_id, prompt_snapshot,
            status, raw_status, pr_url, pr_urls_json, acus_consumed,
            started_at, completed_at, last_polled_at, error_message,
            fix_attempt_number
        FROM sessions;

        DROP TABLE sessions;
        ALTER TABLE sessions_new RENAME TO sessions;
    """)


def init_db() -> None:
    """Create tables if missing, then run idempotent migrations.

    Each migration step is wrapped in an existence check so it's safe to
    re-run on every boot. No version table needed — the checks are the
    version table.
    """
    with conn() as c:
        # Fresh-install schema (also the post-migration target).
        c.executescript("""
        CREATE TABLE IF NOT EXISTS webhook_events (
            delivery_id    TEXT PRIMARY KEY,
            event_type     TEXT NOT NULL,
            action         TEXT,
            payload_json   TEXT NOT NULL,
            received_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at   TIMESTAMP,
            handler_result TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            work_key                TEXT NOT NULL,
            devin_session_id        TEXT,
            devin_url               TEXT,
            trigger_type            TEXT NOT NULL,
            trigger_ref             TEXT NOT NULL,
            issue_number            INTEGER,
            label                   TEXT,
            parent_devin_session_id TEXT,
            prompt_snapshot         TEXT,
            status                  TEXT NOT NULL,
            raw_status              TEXT,
            pr_url                  TEXT,
            pr_urls_json            TEXT,
            acus_consumed           REAL DEFAULT 0,
            started_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at            TIMESTAMP,
            last_polled_at          TIMESTAMP,
            error_message           TEXT,
            fix_attempt_number      INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_status      ON sessions(status);
        CREATE INDEX IF NOT EXISTS idx_sessions_trigger_ref ON sessions(trigger_ref);
        CREATE INDEX IF NOT EXISTS idx_sessions_parent      ON sessions(parent_devin_session_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_started     ON sessions(started_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_devin_id    ON sessions(devin_session_id);
        """)

        # ----- migrations from earlier schema -----
        # M1: add work_key column if pre-existing rows are missing it.
        if not _column_exists(c, "sessions", "work_key"):
            logger.info("Migration: adding sessions.work_key column")
            c.execute("ALTER TABLE sessions ADD COLUMN work_key TEXT")

        # Backfill work_key on any rows missing it. Uses issue URL + label so
        # the value is stable across re-runs (and matches what new code generates
        # when called with the same trigger_ref + label).
        c.execute("""
            UPDATE sessions
            SET work_key = COALESCE(
                'issue:' || trigger_ref || ':' || COALESCE(label, 'unknown'),
                devin_session_id
            )
            WHERE work_key IS NULL OR work_key = ''
        """)

        # M2: relax NOT NULL constraints if we're on the original schema —
        # the reservation pattern needs to insert before we have a Devin
        # session ID. CREATE TABLE IF NOT EXISTS won't drop NOT NULL from an
        # existing column, so we detect and rebuild.
        if _column_is_not_null(c, "sessions", "devin_session_id"):
            _rebuild_sessions_table(c)

        # M3: unique index on work_key (built after backfill + rebuild so no
        # constraint trips). IF NOT EXISTS handles re-runs.
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_work_key ON sessions(work_key)")


def healthcheck() -> bool:
    try:
        with conn() as c:
            c.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def reset_all() -> None:
    """Used by POST /admin/reset for demo replay. Wipes both tables."""
    with conn() as c:
        c.executescript("DELETE FROM sessions; DELETE FROM webhook_events;")
