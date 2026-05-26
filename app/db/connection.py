"""SQLite connection management and schema bootstrap.

Connection-per-call with WAL mode. At our scale (single host, ~30s poll loop)
this is simpler than connection pooling and avoids the threadsafety footgun
of sharing a sqlite3 connection across FastAPI's threadpool.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

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


def init_db() -> None:
    with conn() as c:
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
            devin_session_id        TEXT UNIQUE NOT NULL,
            devin_url               TEXT NOT NULL,
            trigger_type            TEXT NOT NULL,
            trigger_ref             TEXT NOT NULL,
            issue_number            INTEGER,
            label                   TEXT,
            parent_devin_session_id TEXT,
            prompt_snapshot         TEXT NOT NULL,
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
        """)


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
