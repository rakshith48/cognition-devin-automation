"""webhook_events table — idempotency log for incoming GitHub events."""
from __future__ import annotations

import json
import sqlite3

from app.db.connection import conn


def record(delivery_id: str, event_type: str, action: str | None, payload: dict) -> bool:
    """Return True if newly inserted, False if duplicate (idempotent insert)."""
    with conn() as c:
        try:
            c.execute(
                "INSERT INTO webhook_events (delivery_id, event_type, action, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (delivery_id, event_type, action, json.dumps(payload)),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def mark_processed(delivery_id: str, result: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE webhook_events SET processed_at = CURRENT_TIMESTAMP, handler_result = ? "
            "WHERE delivery_id = ?",
            (result, delivery_id),
        )


def get(delivery_id: str) -> sqlite3.Row | None:
    with conn() as c:
        cur = c.execute("SELECT * FROM webhook_events WHERE delivery_id = ?", (delivery_id,))
        return cur.fetchone()


def list_queued(limit: int = 50) -> list[sqlite3.Row]:
    """Return webhook_events whose last handler result was 'queued:*',
    oldest first. The orchestrator retries these from poll_once."""
    with conn() as c:
        cur = c.execute(
            "SELECT * FROM webhook_events "
            "WHERE handler_result LIKE 'queued:%' "
            "ORDER BY received_at ASC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()
