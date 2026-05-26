"""HMAC signature verification — security-critical, must be bulletproof."""
from __future__ import annotations

from app.signature import sign_github_payload, verify_github_signature

SECRET = "test-secret-xyz"
BODY = b'{"action":"opened","number":1}'


def test_round_trip():
    sig = sign_github_payload(BODY, SECRET)
    assert verify_github_signature(BODY, sig, SECRET) is True


def test_wrong_secret_rejected():
    sig = sign_github_payload(BODY, SECRET)
    assert verify_github_signature(BODY, sig, "other-secret") is False


def test_tampered_body_rejected():
    sig = sign_github_payload(BODY, SECRET)
    assert verify_github_signature(b'{"different":true}', sig, SECRET) is False


def test_missing_header_rejected():
    assert verify_github_signature(BODY, None, SECRET) is False
    assert verify_github_signature(BODY, "", SECRET) is False


def test_wrong_prefix_rejected():
    # GitHub uses sha256= prefix; we should reject sha1= or unprefixed.
    sig = sign_github_payload(BODY, SECRET).removeprefix("sha256=")
    assert verify_github_signature(BODY, sig, SECRET) is False
    assert verify_github_signature(BODY, "sha1=" + sig, SECRET) is False


def test_empty_secret_rejected():
    """An empty secret would otherwise verify ANY body — explicit reject."""
    sig = sign_github_payload(BODY, "")
    assert verify_github_signature(BODY, sig, "") is False


def test_signature_format():
    """Output format matches GitHub's: sha256=<64 hex chars>."""
    sig = sign_github_payload(BODY, SECRET)
    assert sig.startswith("sha256=")
    assert len(sig) == len("sha256=") + 64
