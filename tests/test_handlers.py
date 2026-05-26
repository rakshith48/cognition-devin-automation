"""Handler boundary tests with a stubbed Devin client.

Verifies the safety rails (concurrency, daily cap, dedupe via work_key) and
the parse-validate-reserve-call-activate state machine.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class _StubCreated:
    devin_session_id: str
    devin_url: str


class _StubClient:
    """Minimal DevinClient implementation for handler tests."""

    def __init__(self, *, should_fail: bool = False):
        self.should_fail = should_fail
        self.calls: list[dict] = []

    def create_session(self, **kwargs):
        self.calls.append(kwargs)
        if self.should_fail:
            raise RuntimeError("simulated Devin outage")
        return _StubCreated(
            devin_session_id=f"devin-stub-{len(self.calls)}",
            devin_url=f"https://app.devin.ai/sessions/devin-stub-{len(self.calls)}",
        )

    def get_session(self, session_id):
        raise NotImplementedError

    def send_message(self, session_id, message):
        raise NotImplementedError

    def terminate_session(self, session_id):
        raise NotImplementedError


@pytest.fixture
def stub_devin(monkeypatch, db_path):
    """Replace devin.factory.get_client with a stub."""
    stub = _StubClient()
    from app.devin import factory
    monkeypatch.setattr(factory, "get_client", lambda: stub)
    return stub


@pytest.fixture
def stub_devin_failing(monkeypatch, db_path):
    stub = _StubClient(should_fail=True)
    from app.devin import factory
    monkeypatch.setattr(factory, "get_client", lambda: stub)
    return stub


def _cve_issue(number=1):
    return {
        "number": number,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "repository_url": "https://api.github.com/repos/owner/repo",
        "labels": [{"name": "devin-remediate"}, {"name": "devin-security"}],
        "body": (
            "## Vulnerability\n"
            "- **CVE:** CVE-2026-1\n"
            "- **Package:** flask\n"
            "- **Installed:** 2.3.3\n"
            "- **Fix version:** 3.1.3\n"
            "- **Severity:** LOW\n"
            "- **Summary:** test\n"
        ),
    }


def test_happy_path_spawns_one_session(stub_devin):
    from app import db, handlers
    res = handlers.handle_security_issue(_cve_issue())
    assert res.startswith("created_session:")
    assert len(stub_devin.calls) == 1
    rows = db.sessions.list_recent()
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].devin_session_id == "devin-stub-1"
    assert rows[0].work_key == "issue:owner/repo:1:devin-security"


def test_duplicate_event_does_not_spawn_second_session(stub_devin):
    """Two webhooks for the same issue + label → exactly one Devin call."""
    from app import handlers
    res1 = handlers.handle_security_issue(_cve_issue())
    res2 = handlers.handle_security_issue(_cve_issue())
    assert res1.startswith("created_session:")
    assert res2.startswith("skipped:already_reserved:")
    assert len(stub_devin.calls) == 1, "Devin must not be called twice"


def test_missing_fix_version_skips_before_devin(stub_devin):
    """Malformed issue body should never reach Devin."""
    from app import handlers
    issue = _cve_issue()
    issue["body"] = issue["body"].replace("- **Fix version:** 3.1.3\n", "")
    res = handlers.handle_security_issue(issue)
    assert res.startswith("skipped:missing_fields"), res
    assert "fix_version" in res
    assert len(stub_devin.calls) == 0, "Must not call Devin with missing fields"


def test_concurrency_cap_blocks_extra_sessions(stub_devin, monkeypatch):
    """When MAX_CONCURRENT is reached, no new session even with valid input."""
    from app import handlers, settings
    monkeypatch.setattr(settings, "MAX_CONCURRENT_SESSIONS", 1)
    handlers.handle_security_issue(_cve_issue(number=1))
    res = handlers.handle_security_issue(_cve_issue(number=2))
    assert res == "skipped:concurrency_cap:1"
    assert len(stub_devin.calls) == 1


def test_devin_failure_marks_reservation_failed(stub_devin_failing):
    """If Devin call raises, the reservation row stays visible as 'failed'
    (not orphaned, not silently dropped)."""
    from app import db, handlers
    res = handlers.handle_security_issue(_cve_issue())
    assert res.startswith("error:devin_create_failed")
    rows = db.sessions.list_recent()
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert "devin_create_failed" in (rows[0].error_message or "")
    assert rows[0].devin_session_id is None


def test_non_cve_body_skipped(stub_devin):
    from app import handlers
    issue = _cve_issue()
    issue["body"] = "this is a normal issue, no structured fields"
    res = handlers.handle_security_issue(issue)
    assert res.startswith("skipped:not_a_cve_issue")
    assert len(stub_devin.calls) == 0
