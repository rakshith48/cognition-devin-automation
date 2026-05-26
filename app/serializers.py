"""DB row → API/UI dict serialization.

One place. Field renames or omissions happen here, not scattered across
route handlers and the dashboard.
"""
from __future__ import annotations

from app import db


def session_to_dict(r: db.SessionRow) -> dict:
    return {
        "id": r.id,
        "devin_session_id": r.devin_session_id,
        "devin_url": r.devin_url,
        "trigger_type": r.trigger_type,
        "trigger_ref": r.trigger_ref,
        "issue_number": r.issue_number,
        "label": r.label,
        "parent_devin_session_id": r.parent_devin_session_id,
        "status": r.status,
        "raw_status": r.raw_status,
        "pr_url": r.pr_url,
        "acus_consumed": r.acus_consumed,
        "started_at": r.started_at,
        "completed_at": r.completed_at,
        "last_polled_at": r.last_polled_at,
        "error_message": r.error_message,
        "fix_attempt_number": r.fix_attempt_number,
    }
