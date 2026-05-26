"""HMAC verification for GitHub webhooks.

Isolated so the security-critical comparison can be unit-tested without
spinning up a server, and so future signers (Slack, Linear, etc.) share
one well-reviewed primitive.

GitHub's contract: header is `X-Hub-Signature-256: sha256=<hex_digest>`,
where the digest is HMAC-SHA256 of the raw body using the shared secret.
Constant-time comparison is mandatory to defeat timing attacks.
"""
from __future__ import annotations

import hashlib
import hmac

_PREFIX = "sha256="


def verify_github_signature(body: bytes, header_value: str | None, secret: str) -> bool:
    if not secret:
        return False
    if not header_value or not header_value.startswith(_PREFIX):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.removeprefix(_PREFIX))


def sign_github_payload(body: bytes, secret: str) -> str:
    """Produce a header value for a given body — used in manual tests."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return _PREFIX + digest
