"""sessions table — Devin work units we've spawned.

`update` uses a small column whitelist to keep dynamic SQL safe even if a
caller someday passes user-derived kwargs by mistake. Adding a new column?
Add to the whitelist explicitly.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db.connection import conn

ACTIVE_STATUSES = frozenset({"pending", "running", "blocked", "needs_attention"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timeout"})

# Columns allowed in update() — explicit allow-list defeats accidental SQL injection
# via kwargs even when callers are internal.
_UPDATABLE_COLUMNS = frozenset({
    "status", "raw_status", "pr_url", "pr_urls_json", "acus_consumed",
    "completed_at", "last_polled_at", "error_message", "parent_devin_session_id",
})


@dataclass
class SessionRow:
    id: int
    devin_session_id: str
    devin_url: str
    trigger_type: str
    trigger_ref: str
    issue_number: int | None
    label: str | None
    parent_devin_session_id: str | None
    prompt_snapshot: str
    status: str
    raw_status: str | None
    pr_url: str | None
    pr_urls_json: str | None
    acus_consumed: float
    started_at: str
    completed_at: str | None
    last_polled_at: str | None
    error_message: str | None
    fix_attempt_number: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "SessionRow":
        return cls(**{k: r[k] for k in r.keys()})


def insert(
    *,
    devin_session_id: str,
    devin_url: str,
    trigger_type: str,
    trigger_ref: str,
    prompt_snapshot: str,
    status: str = "pending",
    issue_number: int | None = None,
    label: str | None = None,
    parent_devin_session_id: str | None = None,
    fix_attempt_number: int = 0,
) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO sessions (
                devin_session_id, devin_url, trigger_type, trigger_ref,
                issue_number, label, parent_devin_session_id, prompt_snapshot,
                status, fix_attempt_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                devin_session_id, devin_url, trigger_type, trigger_ref,
                issue_number, label, parent_devin_session_id, prompt_snapshot,
                status, fix_attempt_number,
            ),
        )
        return cur.lastrowid or 0


def update(session_pk: int, **fields: Any) -> None:
    if not fields:
        return
    unknown = set(fields) - _UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"Refusing to update unknown columns: {sorted(unknown)}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), session_pk))


def get_by_devin_id(devin_session_id: str) -> SessionRow | None:
    with conn() as c:
        cur = c.execute("SELECT * FROM sessions WHERE devin_session_id = ?", (devin_session_id,))
        r = cur.fetchone()
        return SessionRow.from_row(r) if r else None


def list_non_terminal() -> list[SessionRow]:
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    with conn() as c:
        cur = c.execute(
            f"SELECT * FROM sessions WHERE status IN ({placeholders}) ORDER BY started_at",
            tuple(ACTIVE_STATUSES),
        )
        return [SessionRow.from_row(r) for r in cur.fetchall()]


def has_active_for(trigger_ref: str) -> bool:
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    with conn() as c:
        cur = c.execute(
            f"SELECT 1 FROM sessions WHERE trigger_ref = ? AND status IN ({placeholders}) LIMIT 1",
            (trigger_ref, *ACTIVE_STATUSES),
        )
        return cur.fetchone() is not None


def count_active() -> int:
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    with conn() as c:
        cur = c.execute(
            f"SELECT COUNT(*) FROM sessions WHERE status IN ({placeholders})",
            tuple(ACTIVE_STATUSES),
        )
        return cur.fetchone()[0]


def count_started_today() -> int:
    with conn() as c:
        cur = c.execute("SELECT COUNT(*) FROM sessions WHERE date(started_at) = date('now')")
        return cur.fetchone()[0]


def find_by_pr(pr_url: str) -> SessionRow | None:
    with conn() as c:
        cur = c.execute(
            "SELECT * FROM sessions WHERE pr_url = ? ORDER BY started_at DESC LIMIT 1",
            (pr_url,),
        )
        r = cur.fetchone()
        return SessionRow.from_row(r) if r else None


def list_recent(limit: int = 100) -> list[SessionRow]:
    with conn() as c:
        cur = c.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [SessionRow.from_row(r) for r in cur.fetchall()]
