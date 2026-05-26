"""Event handlers — one per sub-label.

Each handler follows the atomic-reservation pattern:
  1. Cheap safety checks (concurrency, daily cap)
  2. Per-handler precondition guards (Devin authorship, attempt cap, etc)
  3. try_reserve(work_key) — INSERT with UNIQUE, atomic
  4. Strict parse + validation: refuse rather than send Devin a malformed prompt
  5. Devin create_session — the paid step
  6. activate(): UPDATE the reserved row with devin_session_id + prompt
  7. On any failure between 3-6: mark the row failed (so it's visible
     in the dashboard, not orphaned)

The reservation row is the audit trail. Even a session that never made it
to Devin shows up in /sessions with status='failed' and a clear reason.
"""
from __future__ import annotations

import logging

from app import db, devin, github_client, prompts, settings

logger = logging.getLogger(__name__)


def _escalate_to_human(parent, repo: str, pr_number: int, attempt_count: int) -> None:
    """Cap-hit cleanup: stop in-flight work, surface to dashboard, notify
    the PR. Idempotent — if the parent is already marked needs_attention
    from a previous cap-hit webhook, this is a no-op (no duplicate PR
    comments, no double-terminate calls)."""
    if parent.status == "needs_attention":
        logger.info("Escalation already done for parent %s — no-op", parent.devin_session_id)
        return

    logger.warning(
        "CI-fix cap (%d) reached for PR #%d; escalating to human review",
        attempt_count, pr_number,
    )

    # 1. Mark the parent visible in the dashboard's needs_human bucket.
    db.sessions.update(
        parent.id,
        status="needs_attention",
        error_message=(
            f"CI-fix exhausted after {attempt_count} attempts — needs human review"
        ),
    )

    # 2. Stop in-flight ACU burn by terminating any still-running children.
    client = devin.factory.get_client()
    for child in db.sessions.find_active_children(parent.devin_session_id):
        if child.devin_session_id:
            try:
                client.terminate_session(child.devin_session_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Terminate failed for child %s: %s", child.devin_session_id, exc
                )
        db.sessions.update(
            child.id, status="cancelled", error_message="cap_hit_on_parent"
        )

    # 3. Visible signal in the engineering team's review surface.
    try:
        github_client.post_pr_comment(repo, pr_number, _escalation_pr_comment(
            repo=repo, pr_number=pr_number, attempt_count=attempt_count,
            parent_devin_url=parent.devin_url,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("PR comment failed for #%d: %s", pr_number, exc)


def _escalation_pr_comment(
    *, repo: str, pr_number: int, attempt_count: int, parent_devin_url: str | None
) -> str:
    parent_link = (
        f"[Parent Devin session]({parent_devin_url})"
        if parent_devin_url else "Parent session id missing"
    )
    return (
        f"## :rotating_light: Devin Maintenance Orchestrator escalation\n\n"
        f"CI-fix attempts exhausted after **{attempt_count}** tries. The "
        f"most recent failing CI workflow continues to fail; the autonomous "
        f"loop cannot resolve it.\n\n"
        f"Active fix-sessions have been terminated to stop further ACU "
        f"spend. **This PR needs human review.**\n\n"
        f"- {parent_link}\n"
        f"- Configured cap: `MAX_FIX_ATTEMPTS={attempt_count}`\n\n"
        f"_If you fix this manually and want autonomous fixes to resume on a "
        f"new failure, re-run the failing workflow — the orchestrator treats "
        f"each `workflow_run.id` as a fresh chain start (subject to the cap)._"
    )


def _repo_full_name(issue: dict) -> str:
    """Extract owner/name from an issue payload. GitHub puts it in two places
    depending on the event; this handles both."""
    repo = issue.get("repository") or {}
    if repo.get("full_name"):
        return repo["full_name"]
    url = issue.get("repository_url", "")
    if url.startswith("https://api.github.com/repos/"):
        return url.removeprefix("https://api.github.com/repos/")
    return settings.FORK_REPO


# ============================================================================
# handle_security_issue — the CVE flagship handler
# ============================================================================

def handle_security_issue(issue: dict) -> str:
    issue_url = issue.get("html_url", "")
    issue_number = issue.get("number", 0)
    repo = _repo_full_name(issue)
    work_key = db.sessions.make_work_key(repo, issue_number, settings.SECURITY_LABEL)

    if db.sessions.count_active() >= settings.MAX_CONCURRENT_SESSIONS:
        # Queue instead of drop: the poller will retry on the next tick.
        # Concurrency cap is transient (active sessions complete); dropping
        # would silently lose real work.
        return f"queued:concurrency_cap:{work_key}"
    if db.sessions.count_started_today() >= settings.MAX_SESSIONS_PER_DAY:
        # Daily cap is intentional throttle, NOT queue — reset at midnight.
        return f"skipped:daily_cap:{settings.MAX_SESSIONS_PER_DAY}"

    pk = db.sessions.try_reserve(
        work_key=work_key,
        trigger_type="cve_issue",
        trigger_ref=issue_url,
        issue_number=issue_number,
        label=settings.SECURITY_LABEL,
    )
    if pk is None:
        return f"skipped:already_reserved:{work_key}"

    ctx = prompts.parse_issue_to_cve_context(issue)
    if ctx is None:
        db.sessions.mark_failed(pk, "not_a_cve_issue")
        return f"skipped:not_a_cve_issue:{issue_number}"
    missing = ctx.missing_required_fields()
    if missing:
        db.sessions.mark_failed(pk, f"missing_fields:{','.join(missing)}")
        return f"skipped:missing_fields:{','.join(missing)}"

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
            # Force Devin to produce a validated remediation report so the
            # dashboard can show summary, risk, tests, blockers — not just
            # 'session completed.'
            structured_output_schema=prompts.CVE_REMEDIATION_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Devin create_session failed for issue %s", issue_url)
        db.sessions.mark_failed(pk, f"devin_create_failed:{type(exc).__name__}:{exc}")
        return f"error:devin_create_failed:{type(exc).__name__}"

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


# ============================================================================
# handle_ci_failure — closes the loop on Devin PRs whose CI fails
# ============================================================================
# This is the "Dependabot stops here, Devin keeps going" path. Every check
# below is defensive: a single mis-routed event would burn ACUs on an
# unrelated PR or worse, write into someone else's PR branch.
# ============================================================================

def handle_ci_failure(workflow_run: dict) -> str:
    run_id = workflow_run.get("id")
    repo_obj = workflow_run.get("repository") or {}
    repo = repo_obj.get("full_name", settings.FORK_REPO)

    # Guard 1: must be a PR-triggered workflow, not a push/schedule.
    if workflow_run.get("event") != "pull_request":
        return f"skipped:not_pr_triggered:{workflow_run.get('event')}"

    # Guard 2: payload must reference a PR. (GH includes pull_requests[]
    # array on PR-event workflow runs.)
    pr_refs = workflow_run.get("pull_requests") or []
    if not pr_refs:
        return f"skipped:no_pr_in_payload:{run_id}"
    pr_number = pr_refs[0].get("number")
    if not pr_number:
        return f"skipped:no_pr_number:{run_id}"

    # Fetch PR for the rest of the guards. If this fails, we abort — no
    # point spawning a fix without context.
    try:
        pr = github_client.get_pr(repo, pr_number)
    except Exception as exc:  # noqa: BLE001
        logger.exception("PR fetch failed: %s/%s", repo, pr_number)
        return f"error:pr_fetch_failed:{type(exc).__name__}"
    if pr is None:
        return f"skipped:pr_not_found:{pr_number}"

    pr_url = pr.get("html_url") or ""
    head_ref = (pr.get("head") or {}).get("ref", "")

    # Guard 3: only act on Devin branches. Belt + suspenders: we also
    # verify a tracked session below, but the branch-name check stops us
    # touching unrelated PRs before any DB lookup or API call.
    if not head_ref.startswith("devin/"):
        return f"skipped:not_devin_branch:{head_ref}"

    # Guard 4: we must have a tracked parent session for this PR. If we
    # don't, this isn't a session WE spawned — it's some other branch
    # named devin/* by coincidence (or pre-existing state).
    parent = db.sessions.find_by_pr(pr_url)
    if parent is None:
        return f"skipped:no_tracked_session_for_pr:{pr_number}"

    # Guard 5: don't double-up with Devin's own CI-watch loop. Every Devin
    # CVE session has 'Wait for CI checks and fix any failures' in its own
    # task list — while the parent session is still alive, IT owns CI
    # fixing. Spawning a child here would race the parent on the same
    # branch (concurrent pushes, wasted ACUs). Our handler is the
    # FALLBACK: only fires once the parent session has exited and CI is
    # still failing.
    if parent.status not in db.TERMINAL_STATUSES:
        return f"skipped:parent_still_active:{parent.status}"

    # Guard 6: respect the loop cap. A PR that keeps failing after N fix
    # attempts needs a human, not another autonomous run. Query the chain
    # (parent + all its children) for the highest attempt seen, NOT just
    # the parent's row — parent never gets fix_attempt incremented, so
    # using it directly would let attempts past 1 run indefinitely.
    current_max = db.sessions.max_fix_attempt_for_parent(parent.devin_session_id)
    if current_max >= settings.MAX_FIX_ATTEMPTS:
        _escalate_to_human(parent, repo, pr_number, current_max)
        return f"escalated:max_fix_attempts:{current_max}"
    next_attempt = current_max + 1

    # Guard 6: don't fight a human. If someone pushed commits to the
    # branch between Devin's last commit and now, hands off.
    try:
        commits = github_client.list_pr_commits(repo, pr_number)
    except Exception as exc:  # noqa: BLE001
        logger.exception("PR commits fetch failed: %s/%s", repo, pr_number)
        return f"error:commits_fetch_failed:{type(exc).__name__}"
    if github_client.has_non_devin_commits(commits):
        return f"skipped:human_commits_on_branch:{pr_number}"

    # Cheap rails before reservation.
    if db.sessions.count_active() >= settings.MAX_CONCURRENT_SESSIONS:
        return f"skipped:concurrency_cap:{settings.MAX_CONCURRENT_SESSIONS}"

    # Guard 7: atomic dedupe by workflow_run.id. Webhook retries for the
    # same run produce one session; a workflow RE-RUN produces a new
    # run_id and gets a fresh session.
    work_key = db.sessions.make_ci_fix_work_key(repo, run_id)
    pk = db.sessions.try_reserve(
        work_key=work_key,
        trigger_type="ci_failure",
        trigger_ref=pr_url,
        issue_number=pr_number,
        label=settings.CI_FIX_LABEL,
        parent_devin_session_id=parent.devin_session_id,
        fix_attempt_number=next_attempt,
    )
    if pk is None:
        return f"skipped:already_reserved:{work_key}"

    # Fetch failing logs — best-effort; an empty tail just leaves Devin
    # to discover the failure in CI itself.
    try:
        logs_tail = github_client.get_workflow_run_failing_job_logs(
            repo, run_id, tail_lines=200,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Log fetch failed: %s", exc)
        logs_tail = f"(could not fetch logs: {exc})"

    ctx = prompts.CiFixContext(
        pr_url=pr_url,
        branch=head_ref,
        workflow_name=workflow_run.get("name", "unknown"),
        failure_logs_tail=logs_tail,
        parent_prompt=parent.prompt_snapshot or "(parent prompt not preserved)",
        attempt_number=next_attempt,
        max_attempts=settings.MAX_FIX_ATTEMPTS,
    )
    prompt = prompts.build_ci_fix_prompt(ctx)

    try:
        client = devin.factory.get_client()
        created = client.create_session(
            prompt=prompt,
            repos=[settings.FORK_REPO],
            title=f"CI fix attempt #{next_attempt}: PR #{pr_number}",
            tags=["ci-fix", str(next_attempt)],
            max_acu_limit=settings.DEVIN_MAX_ACU_PER_SESSION,
            devin_mode=settings.DEVIN_MODE,
            parent_session_id=parent.devin_session_id,   # Devin tracks the chain natively
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Devin create_session failed for CI fix on PR %s", pr_url)
        db.sessions.mark_failed(pk, f"devin_create_failed:{type(exc).__name__}:{exc}")
        return f"error:devin_create_failed:{type(exc).__name__}"

    db.sessions.update(
        pk,
        devin_session_id=created.devin_session_id,
        devin_url=created.devin_url,
        prompt_snapshot=prompt,
        status="pending",
    )
    logger.info(
        "Spawned CI-fix session %s for PR #%d (attempt %d/%d, parent=%s)",
        created.devin_session_id, pr_number, next_attempt,
        settings.MAX_FIX_ATTEMPTS, parent.devin_session_id,
    )
    return f"created_session:{created.devin_session_id}"


# ============================================================================
# Stubs for other sub-labels — kept so the dispatcher routes are real.
# ============================================================================

def handle_quality_issue(issue: dict) -> str:
    logger.info("TODO: quality-issue handler not yet implemented for %s", issue.get("html_url"))
    return f"stub:would_spawn_quality_session:{issue.get('number')}"


def handle_generic_remediation(issue: dict) -> str:
    logger.info("TODO: generic-remediation handler not yet implemented for %s",
                issue.get("html_url"))
    return f"stub:would_spawn_generic_session:{issue.get('number')}"
