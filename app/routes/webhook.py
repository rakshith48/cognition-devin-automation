"""GitHub webhook receiver.

Verifies signature, dedupes by delivery ID, defers handler work to a
background task so GitHub gets its 200 inside the 10s SLA regardless of how
slow the actual processing is.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app import db, dispatcher, settings, signature

logger = logging.getLogger(__name__)
router = APIRouter()


def _process(delivery_id: str, event_type: str, action: str | None, payload: dict) -> None:
    """Background dispatch. Records the result on webhook_events for observability."""
    try:
        result = dispatcher.dispatch(event_type, action, payload)
    except Exception as exc:  # noqa: BLE001 — last-resort catch so we never lose the event
        logger.exception("Dispatcher crashed for delivery %s", delivery_id)
        result = f"error:{type(exc).__name__}:{exc}"
    db.webhook_events.mark_processed(delivery_id, result)
    logger.info("Processed delivery=%s result=%s", delivery_id, result)


@router.post("/webhook/github")
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> JSONResponse:
    body = await request.body()

    if not signature.verify_github_signature(body, x_hub_signature_256, settings.GH_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="invalid signature")
    if not x_github_event or not x_github_delivery:
        raise HTTPException(status_code=400, detail="missing GitHub headers")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    action = payload.get("action")
    is_new = db.webhook_events.record(x_github_delivery, x_github_event, action, payload)
    if not is_new:
        return JSONResponse({"status": "duplicate", "delivery_id": x_github_delivery})

    background.add_task(_process, x_github_delivery, x_github_event, action, payload)
    return JSONResponse({"status": "accepted", "delivery_id": x_github_delivery})
