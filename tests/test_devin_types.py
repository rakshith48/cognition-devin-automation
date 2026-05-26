"""Devin status mapping — every known status routes to a known bucket;
unknown statuses route to needs_attention (kept ACTIVE for polling)."""
from __future__ import annotations

from app.devin.types import (
    STATUS_MAP,
    UNKNOWN_STATUS,
    PullRequestRef,
    SessionDetails,
    map_status,
)


def test_all_known_statuses_map_explicitly():
    """Every status documented in Devin v3 must have a mapping; we don't
    silently fall back to UNKNOWN_STATUS for known values."""
    documented = {"new", "claimed", "running", "exit", "error", "suspended", "resuming"}
    for s in documented:
        assert s in STATUS_MAP, f"Documented status '{s}' missing from STATUS_MAP"


def test_unknown_status_routes_to_needs_attention():
    """If Devin adds a new status, it should NOT silently drop. It must land
    in needs_attention so the dashboard surfaces it for a human."""
    assert map_status("some_brand_new_status_v4") == UNKNOWN_STATUS
    assert UNKNOWN_STATUS == "needs_attention"


def test_terminal_statuses_consistent():
    """exit/error should map to terminal buckets; running variants stay active."""
    assert map_status("exit") == "completed"
    assert map_status("error") == "failed"
    assert map_status("running") == "running"
    assert map_status("resuming") == "running"


def test_session_details_parses_full_response():
    """Realistic response with PR and ACUs."""
    data = {
        "session_id": "devin-abc",
        "status": "exit",
        "status_detail": "completed",
        "acus_consumed": 4.2,
        "pull_requests": [
            {"pr_url": "https://github.com/x/y/pull/1", "pr_state": "open"},
        ],
        "parent_session_id": "devin-parent",
        "child_session_ids": ["devin-child-1"],
        "title": "Some session",
    }
    sd = SessionDetails.from_api_response(data)
    assert sd.status == "completed"
    assert sd.raw_status == "exit"
    assert sd.acus_consumed == 4.2
    assert sd.first_pr_url == "https://github.com/x/y/pull/1"
    assert sd.parent_session_id == "devin-parent"
    assert sd.child_session_ids == ["devin-child-1"]


def test_session_details_handles_missing_optionals():
    """Most fields are optional in the get-session response."""
    sd = SessionDetails.from_api_response({"session_id": "s", "status": "new"})
    assert sd.status == "pending"
    assert sd.acus_consumed == 0
    assert sd.pull_requests == []
    assert sd.first_pr_url is None


def test_session_details_filters_prs_without_url():
    """An entry in pull_requests with no pr_url is malformed; skip it."""
    data = {
        "session_id": "s", "status": "exit",
        "pull_requests": [{"pr_state": "open"}, {"pr_url": "https://x/y/pull/1"}],
    }
    sd = SessionDetails.from_api_response(data)
    assert len(sd.pull_requests) == 1
    assert sd.pull_requests[0].pr_url == "https://x/y/pull/1"
