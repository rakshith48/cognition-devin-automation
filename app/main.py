"""FastAPI app entrypoint. Wires lifespan + routers; no business logic here."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db, devin, orchestrator, settings
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
    if settings.ENABLE_ADMIN_ROUTES and not settings.ADMIN_TOKEN:
        logger.error("ENABLE_ADMIN_ROUTES=true but ADMIN_TOKEN is empty — routes will 503")

    # Start the background poller. Only when creds are configured —
    # otherwise it would log a flood of auth errors and waste cycles.
    poller_task: asyncio.Task | None = None
    stop_event = asyncio.Event()
    if settings.DEVIN_API_KEY and settings.DEVIN_ORG_ID:
        poller_task = asyncio.create_task(orchestrator.run_loop(stop_event))
        logger.info("Background poller started")
    else:
        logger.warning("Devin creds missing — poller not started")

    try:
        yield
    finally:
        stop_event.set()
        if poller_task is not None:
            try:
                await asyncio.wait_for(poller_task, timeout=5.0)
            except TimeoutError:
                poller_task.cancel()
        devin.factory.reset()
        # GitHub client uses the same singleton pattern — release its sockets too.
        from app import github_client
        github_client.reset()
        logger.info("Devin + GitHub clients released on shutdown")


app = FastAPI(
    title="Devin Maintenance Orchestrator",
    description="Event-driven remediation control plane for dependency vulnerabilities.",
    lifespan=lifespan,
)
app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(metrics.router)
if settings.ENABLE_ADMIN_ROUTES:
    app.include_router(admin.router)
    logger.info("Admin routes mounted (ENABLE_ADMIN_ROUTES=true)")
