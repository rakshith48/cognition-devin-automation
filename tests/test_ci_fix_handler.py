"""CI-fix handler — the six attribution guards (don't fire on someone
else's PR, don't loop forever, don't fight humans, don't double-spend on
webhook retries)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class _StubCreated:
    devin_session_id: str
    devin_url: str


class _StubDevin:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def create_session(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("simulated devin outage")
        return _StubCreated(
            devin_session_id=f"devin-cifix-{len(self.calls)}",
            devin_url=f"https://app.devin.ai/sessions/devin-cifix-{len(self.calls)}",
        )


@pytest.fixture
def stub_devin(monkeypatch, db_path):
    s = _StubDevin()
    from app.devin import factory
    monkeypatch.setattr(factory, "get_client", lambda: s)
    return s


@pytest.fixture
def stub_devin_failing(monkeypatch, db_path):
    s = _StubDevin(fail=True)
    from app.devin import factory
    monkeypatch.setattr(factory, "get_client", lambda: s)
    return s


@pytest.fixture
def stub_github(monkeypatch):
    """Patch github_client to return scripted responses without HTTP."""
    state: dict = {
        "pr": None,
        "commits": [],
        "logs": "fake log line\nanother line",
    }
    from app import github_client as gc

    def _get_pr(repo, number):
        return state["pr"]

    def _list_pr_commits(repo, number):
        return state["commits"]

    def _has_non_devin(commits):
        # Use the real function so we cover that logic, but reading the
        # scripted commits list.
        return gc.has_non_devin_commits(commits)

    def _get_logs(repo, run_id, *, tail_lines=200):
        return state["logs"]

    monkeypatch.setattr(gc, "get_pr", _get_pr)
    monkeypatch.setattr(gc, "list_pr_commits", _list_pr_commits)
    monkeypatch.setattr(gc, "get_workflow_run_failing_job_logs", _get_logs)
    # has_non_devin_commits stays the real implementation
    return state


def _wf_run(*, run_id=12345, event="pull_request", pr_number=42, repo="owner/repo"):
    return {
        "id": run_id,
        "event": event,
        "name": "CI",
        "repository": {"full_name": repo},
        "pull_requests": [{"number": pr_number}] if pr_number else [],
    }


def _seed_parent_session(repo: str, pr_url: str, *, fix_attempt: int = 0,
                          devin_id: str = "devin-parent-1") -> int:
    """Create a 'parent' Devin session row that ci_fix_handler will find."""
    from app import db
    work_key = f"issue:{repo}:1:devin-security"
    pk = db.sessions.try_reserve(
        work_key=work_key, trigger_type="cve_issue",
        trigger_ref="https://x/issues/1", issue_number=1,
        label="devin-security", fix_attempt_number=fix_attempt,
    )
    db.sessions.update(
        pk,
        devin_session_id=devin_id,
        devin_url=f"https://app.devin.ai/sessions/{devin_id}",
        prompt_snapshot="(parent prompt)",
        pr_url=pr_url,
        status="completed",
    )
    return pk


# ------------------------------------------------------------------------------
# Guards
# ------------------------------------------------------------------------------

def test_non_pr_workflow_skipped(stub_devin, stub_github):
    from app import handlers
    res = handlers.handle_ci_failure(_wf_run(event="schedule"))
    assert res.startswith("skipped:not_pr_triggered")
    assert len(stub_devin.calls) == 0


def test_no_pr_in_payload_skipped(stub_devin, stub_github):
    from app import handlers
    res = handlers.handle_ci_failure(_wf_run(pr_number=None))
    assert res.startswith("skipped:no_pr_in_payload")


def test_non_devin_branch_skipped(stub_devin, stub_github):
    """A failing CI on a human-opened PR must NOT spawn a Devin session."""
    from app import handlers
    stub_github["pr"] = {
        "html_url": "https://x/owner/repo/pull/42",
        "head": {"ref": "feature/some-human-branch"},
    }
    res = handlers.handle_ci_failure(_wf_run())
    assert res == "skipped:not_devin_branch:feature/some-human-branch"
    assert len(stub_devin.calls) == 0


def test_no_tracked_session_skipped(stub_devin, stub_github):
    """devin/ branch that ISN'T one of ours (no parent in DB) — skip."""
    from app import handlers
    stub_github["pr"] = {
        "html_url": "https://x/owner/repo/pull/42",
        "head": {"ref": "devin/some-other-task"},
    }
    res = handlers.handle_ci_failure(_wf_run())
    assert res.startswith("skipped:no_tracked_session_for_pr")
    assert len(stub_devin.calls) == 0


def test_max_fix_attempts_skipped(stub_devin, stub_github):
    """Three strikes: a human takes over after fix_attempt 3.

    Seeds a child session at the cap (not the parent — parent fix_attempt
    is always 0). The chain query MAX(fix_attempt_number) across parent
    + children should return the cap, and the handler should skip."""
    from app import db, handlers, settings
    pr_url = "https://x/owner/repo/pull/42"
    stub_github["pr"] = {"html_url": pr_url, "head": {"ref": "devin/cve-x"}}
    stub_github["commits"] = [
        {"committer": {"login": "devin-ai-integration[bot]"}, "author": {}},
    ]
    parent_pk = _seed_parent_session("owner/repo", pr_url, devin_id="devin-parent-cap")
    # Simulate that MAX_FIX_ATTEMPTS child fix-sessions have already run.
    db.sessions.try_reserve(
        work_key="ci_fix:owner/repo:88",  # arbitrary historical run id
        trigger_type="ci_failure",
        trigger_ref=pr_url,
        issue_number=42,
        label="devin-ci-fix",
        parent_devin_session_id="devin-parent-cap",
        fix_attempt_number=settings.MAX_FIX_ATTEMPTS,
    )
    res = handlers.handle_ci_failure(_wf_run())
    assert res.startswith("skipped:max_fix_attempts")
    assert len(stub_devin.calls) == 0


def test_fix_attempt_counter_walks_the_chain(stub_devin, stub_github):
    """Regression: multiple CI failures on the same PR must increment
    fix_attempt_number across the chain, not stay at 1 forever.

    The original bug: handler used parent.fix_attempt_number + 1, but
    parent never gets incremented (only children do), so every fresh
    workflow_run.id past the first spawned another attempt-1 child."""
    from app import db, handlers
    pr_url = "https://x/owner/repo/pull/42"
    stub_github["pr"] = {"html_url": pr_url, "head": {"ref": "devin/cve-x"}}
    stub_github["commits"] = [
        {"committer": {"login": "devin-ai-integration[bot]"}, "author": {}},
    ]
    _seed_parent_session("owner/repo", pr_url, devin_id="devin-parent-chain")

    # Three distinct workflow_run.ids → three distinct child sessions.
    assert handlers.handle_ci_failure(_wf_run(run_id=1001)).startswith("created_session")
    assert handlers.handle_ci_failure(_wf_run(run_id=1002)).startswith("created_session")
    assert handlers.handle_ci_failure(_wf_run(run_id=1003)).startswith("created_session")

    children = sorted(
        (r for r in db.sessions.list_recent() if r.trigger_type == "ci_failure"),
        key=lambda r: r.fix_attempt_number,
    )
    assert [c.fix_attempt_number for c in children] == [1, 2, 3], \
        f"Expected 1,2,3 — got {[c.fix_attempt_number for c in children]}"

    # The 4th failure must be blocked by the cap.
    res = handlers.handle_ci_failure(_wf_run(run_id=1004))
    assert res == "skipped:max_fix_attempts:3"
    assert len(stub_devin.calls) == 3, "No new Devin call on the 4th failure"


def test_human_commits_skipped(stub_devin, stub_github):
    """If a human pushed to the branch, hands off."""
    from app import handlers
    pr_url = "https://x/owner/repo/pull/42"
    stub_github["pr"] = {"html_url": pr_url, "head": {"ref": "devin/cve-x"}}
    stub_github["commits"] = [
        {"committer": {"login": "devin-ai-integration[bot]"}, "author": {}},
        {"committer": {"login": "some-human"}, "author": {"login": "some-human"}},
    ]
    _seed_parent_session("owner/repo", pr_url)
    res = handlers.handle_ci_failure(_wf_run())
    assert res.startswith("skipped:human_commits_on_branch")
    assert len(stub_devin.calls) == 0


def test_workflow_rerun_dedup(stub_devin, stub_github):
    """Webhook redelivery for the same workflow_run.id must spawn once."""
    from app import handlers
    pr_url = "https://x/owner/repo/pull/42"
    stub_github["pr"] = {"html_url": pr_url, "head": {"ref": "devin/cve-x"}}
    stub_github["commits"] = [
        {"committer": {"login": "devin-ai-integration[bot]"}, "author": {}},
    ]
    _seed_parent_session("owner/repo", pr_url)
    res1 = handlers.handle_ci_failure(_wf_run(run_id=999))
    res2 = handlers.handle_ci_failure(_wf_run(run_id=999))
    assert res1.startswith("created_session"), res1
    assert res2.startswith("skipped:already_reserved"), res2
    assert len(stub_devin.calls) == 1


def test_happy_path_spawns_child_with_parent_link(stub_devin, stub_github):
    from app import db, handlers
    pr_url = "https://x/owner/repo/pull/42"
    stub_github["pr"] = {"html_url": pr_url, "head": {"ref": "devin/cve-x"}}
    stub_github["commits"] = [
        {"committer": {"login": "devin-ai-integration[bot]"}, "author": {}},
    ]
    _seed_parent_session("owner/repo", pr_url, devin_id="devin-parent-99")
    res = handlers.handle_ci_failure(_wf_run(run_id=1234))
    assert res.startswith("created_session")
    # Devin was told who the parent is — natively wires the chain
    call = stub_devin.calls[0]
    assert call["parent_session_id"] == "devin-parent-99"
    assert call["repos"] == [__import__("app").settings.FORK_REPO]
    # New child row has fix_attempt_number=1
    children = [r for r in db.sessions.list_recent()
                if r.trigger_type == "ci_failure"]
    assert len(children) == 1
    assert children[0].fix_attempt_number == 1
    assert children[0].parent_devin_session_id == "devin-parent-99"


def test_devin_failure_marks_reservation_failed(stub_devin_failing, stub_github):
    from app import db, handlers
    pr_url = "https://x/owner/repo/pull/42"
    stub_github["pr"] = {"html_url": pr_url, "head": {"ref": "devin/cve-x"}}
    stub_github["commits"] = [
        {"committer": {"login": "devin-ai-integration[bot]"}, "author": {}},
    ]
    _seed_parent_session("owner/repo", pr_url)
    res = handlers.handle_ci_failure(_wf_run(run_id=555))
    assert res.startswith("error:devin_create_failed")
    # The reservation row exists and is marked failed (not orphaned)
    children = [r for r in db.sessions.list_recent()
                if r.trigger_type == "ci_failure"]
    assert len(children) == 1
    assert children[0].status == "failed"
    assert "devin_create_failed" in (children[0].error_message or "")


# ------------------------------------------------------------------------------
# has_non_devin_commits unit
# ------------------------------------------------------------------------------

def test_has_non_devin_commits_detects_human():
    from app.github_client import has_non_devin_commits
    assert has_non_devin_commits([
        {"committer": {"login": "devin-ai-integration[bot]"}, "author": {}},
        {"committer": {"login": "human-user"}, "author": {"login": "human-user"}},
    ]) is True


def test_has_non_devin_commits_all_devin_returns_false():
    from app.github_client import has_non_devin_commits
    assert has_non_devin_commits([
        {"committer": {"login": "devin-ai-integration[bot]"}, "author": {}},
        {"committer": {"login": "devin[bot]"}, "author": {}},
    ]) is False


def test_has_non_devin_commits_missing_committer_falls_through_to_author():
    from app.github_client import has_non_devin_commits
    assert has_non_devin_commits([
        {"committer": None, "author": {"login": "human-user"}},
    ]) is True


# ------------------------------------------------------------------------------
# Work key format
# ------------------------------------------------------------------------------

def test_make_ci_fix_work_key_includes_run_id():
    from app.db.sessions import make_ci_fix_work_key
    # Different runs → different keys (so a workflow re-run DOES spawn a new
    # fix session, while a redelivery of the same run does not)
    assert make_ci_fix_work_key("a/b", 1) != make_ci_fix_work_key("a/b", 2)
    assert make_ci_fix_work_key("a/b", 1) == "ci_fix:a/b:1"
