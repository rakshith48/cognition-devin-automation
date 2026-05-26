"""HTTP transport for the Devin v3 API.

Owns: connection lifecycle, retry/backoff, rate-limit handling.
Knows nothing about session shape — that's types.py + client.py.
"""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)


class DevinTransport:
    """Thin wrapper over httpx.Client with retries and 429/5xx handling."""

    def __init__(
        self,
        api_key: str,
        org_id: str,
        base_url: str = "https://api.devin.ai/v3",
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key.startswith(("cog_", "apk_")):
            raise ValueError(
                "DEVIN_API_KEY should start with 'cog_' (service user) "
                "or 'apk_' (personal). Check env."
            )
        if not org_id.startswith("org-"):
            raise ValueError("DEVIN_ORG_ID should start with 'org-'")
        self._base = f"{base_url.rstrip('/')}/organizations/{org_id}"
        self._client = httpx.Client(
            base_url=self._base,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        self._max_retries = max_retries

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DevinTransport:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = self._client.request(method, path, **kwargs)
                if resp.status_code == 429:
                    delay = float(resp.headers.get("Retry-After", 2**attempt))
                    logger.warning("Devin rate-limited; sleeping %.1fs", delay)
                    time.sleep(delay)
                    continue
                if 500 <= resp.status_code < 600:
                    delay = 2**attempt
                    logger.warning(
                        "Devin %d on %s %s; retry in %ds",
                        resp.status_code, method, path, delay,
                    )
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning("Devin transport error: %s (attempt %d)", exc, attempt + 1)
                time.sleep(2**attempt)
        raise RuntimeError(
            f"Devin API {method} {path} failed after {self._max_retries} attempts: {last_exc}"
        )
