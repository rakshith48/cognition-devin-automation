"""Event handlers — one per sub-label.

Each handler follows the atomic-reservation pattern:
  1. Cheap safety checks (concurrency, daily cap)
  2. try_reserve(work_key) — INSERT with UNIQUE constraint, atomic
  3. Parse + validate inputs strictly (refuse on missing required fields)
  4. Call Devin (only paid step)
  5. activate(): UPDATE the reserved row with devin_session_id + prompt
  6. On any failure between 2 and 5: mark the row failed (so it's visible
     in the dashboard, not orphaned)

The reservation row is the audit trail. Even a session that never made it
to Devin shows up in /sessions with status='failed' and a clear reason.
"""
from __future__ import annotations

import logging

from app import db, devin, prompts, settings

logger = logging.getLogger(__name__)


def _repo_full_name(issue: dict) -> str:
    """Extract owner/name from an issue payload. GitHub puts it in two places
    depending on the event; this handles both."""
    repo = issue.get("repository") or {}
    if repo.get("full_name"):
        return repo["full_name"]
    # On issue events, repo is at the payload root not inside issue;
    # but the issue.repository_url field always works.
    url = issue.get("repository_url", "")
    if url.startswith("https://api.github.com/repos/"):
        return url.removeprefix("https://api.github.com/repos/")
    return settings.FORK_REPO


def handle_security_issue(issue: dict) -> str:
    issue_url = issue.get("html_url", "")
    issue_number = issue.get("number", 0)
    repo = _repo_full_name(issue)
    work_key = db.sessions.make_work_key(repo, issue_number, settings.SECURITY_LABEL)

    # 1. Cheap safety rails — no row created yet, no I/O cost.
    if db.sessions.count_active() >= settings.MAX_CONCURRENT_SESSIONS:
        return f"skipped:concurrency_cap:{settings.MAX_CONCURRENT_SESSIONS}"
    if db.sessions.count_started_today() >= settings.MAX_SESSIONS_PER_DAY:
        return f"skipped:daily_cap:{settings.MAX_SESSIONS_PER_DAY}"

    # 2. Atomic reservation — wins the race against concurrent webhooks.
    pk = db.sessions.try_reserve(
        work_key=work_key,
        trigger_type="cve_issue",
        trigger_ref=issue_url,
        issue_number=issue_number,
        label=settings.SECURITY_LABEL,
    )
    if pk is None:
        return f"skipped:already_reserved:{work_key}"

    # 3. Strict parsing — refuse rather than send Devin a malformed prompt.
    ctx = prompts.parse_issue_to_cve_context(issue)
    if ctx is None:
        db.sessions.mark_failed(pk, "not_a_cve_issue")
        return f"skipped:not_a_cve_issue:{issue_number}"
    missing = ctx.missing_required_fields()
    if missing:
        db.sessions.mark_failed(pk, f"missing_fields:{','.join(missing)}")
        return f"skipped:missing_fields:{','.join(missing)}"

    prompt = prompts.build_cve_prompt(ctx, fork_url=settings.FORK_URL)

    # 4. Paid call.
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
        db.sessions.mark_failed(pk, f"devin_create_failed:{type(exc).__name__}:{exc}")
        return f"error:devin_create_failed:{type(exc).__name__}"

    # 5. Activate — promote reservation to pending with real Devin handle.
    db.sessions.update(
        pk,
        devin_session_id=created.devin_session_id,
        devin_url=created.devin_url,
        prompt_snapshot=prompt,
        status="pending",
    )
    logger.info(
        "Spawned Devin session %s for CVE %s on issue #%d (db pk=%d, work_key=%s)",
        created.devin_session_id, ctx.cve_id, issue_number, pk, work_key,
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
