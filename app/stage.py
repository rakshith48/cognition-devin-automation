"""Workflow-stage derivation.

Devin gives us a session-level status (running, completed, error, ...). The
dashboard needs the WORKFLOW-level stage: where is this unit of remediation
work in its lifecycle? That's a richer concept that requires looking at
multiple fields (trigger_type + status + pr_url presence + state of any
child fix-sessions).

Pure function: SessionRow + the full set of related rows in, stage string out.
No I/O, no DB calls — caller passes the rows it already has.
"""
from __future__ import annotations

from app.db.sessions import ACTIVE_STATUSES, SessionRow


def compute_stage(row: SessionRow, all_rows: list[SessionRow]) -> str:
    """Return the workflow stage label for the given row.

    For CVE sessions (parents), aggregates child fix-session state.
    For CI-fix sessions (children), shows the attempt number + status.
    """
    if row.trigger_type == "cve_issue":
        return _cve_stage(row, all_rows)
    if row.trigger_type == "ci_failure":
        return _ci_fix_stage(row)
    # Generic/quality stubs — just surface the status.
    return row.status


def _cve_stage(row: SessionRow, all_rows: list[SessionRow]) -> str:
    if row.status == "cancelled":
        return "cancelled"
    if row.status in {"failed", "timeout"}:
        return "failed"
    if row.status in {"needs_attention", "blocked"}:
        return "needs human"
    if row.status == "reserving":
        return "detected"

    # status now ∈ {pending, running, completed}.
    if not row.pr_url:
        return "devin running"

    # PR exists — check for ANY active child fix-sessions.
    if row.devin_session_id:
        active_children = [
            r for r in all_rows
            if r.parent_devin_session_id == row.devin_session_id
            and r.status in ACTIVE_STATUSES
        ]
        if active_children:
            return "ci fix running"

    if row.status == "completed":
        return "verified"
    return "PR open"


def _ci_fix_stage(row: SessionRow) -> str:
    status_label = row.status.replace("_", " ")
    return f"fix #{row.fix_attempt_number}: {status_label}"


# Stage → emoji indicator for the dashboard. Distinct from session-status
# emoji because stage is a richer concept.
STAGE_EMOJI = {
    "detected":         "🟣",
    "devin running":    "🔵",
    "PR open":          "🟢",
    "ci fix running":   "🟠",
    "verified":         "✅",
    "needs human":      "🟡",
    "failed":           "🔴",
    "cancelled":        "⚫",
}


def stage_label(stage: str) -> str:
    """Stage string prefixed with its emoji, fallback to ⚪ for fix-* labels."""
    emoji = STAGE_EMOJI.get(stage, "⚪")
    return f"{emoji} {stage}"
