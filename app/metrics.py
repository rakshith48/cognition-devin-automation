"""Metrics aggregation — pure functions over SessionRow lists.

Buckets are deliberately granular so the dashboard can answer the VP-of-Eng
question "is this actually working?" honestly. "Devin exited" is not the
same as "PR was opened" is not the same as "PR is mergeable."

What we DON'T track yet (out of scope for this iteration):
  - ci_green: whether the PR's CI is passing. Would require a second polling
    loop against GitHub. Called out in the architecture walkthrough as the
    natural next addition.
"""
from __future__ import annotations

from app import db, settings

# Statuses we treat as "human needs to look at this."
NEEDS_HUMAN_STATUSES = frozenset({"blocked", "needs_attention", "timeout"})


def compute_dashboard_metrics(rows: list[db.SessionRow]) -> dict:
    # Lifecycle buckets — every row falls in exactly one of these counts.
    total = len(rows)
    active = sum(1 for r in rows if r.status in db.ACTIVE_STATUSES)
    completed = sum(1 for r in rows if r.status == "completed")
    failed = sum(1 for r in rows if r.status == "failed")
    cancelled = sum(1 for r in rows if r.status == "cancelled")
    needs_human = sum(1 for r in rows if r.status in NEEDS_HUMAN_STATUSES)

    # Output buckets — what work product came out.
    # pr_created is a LIFETIME count: every session that ever opened a PR,
    # including sessions still active.
    pr_created = sum(1 for r in rows if r.pr_url)
    # "Devin said done, but no PR" — usually a no-op or a "couldn't proceed"
    # finish that the dashboard should flag.
    completed_without_pr = sum(
        1 for r in rows if r.status == "completed" and not r.pr_url
    )

    # Cost.
    total_acus = sum((r.acus_consumed or 0) for r in rows)

    # Honest success rate: of sessions that have reached a TERMINAL state,
    # what fraction produced a PR? Numerator must be a subset of denominator,
    # so we count "terminal AND has PR" — not the lifetime pr_created
    # (which includes active sessions like needs_attention with an open PR).
    terminal = sum(1 for r in rows if r.status in db.TERMINAL_STATUSES)
    successful_terminal = sum(
        1 for r in rows if r.status in db.TERMINAL_STATUSES and r.pr_url
    )
    success_rate = (successful_terminal / terminal) if terminal else 0.0

    # Engineering value (calibrated estimate; see settings for the constants).
    hours_saved = successful_terminal * settings.HOURS_SAVED_PER_COMPLETED_SESSION

    # Per-label breakdown so the dashboard can show "5 security, 2 quality."
    by_label: dict[str, int] = {}
    for r in rows:
        if r.label:
            by_label[r.label] = by_label.get(r.label, 0) + 1

    return {
        # Lifecycle
        "total_sessions": total,
        "active_sessions": active,
        "completed_sessions": completed,
        "failed_sessions": failed,
        "cancelled_sessions": cancelled,
        "needs_human": needs_human,

        # Outputs (the bucket that actually matters for the pitch)
        "pr_created": pr_created,
        "completed_without_pr": completed_without_pr,

        # Cost
        "total_acus_consumed": round(total_acus, 2),

        # Headline ratios + derived value
        "success_rate": round(success_rate, 3),  # pr_created / terminal
        "estimated_hours_saved": hours_saved,
        "estimated_dollars_saved": int(hours_saved * settings.ENGINEER_HOURLY_RATE_USD),

        # Slices
        "by_label": by_label,
        "last_activity_at": rows[0].started_at if rows else None,
    }
