"""Admin endpoints — replay a captured webhook, reset the DB for demo runs.

Two-layer defense:
  1. Routes only mount if ENABLE_ADMIN_ROUTES=true at boot.
  2. Each request must present X-Admin-Token matching ADMIN_TOKEN.

The reasoning: an exposed `/admin/reset` is a "wipe my demo state" footgun
for anyone who knows the ngrok URL. The mount-time gate stops the public
route from existing at all in normal operation; the token gate covers the
case where an operator left it enabled.
"""
from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from fastapi.responses import JSONResponse

from app import db, settings
from app.routes.webhook import _process

router = APIRouter(prefix="/admin")


def _require_token(provided: str | None) -> None:
    if not settings.ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="admin token not configured")
    if not provided or not hmac.compare_digest(provided, settings.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")


@router.post("/replay/{delivery_id}")
async def replay(
    delivery_id: str,
    background: BackgroundTasks,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> JSONResponse:
    _require_token(x_admin_token)
    row = db.webhook_events.get(delivery_id)
    if not row:
        raise HTTPException(status_code=404, detail="delivery not found")
    payload = json.loads(row["payload_json"])
    background.add_task(_process, delivery_id, row["event_type"], row["action"], payload)
    return JSONResponse({"status": "replayed", "delivery_id": delivery_id})


@router.post("/reset")
def reset(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> JSONResponse:
    _require_token(x_admin_token)
    db.reset_all()
    return JSONResponse({"status": "reset"})
