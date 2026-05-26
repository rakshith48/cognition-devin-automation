"""Admin endpoints — replay a captured webhook, reset the DB for demo runs.

These bypass signature verification by design. They're intended to be
reachable only from the operator's machine; production deployments would
gate them behind network policy or auth middleware.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from app import db
from app.routes.webhook import _process

router = APIRouter(prefix="/admin")


@router.post("/replay/{delivery_id}")
async def replay(delivery_id: str, background: BackgroundTasks) -> JSONResponse:
    row = db.webhook_events.get(delivery_id)
    if not row:
        raise HTTPException(status_code=404, detail="delivery not found")
    payload = json.loads(row["payload_json"])
    background.add_task(_process, delivery_id, row["event_type"], row["action"], payload)
    return JSONResponse({"status": "replayed", "delivery_id": delivery_id})


@router.post("/reset")
def reset() -> JSONResponse:
    db.reset_all()
    return JSONResponse({"status": "reset"})
