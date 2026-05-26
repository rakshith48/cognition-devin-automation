# Cognition Devin Take-Home — Detailed Plan (v2)

**Role:** Deployment Engineer
**Deliverable:** Event-driven automation using the Devin API that remediates issues in an apache/superset fork
**Submission portal:** https://you.ashbyhq.com/cognition/assignment/eb7c0255-3ce8-4283-8ee4-272c646f112a
**Budget:** $500 Devin credit on rakshithramprakash@gmail.com
**Time budget:** Brief says 2–3 hours; realistic target to stand out is 8–10 hours

> **What changed from v1 of this plan.** After a critical review:
> 1. Pivoted from "CVE remediation system" to "Devin Maintenance Orchestrator" with CVE as the *flagship* use case. Label-based dispatch (`devin-remediate`, `devin-security`, `devin-ci-fix`) makes the system extensible and gives fallback demo paths if any single use case proves intractable.
> 2. Build the spine first (label → session → poll → PR → dashboard). CI-fix loop and scheduled scanner are now stretch additions, not foundations.
> 3. Switched from v1 to **v3 Devin API**. v1 is deprecated per Cognition docs; submitting a take-home on their deprecated API is self-sabotage. v3 also gives us `acus_consumed` (real cost), `repos` (explicit binding), `pull_requests[]` array, and native `parent_session_id`.
> 4. Dropped the `known_vulnerabilities` table. Dedup now via GitHub issue search — keeps the source of truth in GitHub, removes coupling between Action and service.
> 5. Updated CVE prompt: Superset uses **uv** to compile `requirements/base.txt` from `pyproject.toml` + `requirements/base.in`. Editing `base.txt` directly is wrong. Prompt now targets `pyproject.toml` and triggers `uv pip compile` for regeneration.
> 6. Added a `/metrics` JSON endpoint alongside the Streamlit dashboard so IC reviewers can verify state without spinning up the UI.

---

## Part 1 — First-principles reasoning

### 1.1 What is this system, fundamentally?

> A reactive state machine that consumes events from one external system (GitHub), spawns work in another (Devin), and surfaces its own state through observable endpoints.

Load-bearing properties (unchanged from v1):

| Property | Why | What breaks if ignored |
|---|---|---|
| Idempotent | External events arrive more than once | Duplicate sessions, burned credit |
| Bounded | Devin sessions cost ACUs and can loop | Runaway cost kills the credit |
| Observable | The pitch IS the dashboard | "Trust me it worked" loses the VP |
| Resumable | Devin sessions are long; our process can restart | Lose in-flight work mid-demo |
| Verifiable | Webhooks come from the public internet | Spoofed events, DoS |

### 1.2 What is the "primitive" here?

Devin is the execution primitive. Our system is the **control plane**: routing events, bounding cost, deduping, surfacing state. We do not write code that does engineering work — Devin does. We translate events into well-engineered prompts and translate Devin's outputs back into observable state.

Implication: spend time on prompts, event routing, idempotency, cost control, dashboard. Not on diff parsing, test running, file mutation.

### 1.3 Entities and state transitions (revised)

```
GitHub Issue (label: devin-remediate + sub-label)
     │
     ▼ webhook: issues.opened
JobRecord  (our SQLite row, status ∈ {pending, running, blocked, completed, failed, timeout})
     │ creates
     ▼
Devin Session  (status ∈ {new, claimed, running, exit, error, suspended, resuming})
     │ on exit, returns pull_requests[]
     ▼
GitHub PR (by Devin)
     │ on workflow_run.completed + conclusion=failure
     ▼  (STRETCH — only if spine works)
Child Devin Session (parent_session_id = original, capped at 3 attempts)
```

Note: the parent/child relationship lives in **Devin's own session model** (`parent_session_id`, `child_session_ids`). Our DB just stores Devin session IDs and queries Devin for the relationship when rendering the dashboard.

---

## Part 2 — Data model (simplified)

SQLite, one file, **two** tables.

```sql
-- Every webhook we've ever received. Dedupe by delivery_id.
CREATE TABLE webhook_events (
    delivery_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    action TEXT,
    payload_json TEXT NOT NULL,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    handler_result TEXT
);

-- Each Devin session we've spawned, with its trigger context.
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    devin_session_id TEXT UNIQUE NOT NULL,
    devin_url TEXT NOT NULL,                -- app.devin.ai/sessions/{id}
    trigger_type TEXT NOT NULL,             -- "cve_issue" | "ci_failure" | "manual"
    trigger_ref TEXT NOT NULL,              -- issue URL or PR URL
    issue_number INTEGER,                   -- for fast lookup
    label TEXT,                             -- which sub-label routed this (devin-security, etc)
    parent_devin_session_id TEXT,           -- denormalized cache of Devin's parent linkage
    prompt_snapshot TEXT NOT NULL,
    status TEXT NOT NULL,                   -- our internal: pending|running|blocked|completed|failed|timeout|cancelled
    raw_status TEXT,                        -- Devin's raw enum, for debugging
    pr_url TEXT,                            -- first PR from pull_requests[]; full list in pr_urls_json
    pr_urls_json TEXT,                      -- all PRs as JSON array
    acus_consumed REAL DEFAULT 0,           -- from Devin API — REAL cost, not estimate
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    last_polled_at TIMESTAMP,
    error_message TEXT,
    fix_attempt_number INTEGER DEFAULT 0    -- 0 = original; 1/2/3 = CI fix follow-ups
);

CREATE INDEX idx_sessions_status ON sessions(status);
CREATE INDEX idx_sessions_trigger_ref ON sessions(trigger_ref);
CREATE INDEX idx_sessions_parent ON sessions(parent_devin_session_id);
```

What we deliberately do NOT store:
- Known vulnerabilities — GitHub issue search is the source of truth
- Engineering "hours saved" estimates — derived at query time from `acus_consumed × known_rate`
- Devin session messages — fetched live from API for drill-down

---

## Part 3 — Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                   YOUR FORK: rakshith48/superset                  │
│                                                                   │
│   GitHub Actions (stretch)         GitHub Webhooks                │
│         │                                  │                      │
│         │ pip-audit / OSV scan             │ issues.opened        │
│         │ files issues with                │ workflow_run.completed (stretch) │
│         │ label: devin-security            │                      │
│         │ (dedup via gh issue search)      │                      │
└─────────┼──────────────────────────────────┼──────────────────────┘
          │                                  │
          ▼                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│   DOCKERIZED SERVICE (FastAPI, behind ngrok)                     │
│                                                                   │
│   POST /webhook/github  ─►  Router  ─►  ┌──────────────────┐    │
│                                          │   Orchestrator   │    │
│                                          │ (SQLite state)   │    │
│   GET  /metrics  (JSON)                  └────────┬─────────┘    │
│   POST /admin/replay/{id}                         │              │
│   POST /admin/reset                       create / poll          │
│                                                   │              │
│                                                   ▼              │
│                                          ┌─────────────────┐    │
│                                          │  Devin v3 API   │    │
│                                          │ api.devin.ai/v3 │    │
│                                          └─────────────────┘    │
│                                                                   │
│   Streamlit dashboard ─► reads /metrics + SQLite ─► live UI      │
└──────────────────────────────────────────────────────────────────┘
```

Two services in docker-compose: `api` (FastAPI + poller) + `dashboard` (Streamlit), sharing a SQLite volume.

---

## Part 4 — Component-by-component

### 4.1 Devin v3 API client

Base URL: `https://api.devin.ai/v3/organizations/{ORG_ID}`
Auth: `Authorization: Bearer cog_*` (service user token)

Endpoints used:
- `POST /sessions` — create. Key fields: `prompt`, `repos: [...]`, `tags`, `title`, `max_acu_limit`, `devin_mode` ("normal"|"fast"), `parent_session_id` (for CI-fix linkage)
- `GET  /sessions/{session_id}` — poll. Returns `status`, `status_detail`, `acus_consumed`, `pull_requests[]`, `parent_session_id`, `child_session_ids`
- `POST /sessions/{session_id}/messages` — send follow-up message
- `POST /sessions/archive` — for cleanup

Status mapping (Devin → our internal):

| Devin `status` | Our internal | Notes |
|---|---|---|
| `new` | `pending` | Session created, not yet picked up |
| `claimed` | `pending` | Queued by Devin |
| `running` | `running` | Actively working |
| `exit` | `completed` | Look at `pull_requests` to confirm success |
| `error` | `failed` | Capture `status_detail` into `error_message` |
| `suspended` | `cancelled` | Manual or `max_acu_limit` cancel |
| `resuming` | `running` | After suspend → resume |

The mapping lives in `devin_client.py` so handlers never see Devin's raw vocabulary.

### 4.2 Webhook receiver (FastAPI)

```
POST /webhook/github   — verify HMAC, dedupe by X-GitHub-Delivery, dispatch
GET  /healthz          — env vars + DB + Devin reachability
GET  /metrics          — JSON for /metrics scraping AND Streamlit
GET  /sessions         — JSON list of sessions
POST /admin/replay/{delivery_id}  — re-fire a captured webhook (demo aid)
POST /admin/reset      — wipe SQLite for clean demo (keeps GitHub state)
```

Dispatch logic (label-based — this is the key extensibility):

```python
def dispatch_issue(issue):
    labels = {l.name for l in issue.labels}
    if "devin-remediate" not in labels:
        return "skipped:no_label"
    if "devin-security" in labels:
        return handle_security_issue(issue)
    if "devin-quality" in labels:
        return handle_quality_issue(issue)
    return handle_generic_remediation(issue)   # default
```

This makes the system feel extensible to reviewers without us actually shipping multiple use cases on day 1.

### 4.3 The CVE prompt (corrected for uv)

Superset's `requirements/base.txt` is uv-generated. Editing it directly will get clobbered by the next regeneration. Correct prompt:

```
You are remediating a security vulnerability in {FORK_URL}.

VULNERABILITY:
- CVE: {cve_id}
- Package: {package} {installed_version}
- Fix version available: {fix_version}
- Severity: {severity}

CONTEXT — DEPENDENCY MANAGEMENT IN THIS REPO:
- This repo uses `uv` to compile requirements files.
- `pyproject.toml` is the source of truth for direct deps.
- `requirements/base.in` is the source for additional pinned deps.
- `requirements/base.txt` is AUTOGENERATED by:
    `uv pip compile pyproject.toml requirements/base.in -o requirements/base.txt`
  DO NOT edit base.txt directly — it will be regenerated.

YOUR TASK:
1. Create branch `devin/cve-{cve_id_slug}` off `master`.
2. Identify where `{package}` is constrained:
   - If in `pyproject.toml` dependencies array → update the version bound there
   - If only a transitive pin in `requirements/base.in` → update there
3. Run `uv pip compile pyproject.toml requirements/base.in -o requirements/base.txt`
   to regenerate the locked file. Commit both source and generated files.
4. Run the test suite. If any tests fail because of the upgrade, fix them.
5. Open a PR against `master` titled
   "Security: upgrade {package} to {fix_version} ({cve_id})".
6. PR description must include: link to CVE, summary, test results.

CONSTRAINTS:
- Modify only what's needed for this upgrade. No drive-by changes.
- Do not bump other dependencies in pyproject.toml unrelated to this CVE.
- If the upgrade is incompatible with Superset's constraints (e.g. fix version
  is outside the allowed range in pyproject.toml), explain in the PR description
  and open as DRAFT.
- If the package is a transitive dep we don't directly depend on,
  pin the parent dep at a version that pulls in the fixed version,
  or document in PR why that's not possible.

ACCEPTANCE:
- CI green on the resulting PR.
- No new lint errors.
- pyproject.toml and requirements/base.txt are internally consistent.
```

### 4.4 The CI-fix prompt (stretch — only if spine works)

```
A PR you opened has failing CI. Your job is to push commits to the same branch
to make CI green.

PR: {pr_url}
Branch: {branch}
Failing workflow: {workflow_name}
Failure logs (last 200 lines):
{logs}

ORIGINAL TASK CONTEXT:
{parent_session_prompt}

CONSTRAINTS:
- Push to the existing branch {branch}; do not open a new PR.
- Only fix what's broken by the failure. Do not refactor.
- If the failure is a flaky test unrelated to the change, document this
  in a PR comment but do not skip the test.
- This is fix attempt #{n} of {MAX_FIX_ATTEMPTS}. If you cannot resolve,
  leave a comment explaining what's blocking you.
```

When spawning the child session, pass `parent_session_id={parent_devin_session_id}` to Devin so the relationship is native.

### 4.5 Orchestrator / poller

Background task, every 30 seconds:

```python
for session in db.get_non_terminal_sessions():
    remote = devin_client.get_session(session.devin_session_id)
    db.update(session.id,
        status=remote.status,
        raw_status=remote.raw_status,
        acus_consumed=remote.acus_consumed,
        pr_url=remote.first_pr_url,
        pr_urls_json=json.dumps(remote.pr_urls),
        last_polled_at=now(),
        completed_at=now() if remote.status in TERMINAL else None,
    )
    # Timeout enforcement
    age = now() - session.started_at
    if remote.status in ("pending", "running") and age > SESSION_TIMEOUT:
        devin_client.terminate_session(session.devin_session_id)
        db.mark_timeout(session.id)
```

### 4.6 Dashboard (Streamlit) + /metrics endpoint

`/metrics` returns:
```json
{
  "active_sessions": 2,
  "completed_sessions": 7,
  "failed_sessions": 1,
  "prs_opened": 6,
  "success_rate": 0.857,
  "total_acus_consumed": 18.3,
  "estimated_hours_saved": 28,
  "estimated_dollars_saved": 4200,
  "by_label": {"devin-security": 5, "devin-quality": 2},
  "last_activity_at": "2026-05-26T14:32:00Z"
}
```

Streamlit reads `/metrics` (or directly from SQLite when running in-container), plus shows:
- Hero row: 4 numbers
- Live session table with status pills, drill-down to Devin URL + PR URL
- 24h throughput chart

Cap Streamlit time at 60 minutes. If it's eating more, stop.

---

## Part 5 — Edge case catalog

Unchanged from v1 except:
- ❌ Removed cases related to `known_vulnerabilities` (table dropped)
- ✏️ Edge case #19 (prompt injection): now also covers `devin-remediate` label as the gate, not just `issue.user.login`
- ✏️ Edge case #25 (transitive dep): now explicitly handled by the CVE prompt's "pin the parent dep" instruction

(Full v1 catalog of 30 cases still applies — see Part 5 of git history if needed.)

---

## Part 6 — Manual test plan (updated phases)

### Phase A: Foundations (~30 min)
A1. Health endpoint with all dep checks passing
A2. Webhook signature verification (no sig, wrong sig, right sig)
A3. Webhook idempotency (same delivery_id 2x → one row, one session)

### Phase B: CVE spine happy path (~45 min) — **MUST PASS BEFORE STRETCH**
B1. File issue with `devin-remediate` + `devin-security` labels by hand
B2. Webhook fires, session row created, Devin session URL visible in dashboard
B3. Devin produces a PR (5–20 min wait); `pr_url` populated
B4. PR is real, branches off master, modifies pyproject.toml + base.txt consistently

### Phase C: Observability (~15 min)
C1. `curl /metrics` returns valid JSON matching SQLite truth
C2. Streamlit shows the session live, auto-refreshes
C3. Drill-down links all resolve

### Phase D: Safety rails (~20 min)
D1. `MAX_CONCURRENT=2`: file 5 issues → 2 active in Devin at any time
D2. `MAX_SESSIONS_PER_DAY=3`: 4th issue marked `skipped:daily_cap`
D3. `SESSION_TIMEOUT=60s`: session terminated and marked timeout
D4. Devin API down (point at closed port): session marked failed gracefully

### Phase E: Idempotency (~10 min)
E1. Replay webhook via GitHub UI → no duplicate session
E2. POST same payload twice → second returns "duplicate"

### Phase F: Stretch — CI-fix loop (~45 min, only if A-E pass)
F1. Force CI failure on a Devin PR; child session spawns with `parent_devin_session_id`
F2. Loop cap: after 3 attempts, 4th does not spawn; dashboard shows "escalated"

### Phase G: Stretch — Scheduled scanner (~30 min, only if A-F pass)
G1. `workflow_dispatch` the cve-scan action → issues filed with correct labels
G2. Re-run scanner → zero new issues (dedup via `gh issue list` query)

### Phase H: Demo dry run (~30 min)
H1. Full Loom rehearsal timed segment-by-segment
H2. Reset + replay 3 captured webhooks for the "earlier today" demo segment

---

## Part 7 — Build order (revised: spine first, stretch last)

| Order | Component | Type | Budget |
|---|---|---|---|
| 1 | ✅ Fork Superset, scan with OSV, pick CVEs | DONE | — |
| 2 | Devin v3 client | SPINE | 45 min |
| 3 | SQLite schema + db.py | SPINE | 20 min |
| 4 | FastAPI app: webhook receiver + /healthz + /metrics + dedup | SPINE | 60 min |
| 5 | prompts.py (CVE prompt with uv awareness) | SPINE | 30 min |
| 6 | Label-based dispatcher + cve handler, real Devin call end-to-end | SPINE | 60 min |
| 7 | Orchestrator/poller with timeout enforcement | SPINE | 30 min |
| 8 | Safety rails (MAX_CONCURRENT, daily cap) | SPINE | 30 min |
| 9 | Streamlit dashboard | SPINE | 60 min |
| 10 | Dockerfile + docker-compose + .env.example + README | SPINE | 45 min |
| 11 | Run Phases A–E of test plan | SPINE | 45 min |
| --- | --- SPINE COMPLETE — DEMO IS NOW VIABLE --- | | |
| 12 | CI-fix handler + child session creation | STRETCH | 45 min |
| 13 | GitHub Action: cve-scan.yml + file_issues.py | STRETCH | 30 min |
| 14 | Run Phases F–G | STRETCH | 30 min |
| 15 | Loom dry run + record | DELIVERABLE | 60 min |

Total spine: ~7 hours. Total with stretch: ~9 hours.
**Rule: never start a stretch task while a spine task is incomplete.**

---

## Part 8 — Loom script (5 min max)

| Time | Beat |
|---|---|
| 0:00–0:30 | **Hook**: "Superset ships with a flask CVE published this year. Why? Because patching breaks tests. Dependabot opens the PR and dies. Watch Devin close the loop." |
| 0:30–2:30 | **Live demo**: trigger scan or label an issue → session appears → dashboard updates → PR produced. If CI-fix stretch built, intentionally break a PR's CI live, watch Devin self-heal. |
| 2:30–3:30 | **Architecture**: webhook router, label-based dispatch ("CVEs are just one trigger — same pipeline handles CI-fix, code quality, anything an autonomous agent can resolve"), idempotency, ACU-based cost tracking via Devin's native field. |
| 3:30–4:15 | **Why Devin specifically**: "Dependabot is rules + a single PR. Cursor needs a human. Devin runs autonomously, reads logs, iterates. That's the primitive this control plane is built on." |
| 4:15–5:00 | **Next steps**: SSO, RBAC, multi-repo, Jira integration, cost guardrails per team. |

---

## Part 9 — Repo layout

```
cognition-devin-automation/
├── docker-compose.yml
├── Dockerfile
├── README.md
├── PLAN.md                        # this file
├── CVE_SELECTION.md               # which CVEs we picked and why
├── pyproject.toml
├── .env.example                   # DEVIN_API_KEY, DEVIN_ORG_ID, GH_WEBHOOK_SECRET, GH_TOKEN, FORK_URL
├── app/
│   ├── main.py                    # FastAPI app + routes
│   ├── devin_client.py            # v3 wrapper + status mapping
│   ├── orchestrator.py            # poller loop
│   ├── handlers.py                # one per sub-label
│   ├── dispatcher.py              # label-based routing
│   ├── prompts.py                 # CVE prompt + CI-fix prompt
│   ├── db.py                      # SQLite schema + queries
│   ├── metrics.py                 # /metrics aggregations
│   └── github_client.py           # PR/issue/workflow API + log fetch
├── dashboard/
│   └── app.py                     # Streamlit
├── scripts/
│   ├── scan_osv.py                # standalone scanner (used in planning + GH Action)
│   └── sample-findings.json       # captured findings for replay
└── superset-fork-scripts/
    ├── cve-scan.yml               # GH Action workflow (stretch)
    └── file_issues.py             # OSV JSON → GitHub issues with dedup (stretch)
```

---

## Part 10 — Locked decisions

- **Use case (flagship):** CVE remediation in Superset
- **Use case (architecture):** Devin Maintenance Orchestrator — label-routed event pipeline
- **Trigger:** GitHub webhooks (`issues.opened`), optionally `workflow_run.completed` for CI-fix
- **Hosting:** Local Docker + ngrok tunnel
- **Stack:** Python 3.13, FastAPI, SQLite, Streamlit, Docker Compose
- **Devin API:** **v3** (`/v3/organizations/{ORG_ID}/`)
- **Auth:** `cog_*` service user token in `Authorization: Bearer`
- **Cost tracking:** Real `acus_consumed` from Devin, NOT estimated dollars
- **Dedup of CVEs:** GitHub issue search (`gh issue list --label ...`), NOT our SQLite
- **Pitch hook:** "Dependabot stops when CI fails. Devin closes the loop."
- **Pitch broadening:** "And the same pipeline handles every other kind of maintenance work — this is the control plane."
