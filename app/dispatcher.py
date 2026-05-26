"""Label-based event router.

A single function decides which handler to call based on event type + labels.
Keeps routing logic out of the webhook receiver itself (testable in isolation,
swappable for new triggers without touching the HTTP layer).
"""
from __future__ import annotations

import logging

from app import handlers, settings

logger = logging.getLogger(__name__)


def dispatch(event_type: str, action: str | None, payload: dict) -> str:
    """Returns a short string describing the outcome — recorded in webhook_events.handler_result."""
    if event_type == "issues":
        return _dispatch_issue(action, payload)
    if event_type == "workflow_run":
        return _dispatch_workflow_run(action, payload)
    if event_type == "ping":
        return "ok:ping"
    return f"skipped:unhandled_event:{event_type}"


def _dispatch_issue(action: str | None, payload: dict) -> str:
    if action not in ("opened", "labeled", "reopened"):
        return f"skipped:issue_action:{action}"
    issue = payload.get("issue") or {}
    label_names = {lbl.get("name") for lbl in (issue.get("labels") or []) if lbl.get("name")}

    if settings.GATE_LABEL not in label_names:
        return "skipped:no_gate_label"

    # Sub-label routing. First match wins; order matters.
    if settings.SECURITY_LABEL in label_names:
        return handlers.handle_security_issue(issue)
    if settings.QUALITY_LABEL in label_names:
        return handlers.handle_quality_issue(issue)
    return handlers.handle_generic_remediation(issue)


def _dispatch_workflow_run(action: str | None, payload: dict) -> str:
    if action != "completed":
        return f"skipped:workflow_action:{action}"
    run = payload.get("workflow_run") or {}
    if run.get("conclusion") != "failure":
        return f"skipped:workflow_conclusion:{run.get('conclusion')}"
    return handlers.handle_ci_failure(run)
