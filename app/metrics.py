"""Metrics aggregation — pure functions over SessionRow lists.

Kept separate from HTTP and DB so the aggregation logic is testable in
isolation: hand it a list, get a dict back. Also lets the Streamlit
dashboard reuse the exact same shape without going through HTTP.
"""
from __future__ import annotations

from app import db, settings


def compute_dashboard_metrics(rows: list[db.SessionRow]) -> dict:
    active = sum(1 for r in rows if r.status in db.ACTIVE_STATUSES)
    completed = sum(1 for r in rows if r.status == "completed")
    failed = sum(1 for r in rows if r.status == "failed")
    prs = sum(1 for r in rows if r.pr_url)
    total_acus = sum((r.acus_consumed or 0) for r in rows)
    terminal = sum(1 for r in rows if r.status in db.TERMINAL_STATUSES)
    success_rate = (completed / terminal) if terminal else 0.0
    hours_saved = completed * settings.HOURS_SAVED_PER_COMPLETED_SESSION

    by_label: dict[str, int] = {}
    for r in rows:
        if r.label:
            by_label[r.label] = by_label.get(r.label, 0) + 1

    return {
        "active_sessions": active,
        "completed_sessions": completed,
        "failed_sessions": failed,
        "total_sessions": len(rows),
        "prs_opened": prs,
        "success_rate": round(success_rate, 3),
        "total_acus_consumed": round(total_acus, 2),
        "estimated_hours_saved": hours_saved,
        "estimated_dollars_saved": int(hours_saved * settings.ENGINEER_HOURLY_RATE_USD),
        "by_label": by_label,
        "last_activity_at": rows[0].started_at if rows else None,
    }
