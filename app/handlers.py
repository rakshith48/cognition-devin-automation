"""Event handlers — one per sub-label.

Each handler is a sync function that takes the raw GitHub payload, applies
safety checks (idempotency, concurrency, daily cap), spawns Devin work, and
returns a short result string that gets recorded on webhook_events.

The return string is the trace — every dispatched event has a one-line audit
record visible via /sessions and the dashboard.
"""
from __future__ import annotations

import logging

from app import db, devin, prompts, settings

logger = logging.getLogger(__name__)


def handle_security_issue(issue: dict) -> str:
    issue_url = issue.get("html_url", "")
    issue_number = issue.get("number", 0)

    # Safety rails — short-circuit before any cost.
    if db.sessions.has_active_for(issue_url):
        return f"skipped:already_active_for_issue:{issue_number}"
    if db.sessions.count_active() >= settings.MAX_CONCURRENT_SESSIONS:
        return f"skipped:concurrency_cap:{settings.MAX_CONCURRENT_SESSIONS}"
    if db.sessions.count_started_today() >= settings.MAX_SESSIONS_PER_DAY:
        return f"skipped:daily_cap:{settings.MAX_SESSIONS_PER_DAY}"

    # Parse the issue body filled by file_issues.py; bail out cleanly if it
    # doesn't look like a CVE we filed.
    ctx = prompts.parse_issue_to_cve_context(issue)
    if ctx is None:
        return f"skipped:not_a_cve_issue:{issue_number}"

    prompt = prompts.build_cve_prompt(ctx, fork_url=settings.FORK_URL)

    try:
        client = devin.factory.get_client()
        created = client.create_session(
            prompt=prompt,
            repos=[settings.FORK_REPO],
            title=f"CVE remediation: {ctx.cve_id} ({ctx.package})",
            tags=["cve", ctx.severity.lower(), ctx.package],
            max_acu_limit=settings.DEVIN_MAX_ACU_PER_SESSION,
            devin_mode=settings.DEVIN_MODE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Devin create_session failed for issue %s", issue_url)
        return f"error:devin_create_failed:{type(exc).__name__}"

    pk = db.sessions.insert(
        devin_session_id=created.devin_session_id,
        devin_url=created.devin_url,
        trigger_type="cve_issue",
        trigger_ref=issue_url,
        issue_number=issue_number,
        label=settings.SECURITY_LABEL,
        prompt_snapshot=prompt,
    )
    logger.info(
        "Spawned Devin session %s for CVE %s on issue #%d (db pk=%d)",
        created.devin_session_id, ctx.cve_id, issue_number, pk,
    )
    return f"created_session:{created.devin_session_id}"


def handle_quality_issue(issue: dict) -> str:
    logger.info("TODO: quality-issue handler not yet implemented for %s", issue.get("html_url"))
    return f"stub:would_spawn_quality_session:{issue.get('number')}"


def handle_generic_remediation(issue: dict) -> str:
    logger.info("TODO: generic-remediation handler not yet implemented for %s",
                issue.get("html_url"))
    return f"stub:would_spawn_generic_session:{issue.get('number')}"


def handle_ci_failure(workflow_run: dict) -> str:
    logger.info(
        "TODO[Task#9]: would spawn CI-fix session for workflow_run id=%s",
        workflow_run.get("id"),
    )
    return f"stub:would_spawn_ci_fix:{workflow_run.get('id')}"
