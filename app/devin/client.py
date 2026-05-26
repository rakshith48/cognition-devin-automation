"""Devin v3 session client — the verbs we use against Devin.

Composes DevinTransport (HTTP concerns) with SessionDetails (data shape).
Handlers depend on this. Nothing in this file knows about HTTP retries or
JSON parsing — those are pushed down to transport and types respectively.
"""
from __future__ import annotations

import logging
from typing import Protocol

from app.devin.http import DevinTransport
from app.devin.types import SessionCreated, SessionDetails

logger = logging.getLogger(__name__)


class DevinClient(Protocol):
    def create_session(
        self,
        *,
        prompt: str,
        repos: list[str] | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        max_acu_limit: int | None = None,
        devin_mode: str | None = None,
        parent_session_id: str | None = None,
    ) -> SessionCreated: ...

    def get_session(self, session_id: str) -> SessionDetails: ...
    def send_message(self, session_id: str, message: str) -> None: ...
    def terminate_session(self, session_id: str) -> None: ...


class HttpDevinClient:
    def __init__(self, transport: DevinTransport) -> None:
        self._t = transport

    def create_session(
        self,
        *,
        prompt: str,
        repos: list[str] | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        max_acu_limit: int | None = None,
        devin_mode: str | None = None,
        parent_session_id: str | None = None,
    ) -> SessionCreated:
        body: dict = {"prompt": prompt}
        if repos:
            body["repos"] = repos
        if title:
            body["title"] = title[:200]
        if tags:
            body["tags"] = tags[:50]
        if max_acu_limit is not None:
            body["max_acu_limit"] = max_acu_limit
        if devin_mode:
            body["devin_mode"] = devin_mode
        if parent_session_id:
            body["parent_session_id"] = parent_session_id
        data = self._t.request("POST", "/sessions", json=body).json()
        return SessionCreated(
            devin_session_id=data["session_id"], devin_url=data["url"]
        )

    def get_session(self, session_id: str) -> SessionDetails:
        data = self._t.request("GET", f"/sessions/{session_id}").json()
        return SessionDetails.from_api_response(data)

    def send_message(self, session_id: str, message: str) -> None:
        self._t.request(
            "POST", f"/sessions/{session_id}/messages", json={"message": message}
        )

    def terminate_session(self, session_id: str) -> None:
        try:
            self._t.request(
                "POST", "/sessions/archive", json={"session_ids": [session_id]}
            )
        except RuntimeError as e:
            if any(s in str(e).lower() for s in ("already", "exited", "archived")):
                logger.info("Session %s already terminated", session_id)
                return
            raise

    def healthcheck(self) -> bool:
        """Cheap auth-and-reachability probe — hits the session list endpoint
        with limit=1. Returns True iff we get 2xx, meaning the token can read
        this org's sessions. NB: hitting GET /sessions/<fake-id> would 403 even
        with valid creds (Devin returns 403 for unknown IDs to prevent
        enumeration), so the list endpoint is the right probe."""
        try:
            self._t.request("GET", "/sessions", params={"limit": 1})
            return True
        except Exception:  # noqa: BLE001
            return False
