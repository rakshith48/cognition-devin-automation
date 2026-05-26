"""Health endpoint. Used by docker-compose healthchecks and human eyeballs."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import db, settings

router = APIRouter()


@router.get("/healthz")
def healthz() -> JSONResponse:
    problems = settings.validate_for_runtime()
    body = {
        "db": "ok" if db.healthcheck() else "fail",
        "config": "ok" if not problems else "warn",
        "config_issues": problems,
    }
    return JSONResponse(body, status_code=200 if body["db"] == "ok" else 503)
