"""Queueing behavior: events that hit the concurrency cap are NOT dropped
— they stay in webhook_events with handler_result='queued:*', and the
poller's drain_queue picks them up on subsequent ticks once capacity frees."""
from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class _StubCreated:
    devin_session_id: str
    devin_url: str


class _StubDevin:
    def __init__(self):
        self.calls = []

    def create_session(self, **kwargs):
        self.calls.append(kwargs)
        return _StubCreated(
            devin_session_id=f"devin-stub-{len(self.calls)}",
            devin_url=f"https://x/{len(self.calls)}",
        )

    def terminate_session(self, devin_session_id: str) -> None:
        pass


@pytest.fixture
def stub_devin(monkeypatch, db_path):
    s = _StubDevin()
    from app.devin import factory
    monkeypatch.setattr(factory, "get_client", lambda: s)
    return s


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


def _record_event_with_result(delivery_id, payload, result):
    from app import db
    db.webhook_events.record(delivery_id, "issues", "opened", payload)
    db.webhook_events.mark_processed(delivery_id, result)


def test_handler_returns_queued_when_cap_hit(stub_devin, monkeypatch):
    """When MAX_CONCURRENT is reached, the handler queues instead of
    skipping — webhook_events.handler_result starts with 'queued:'."""
    from app import handlers, settings
    monkeypatch.setattr(settings, "MAX_CONCURRENT_SESSIONS", 1)

    # Fill the one slot.
    handlers.handle_security_issue(_cve_issue(number=1))

    # Second issue: cap is reached → must be queued, not skipped.
    result = handlers.handle_security_issue(_cve_issue(number=2))
    assert result.startswith("queued:concurrency_cap:"), result
    assert "issue:owner/repo:2:devin-security" in result
    assert len(stub_devin.calls) == 1  # No new Devin call


def test_drain_queue_picks_up_when_capacity_frees(stub_devin, monkeypatch):
    """Once active sessions complete and capacity opens, drain_queue
    re-dispatches the queued event and Devin is called."""
    from app import db, dispatcher, orchestrator, settings
    monkeypatch.setattr(settings, "MAX_CONCURRENT_SESSIONS", 1)

    # First issue takes the slot.
    handlers_result = dispatcher.dispatch("issues", "opened",
                                          {"action": "opened", "issue": _cve_issue(1)})
    assert handlers_result.startswith("created_session"), handlers_result

    # Second event hits cap → recorded as queued in webhook_events.
    payload2 = {"action": "opened", "issue": _cve_issue(2)}
    db.webhook_events.record("d2", "issues", "opened", payload2)
    res2 = dispatcher.dispatch("issues", "opened", payload2)
    db.webhook_events.mark_processed("d2", res2)
    assert res2.startswith("queued:")

    # Drain with no capacity: nothing happens.
    summary = orchestrator.drain_queue()
    assert summary == {"queue_seen": 0, "queue_retried": 0, "queue_dispatched": 0}

    # Free up the slot by completing the first session.
    first = db.sessions.list_recent()[-1]
    db.sessions.update(first.id, status="completed", pr_url="https://x/pull/1")

    # Now drain → queued event dispatches.
    summary = orchestrator.drain_queue()
    assert summary["queue_seen"] == 1
    assert summary["queue_dispatched"] == 1
    assert len(stub_devin.calls) == 2  # Second Devin call now happened

    # Webhook event's handler_result has been updated to the new result.
    row = db.webhook_events.get("d2")
    assert not row["handler_result"].startswith("queued:")
    assert row["handler_result"].startswith("created_session")


def test_drain_queue_respects_capacity(stub_devin, monkeypatch):
    """drain_queue must not pull more events than current available
    capacity — preserves the cap as the load-bearing safety rail."""
    from app import db, orchestrator, settings
    monkeypatch.setattr(settings, "MAX_CONCURRENT_SESSIONS", 2)

    # Three events all queued, zero active.
    for n in (1, 2, 3):
        payload = {"action": "opened", "issue": _cve_issue(n)}
        _record_event_with_result(
            f"d{n}", payload, f"queued:concurrency_cap:issue:owner/repo:{n}:devin-security",
        )

    # Capacity is 2 — drain should retry only 2.
    summary = orchestrator.drain_queue()
    assert summary["queue_seen"] == 2
    assert summary["queue_dispatched"] == 2
    assert len(stub_devin.calls) == 2

    # Third event still queued for next tick.
    remaining = db.webhook_events.list_queued()
    assert len(remaining) == 1
    assert remaining[0]["delivery_id"] == "d3"


def test_daily_cap_is_NOT_queued(stub_devin, monkeypatch):
    """Daily cap is an intentional throttle. We should NOT queue across
    day boundaries — that's a different operational decision."""
    from app import handlers, settings
    monkeypatch.setattr(settings, "MAX_CONCURRENT_SESSIONS", 100)
    monkeypatch.setattr(settings, "MAX_SESSIONS_PER_DAY", 1)

    handlers.handle_security_issue(_cve_issue(1))   # consumes daily cap
    result = handlers.handle_security_issue(_cve_issue(2))
    assert result.startswith("skipped:daily_cap"), result
    # Not queued — the event was a hard skip.
    assert not result.startswith("queued:")
