"""Health endpoint. Used by docker-compose healthchecks and human eyeballs."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import db, devin, settings

router = APIRouter()


@router.get("/healthz")
def healthz() -> JSONResponse:
    problems = settings.validate_for_runtime()
    db_ok = db.healthcheck()

    # Only probe Devin if creds are configured — avoids confusing health output
    # before the operator has filled in .env.
    if settings.DEVIN_API_KEY and settings.DEVIN_ORG_ID:
        try:
            devin_ok = devin.factory.get_client().healthcheck()
        except Exception:  # noqa: BLE001
            devin_ok = False
    else:
        devin_ok = None  # "not configured"

    body = {
        "db": "ok" if db_ok else "fail",
        "devin": ("ok" if devin_ok else "fail") if devin_ok is not None else "not_configured",
        "config": "ok" if not problems else "warn",
        "config_issues": problems,
    }
    ok = db_ok and (devin_ok is not False)  # None acceptable; False is not
    return JSONResponse(body, status_code=200 if ok else 503)
