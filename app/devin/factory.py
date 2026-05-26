"""Lazy singleton accessor for the Devin client.

Why lazy: the FastAPI app should boot even with missing Devin credentials so
that /healthz can report the problem instead of the process crashing. Validation
happens the first time a handler actually needs to talk to Devin.

Why singleton: opening a fresh httpx.Client per handler call would leak sockets
and add ~100ms per request for TLS handshake. One client, shared across the
process, closed at app shutdown.
"""
from __future__ import annotations

import logging
import threading

from app import settings
from app.devin.client import HttpDevinClient
from app.devin.http import DevinTransport

logger = logging.getLogger(__name__)

_client: HttpDevinClient | None = None
_transport: DevinTransport | None = None
_lock = threading.Lock()


def get_client() -> HttpDevinClient:
    global _client, _transport
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _transport = DevinTransport(
                api_key=settings.DEVIN_API_KEY,
                org_id=settings.DEVIN_ORG_ID,
                base_url=settings.DEVIN_BASE_URL,
            )
            _client = HttpDevinClient(_transport)
            logger.info("Initialized Devin client for org=%s", settings.DEVIN_ORG_ID)
    return _client


def reset() -> None:
    """For tests; also called from app shutdown."""
    global _client, _transport
    if _transport is not None:
        _transport.close()
    _client = None
    _transport = None
