"""Devin v3 API data types and status translation.

The status mapping is the boundary translation that keeps Devin's vocabulary
out of the rest of the system. Add a status here, not in handlers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Devin v3 status enum → our internal status.
STATUS_MAP: dict[str, str] = {
    "new": "pending",
    "claimed": "pending",
    "running": "running",
    "exit": "completed",     # success-or-noop; PR presence disambiguates
    "error": "failed",
    "suspended": "cancelled",
    "resuming": "running",
}

# Sentinel for a Devin status we don't recognize — kept ACTIVE so the poller
# keeps surfacing it and the dashboard flags it for human attention, rather
# than silently dropping a session if Devin adds a new status enum.
UNKNOWN_STATUS = "needs_attention"

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timeout"})


def map_status(raw: str) -> str:
    return STATUS_MAP.get(raw, UNKNOWN_STATUS)


@dataclass(frozen=True)
class SessionCreated:
    devin_session_id: str
    devin_url: str  # https://app.devin.ai/sessions/{id}


@dataclass(frozen=True)
class PullRequestRef:
    pr_url: str
    pr_state: str | None


@dataclass
class SessionDetails:
    devin_session_id: str
    status: str                            # our internal status
    raw_status: str                        # Devin's raw enum, kept for debugging
    status_detail: str | None
    acus_consumed: float
    pull_requests: list[PullRequestRef] = field(default_factory=list)
    parent_session_id: str | None = None
    child_session_ids: list[str] = field(default_factory=list)
    created_at: int | None = None
    updated_at: int | None = None
    title: str | None = None

    @property
    def first_pr_url(self) -> str | None:
        return self.pull_requests[0].pr_url if self.pull_requests else None

    @classmethod
    def from_api_response(cls, data: dict) -> "SessionDetails":
        raw_status = data.get("status") or "unknown"
        prs = [
            PullRequestRef(pr_url=p["pr_url"], pr_state=p.get("pr_state"))
            for p in (data.get("pull_requests") or [])
            if p.get("pr_url")
        ]
        return cls(
            devin_session_id=data["session_id"],
            status=map_status(raw_status),
            raw_status=raw_status,
            status_detail=data.get("status_detail"),
            acus_consumed=float(data.get("acus_consumed") or 0),
            pull_requests=prs,
            parent_session_id=data.get("parent_session_id"),
            child_session_ids=list(data.get("child_session_ids") or []),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            title=data.get("title"),
        )
