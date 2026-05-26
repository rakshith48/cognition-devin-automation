"""sessions table — Devin work units we've spawned.

Lifecycle:
  reserving → (Devin call succeeds) → pending → running → completed/failed
                ↓ (Devin call fails)
              failed

`try_reserve` is the atomic claim: it inserts a row with the work_key UNIQUE
constraint, returning the PK on success or None on race. The Devin call only
happens AFTER a successful reservation, so we never have an orphaned paid
session with no DB row.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db.connection import conn

# 'reserving' is the pre-Devin state. Kept in ACTIVE so concurrent webhooks
# see the in-flight work and skip; the row transitions to 'pending' once we
# have a devin_session_id, or 'failed' if Devin rejects the request.
ACTIVE_STATUSES = frozenset({"reserving", "pending", "running", "blocked", "needs_attention"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timeout"})

# Updatable columns — explicit allow-list defeats accidental SQL injection
# via kwargs even when callers are internal.
_UPDATABLE_COLUMNS = frozenset({
    "status", "raw_status", "pr_url", "pr_urls_json", "acus_consumed",
    "completed_at", "last_polled_at", "error_message", "parent_devin_session_id",
    "devin_session_id", "devin_url", "prompt_snapshot",
})


@dataclass
class SessionRow:
    id: int
    work_key: str
    devin_session_id: str | None
    devin_url: str | None
    trigger_type: str
    trigger_ref: str
    issue_number: int | None
    label: str | None
    parent_devin_session_id: str | None
    prompt_snapshot: str | None
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


def make_work_key(repo: str, issue_number: int | str, label: str) -> str:
    """Stable identity for a unit of remediation work.

    Deliberately scoped to (repo, issue_number, label) — not delivery_id —
    so semantically identical events (opened then labeled, redelivery, etc)
    collapse to the same key.
    """
    return f"issue:{repo}:{issue_number}:{label}"


def try_reserve(
    *,
    work_key: str,
    trigger_type: str,
    trigger_ref: str,
    issue_number: int | None = None,
    label: str | None = None,
    parent_devin_session_id: str | None = None,
    fix_attempt_number: int = 0,
) -> int | None:
    """Atomically claim a unit of work. Returns the PK on success, None if
    work_key is already taken (idempotent — concurrent webhooks both pass
    the safety checks but only one wins the INSERT).
    """
    with conn() as c:
        try:
            cur = c.execute(
                """INSERT INTO sessions (
                    work_key, trigger_type, trigger_ref, issue_number, label,
                    parent_devin_session_id, fix_attempt_number, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserving')""",
                (
                    work_key, trigger_type, trigger_ref, issue_number, label,
                    parent_devin_session_id, fix_attempt_number,
                ),
            )
            return cur.lastrowid or 0
        except sqlite3.IntegrityError:
            return None


def update(session_pk: int, **fields: Any) -> None:
    if not fields:
        return
    unknown = set(fields) - _UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"Refusing to update unknown columns: {sorted(unknown)}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), session_pk))


def mark_failed(session_pk: int, reason: str) -> None:
    update(session_pk, status="failed", error_message=reason,
           completed_at=_now_iso())


def get(session_pk: int) -> SessionRow | None:
    with conn() as c:
        cur = c.execute("SELECT * FROM sessions WHERE id = ?", (session_pk,))
        r = cur.fetchone()
        return SessionRow.from_row(r) if r else None


def get_by_devin_id(devin_session_id: str) -> SessionRow | None:
    with conn() as c:
        cur = c.execute("SELECT * FROM sessions WHERE devin_session_id = ?", (devin_session_id,))
        r = cur.fetchone()
        return SessionRow.from_row(r) if r else None


def get_by_work_key(work_key: str) -> SessionRow | None:
    with conn() as c:
        cur = c.execute("SELECT * FROM sessions WHERE work_key = ?", (work_key,))
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


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
