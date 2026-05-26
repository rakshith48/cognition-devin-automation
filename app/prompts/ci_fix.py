"""CI-failure follow-up prompt — used when a Devin-authored PR fails CI.

The child session is created with `parent_session_id=<original>` so Devin's
own UI surfaces the chain. The prompt re-supplies the original task context
because Devin sessions are otherwise independent.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CiFixContext:
    pr_url: str
    branch: str
    workflow_name: str
    failure_logs_tail: str   # last N lines of the failing job's log
    parent_prompt: str
    attempt_number: int      # 1-indexed; cap enforced at the handler layer
    max_attempts: int


def build_ci_fix_prompt(ctx: CiFixContext) -> str:
    return f"""A pull request you opened is failing CI. Your job is to push commits to the
existing branch to make CI green.

PR: {ctx.pr_url}
Branch: {ctx.branch}
Failing workflow: {ctx.workflow_name}

FAILURE LOGS (last lines):
```
{ctx.failure_logs_tail}
```

ORIGINAL TASK CONTEXT (what you were asked to do in the parent session):
\"\"\"
{ctx.parent_prompt}
\"\"\"

YOUR TASK:
1. Diagnose what's failing in the CI run.
2. Push commits to the existing branch `{ctx.branch}` — do NOT open a new PR.
3. Re-run the failing workflow (or let CI pick up the new commits automatically).

CONSTRAINTS:
- Only modify what's needed to fix the failure. No refactoring, no scope creep.
- If the failure is a known-flaky test unrelated to your change, leave a comment
  on the PR explaining this and do NOT skip the test.
- If the failure reveals that the original change was wrong (e.g. the upgrade
  is genuinely incompatible), say so in a PR comment rather than papering over
  with test mocks.
- This is fix attempt #{ctx.attempt_number} of {ctx.max_attempts}. If you cannot
  resolve the failure, leave a PR comment explaining what's blocking you so a
  human can take over.

ACCEPTANCE:
- The previously failing workflow becomes green on the next push.
- No new failures introduced in other workflows.
"""
