"""Read-side endpoints: aggregated metrics and raw session list."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import db, metrics, serializers

router = APIRouter()


@router.get("/metrics")
def metrics_endpoint() -> JSONResponse:
    rows = db.sessions.list_recent(limit=10_000)
    return JSONResponse(metrics.compute_dashboard_metrics(rows))


@router.get("/sessions")
def sessions_endpoint(limit: int = 100) -> JSONResponse:
    rows = db.sessions.list_recent(limit=limit)
    return JSONResponse([serializers.session_to_dict(r) for r in rows])
