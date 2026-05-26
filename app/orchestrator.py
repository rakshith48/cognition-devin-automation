"""Background poller — keeps the local DB in sync with Devin's session state.

Why polling: Devin v3 doesn't push webhooks back to the caller, so we have
to ask. The poll interval is a tradeoff: too long and the dashboard feels
dead, too short and we waste API calls. 30s is a comfortable default and
matches the cadence Devin itself updates session state.

Lifecycle:
- Mounted in the FastAPI lifespan as an asyncio task
- Wraps the sync Devin calls with asyncio.to_thread so we don't block the
  event loop
- Catches its own exceptions per-tick so one bad session doesn't kill the
  loop
- Stops on app shutdown via cancellation

Timeout enforcement is INACTIVITY-based (time since Devin's updated_at),
not absolute session age. A session that spent two hours waiting for user
input is not "stuck"; one that hasn't moved in 45 minutes is.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from app import db, devin, settings

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _seconds_since(iso_or_sqlite: str) -> float:
    """Tolerant timestamp parser — SQLite's CURRENT_TIMESTAMP returns
    'YYYY-MM-DD HH:MM:SS' (no TZ), our own writes return ISO with TZ."""
    s = iso_or_sqlite.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds()


def poll_once() -> dict:
    """Sync — runs one tick. Returns a small summary dict for logging.

    Pure-by-effects: every output (status update, timeout) is a DB write or
    a Devin call; no in-memory state survives between ticks.
    """
    client = devin.factory.get_client()
    rows = db.sessions.list_non_terminal()
    summary = {"polled": 0, "updated": 0, "timed_out": 0, "errors": 0}

    for row in rows:
        # 'reserving' rows have no Devin handle yet — skip; the handler
        # either promotes them to 'pending' or marks them 'failed'.
        if not row.devin_session_id:
            continue

        summary["polled"] += 1
        try:
            remote = client.get_session(row.devin_session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Poll failed for session %s: %s", row.devin_session_id, exc)
            summary["errors"] += 1
            continue

        # Inactivity-based timeout: trigger only if Devin's own updated_at
        # hasn't advanced in SESSION_TIMEOUT_SECONDS. Devin's updated_at is
        # a Unix epoch int. Falls back to local started_at if absent.
        if remote.status in {"pending", "running"}:
            if remote.updated_at:
                from datetime import datetime
                inactivity = (
                    datetime.now(UTC)
                    - datetime.fromtimestamp(remote.updated_at, tz=UTC)
                ).total_seconds()
            else:
                inactivity = _seconds_since(row.last_polled_at or row.started_at)

            if inactivity > settings.SESSION_TIMEOUT_SECONDS:
                logger.warning(
                    "Session %s inactive for %.0fs (limit %ds); terminating",
                    row.devin_session_id, inactivity, settings.SESSION_TIMEOUT_SECONDS,
                )
                try:
                    client.terminate_session(row.devin_session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Terminate failed for %s: %s",
                                   row.devin_session_id, exc)
                db.sessions.update(
                    row.id,
                    status="timeout",
                    error_message=f"inactive for {int(inactivity)}s",
                    completed_at=_now_iso(),
                    last_polled_at=_now_iso(),
                )
                summary["timed_out"] += 1
                continue

        # Diff-aware update: only write if something changed. Includes
        # structured_output so we pick it up when Devin first produces it.
        remote_so_json = (
            json.dumps(remote.structured_output, sort_keys=True)
            if remote.structured_output else None
        )
        changed = (
            remote.status != row.status
            or remote.raw_status != row.raw_status
            or remote.first_pr_url != row.pr_url
            or float(remote.acus_consumed) != float(row.acus_consumed or 0)
            or remote_so_json != row.structured_output_json
        )
        if not changed:
            db.sessions.update(row.id, last_polled_at=_now_iso())
            continue

        updates: dict = {
            "status": remote.status,
            "raw_status": remote.raw_status,
            "acus_consumed": remote.acus_consumed,
            "last_polled_at": _now_iso(),
        }
        if remote.pull_requests:
            updates["pr_url"] = remote.first_pr_url
            updates["pr_urls_json"] = json.dumps([
                {"pr_url": p.pr_url, "pr_state": p.pr_state} for p in remote.pull_requests
            ])
        if remote_so_json is not None:
            updates["structured_output_json"] = remote_so_json
        if remote.status in db.TERMINAL_STATUSES and not row.completed_at:
            updates["completed_at"] = _now_iso()

        db.sessions.update(row.id, **updates)
        summary["updated"] += 1
        logger.info(
            "Session %s: %s → %s (raw=%s, ACUs=%.2f, PR=%s)",
            row.devin_session_id, row.status, remote.status,
            remote.raw_status, remote.acus_consumed, remote.first_pr_url or "-",
        )

    return summary


async def run_loop(stop_event: asyncio.Event) -> None:
    """Run poll_once every POLL_INTERVAL_SECONDS until stop_event is set."""
    logger.info("Poller starting (interval=%ds)", settings.POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            summary = await asyncio.to_thread(poll_once)
            if summary["updated"] or summary["timed_out"] or summary["errors"]:
                logger.info("Poll tick: %s", summary)
        except Exception:  # noqa: BLE001
            logger.exception("Poller tick crashed (continuing)")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.POLL_INTERVAL_SECONDS)
        except TimeoutError:
            pass
    logger.info("Poller stopped")
