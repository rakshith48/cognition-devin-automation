"""Persistence layer. Public surface for the rest of the app.

Two entities: webhook_events (idempotency log) and sessions (Devin work).
Re-exports the namespaces so callers write `db.sessions.insert(...)` rather
than chasing through submodules.
"""
from app.db import sessions, webhook_events
from app.db.connection import DB_PATH, conn, healthcheck, init_db, reset_all
from app.db.sessions import ACTIVE_STATUSES, TERMINAL_STATUSES, SessionRow

__all__ = [
    "sessions",
    "webhook_events",
    "conn",
    "DB_PATH",
    "init_db",
    "healthcheck",
    "reset_all",
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "SessionRow",
]
