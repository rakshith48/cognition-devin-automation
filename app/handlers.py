"""Event handlers — one per sub-label.

Currently stubbed; the real CVE handler lands in Task #6 (Devin call wired up)
and the CI-fix handler lands in Task #9. Stubs return a string describing
what they WOULD do, which gets recorded as the webhook's handler_result for
end-to-end observability of the dispatch chain.
"""
from __future__ import annotations

import logging

from app import db, settings

logger = logging.getLogger(__name__)


def handle_security_issue(issue: dict) -> str:
    issue_url = issue["html_url"]
    if db.sessions.has_active_for(issue_url):
        return f"skipped:already_active_for_issue:{issue['number']}"
    if db.sessions.count_active() >= settings.MAX_CONCURRENT_SESSIONS:
        return f"skipped:concurrency_cap:{settings.MAX_CONCURRENT_SESSIONS}"
    if db.sessions.count_started_today() >= settings.MAX_SESSIONS_PER_DAY:
        return f"skipped:daily_cap:{settings.MAX_SESSIONS_PER_DAY}"
    logger.info("TODO[Task#6]: would spawn Devin session for security issue %s", issue_url)
    return f"stub:would_spawn_security_session:{issue['number']}"


def handle_quality_issue(issue: dict) -> str:
    logger.info("TODO: quality-issue handler not yet implemented for %s", issue["html_url"])
    return f"stub:would_spawn_quality_session:{issue['number']}"


def handle_generic_remediation(issue: dict) -> str:
    logger.info("TODO: generic-remediation handler not yet implemented for %s", issue["html_url"])
    return f"stub:would_spawn_generic_session:{issue['number']}"


def handle_ci_failure(workflow_run: dict) -> str:
    logger.info(
        "TODO[Task#9]: would spawn CI-fix session for workflow_run id=%s",
        workflow_run.get("id"),
    )
    return f"stub:would_spawn_ci_fix:{workflow_run.get('id')}"
