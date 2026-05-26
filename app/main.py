"""FastAPI app entrypoint. Wires lifespan + routers; no business logic here."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db, settings
from app.routes import admin, health, metrics, webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("automation")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    for problem in settings.validate_for_runtime():
        logger.warning("Config issue: %s", problem)
    yield


app = FastAPI(title="Devin Maintenance Orchestrator", lifespan=lifespan)
app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(metrics.router)
app.include_router(admin.router)
