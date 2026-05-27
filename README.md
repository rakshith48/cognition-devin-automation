# Devin Maintenance Orchestrator

> **An event-driven remediation control plane for dependency vulnerabilities in large repos.**
>
> It detects vulnerable packages, opens GitHub issues, assigns Devin bounded remediation tasks, watches for PRs, reacts to CI failures with capped follow-up sessions, auto-closes on merge, and reports progress / cost / success to engineering leadership.
>
> *Dependabot stops at the PR. Devin closes the loop.*

A take-home for Cognition. Built as the kind of system a Deployment Engineer would hand to a Stripe or Ramp VP of Engineering to prove out Devin as production infrastructure — not as a chatbot, not as a script runner, as the *control plane* that turns one class of recurring engineering toil into a managed, observable pipeline.

[`PLAN.md`](./PLAN.md) — first-principles design doc. [`CVE_SELECTION.md`](./CVE_SELECTION.md) — which CVEs the demo targets and why.

---

## What this does

```
GitHub issue (labeled devin-remediate + devin-security)
       │  ← filed by the nightly OSV scanner GH Action, or by hand
       ▼
   FastAPI webhook receiver
       │  ← HMAC verified, deduped by X-GitHub-Delivery
       ▼
   Label-based dispatcher
       │  ← routes by sub-label (security / quality / generic / ci-fix)
       ▼
   Atomic work_key reservation (sqlite UNIQUE)
       │  ← prevents duplicate paid sessions on retried webhooks;
       │    over-capacity events QUEUE, never drop
       ▼
   Devin v3 session (structured_output_schema → remediation report)
       │  ← uv-aware prompt, max_acu_limit cap, repos[] binding
       ▼
   Pull Request on the fork
       │
       ├── CI passes → user merges
       │       │
       │       ▼  (poller detects pr_state="merged")
       │   Terminate Devin session (stop ACU bleed)
       │   Mark session 'completed' / Stage = ✅ verified
       │
       └── CI fails AND Devin's own watch loop has exited
               │  ← parent-terminal gate: don't race Devin while it's alive
               ▼
           Child Devin fix-session (parent_session_id wired natively)
               │
               ├── fix succeeds within MAX_FIX_ATTEMPTS → green
               │
               └── cap reached (3) → ESCALATION
                       │
                       ├── parent → 🟡 needs_attention (dashboard warning)
                       ├── active children terminated
                       └── PR comment posted with context
```

**Validated end-to-end on a real CVE**: [rakshith48/superset#11](https://github.com/rakshith48/superset/pull/11) — `mako 1.3.11 → 1.3.12` (CVE-2026-44307). Devin's session opened the PR, our `uv lock consistency` check verified it, the user merged via GitHub UI, the poller auto-detected `pr_state=merged` within 30s and closed the session.

---

## Quickstart

### 1. Fork Apache Superset, get Devin credentials

```bash
gh repo fork apache/superset --clone=false
gh api -X PATCH repos/<you>/superset -f has_issues=true   # forks inherit Apache's "issues disabled"
git clone https://github.com/<you>/superset.git superset-fork

# Install the Devin GitHub App on your fork:
#   https://github.com/apps/devin-ai → Install → select <you>/superset
# Generate a service-user token:
#   app.devin.ai → Settings → Service Users → create → copy cog_*
# Find your org id:
#   app.devin.ai URL bar → org-*
```

### 1b. Install the nightly scanner + lock-consistency workflows into the fork

```bash
# Copies superset-fork-scripts/* into the fork's .github/, commits, and pushes.
# Idempotent — re-runs are no-ops when nothing has changed.
./scripts/install_fork_workflows.sh ./superset-fork
```

**Note on Superset's upstream workflows**: a fresh fork inherits ~40 of Apache Superset's CI workflows, most of which require their proprietary CI infrastructure (DB services, Playwright browsers, secrets) and will fail on any fork. For a clean demo, disable everything except your `lock file validity`, `CVE scan`, and `PR Lint`:

```bash
KEEP=$(gh workflow list --repo <you>/superset --json id,name --jq '.[] | select(.name=="lock file validity" or .name=="CVE scan" or .name=="PR Lint") | .id' | tr '\n' ' ')
gh workflow list --repo <you>/superset --json id,name --jq '.[].id' | while read id; do
  echo "$KEEP" | grep -qw "$id" && continue
  gh workflow disable "$id" --repo <you>/superset
done
```

### 2. Configure `.env`

```bash
cp .env.example .env
# Fill in: DEVIN_API_KEY (cog_*), DEVIN_ORG_ID (org-*), FORK_REPO,
# GH_TOKEN (gh auth token works), GH_WEBHOOK_SECRET (any 32+ char random),
# ADMIN_TOKEN if you want /admin/* enabled.
```

### 3. Bring up the stack

```bash
docker compose up --build -d
docker compose logs -f api          # watch the poller
# API:       http://localhost:8000
# Dashboard: http://localhost:8502
```

### 4. Verify it's alive

```bash
curl -s http://localhost:8000/healthz | jq
# { "db": "ok", "devin": "ok", "config": "ok", "config_issues": [] }
```

### 5. Trigger the first remediation (without ngrok)

```bash
# File a CVE issue with the right labels.
gh label create devin-remediate --color 0E8A16 --description "Gate label"
gh label create devin-security  --color B60205 --description "Security subroute"
gh issue create --repo <you>/superset \
  --title "[CVE] idna 3.10 → 3.15 (CVE-2026-45409)" \
  --label "devin-remediate,devin-security" \
  --body "$(cat scripts/example-issue-body.md)"

# Then simulate the webhook locally (no ngrok needed). The simulator fetches
# the real issue from GitHub and POSTs a signed webhook to /webhook/github:
.venv/bin/python scripts/simulate_issue.py 1
# 200 {"status":"accepted","delivery_id":"sim-1-1748000000"}
# Open http://localhost:8502 to watch the session progress live.
```

### 6. Wire up the webhook for live demos

```bash
ngrok http 8000
# In another terminal:
gh api -X POST repos/<you>/superset/hooks \
  -f name=web \
  -F active=true \
  -f 'events[]=issues' \
  -f 'events[]=workflow_run' \
  -F 'config[url]=https://<your-ngrok>.ngrok-free.app/webhook/github' \
  -F 'config[secret]='"$(grep ^GH_WEBHOOK_SECRET .env | cut -d= -f2)" \
  -F 'config[content_type]=json'
```

Now adding `devin-security` to any issue with the `devin-remediate` gate spawns a Devin session within seconds.

---

## Architecture

```
cognition-devin-automation/
├── app/
│   ├── main.py              FastAPI app, lifespan, router mounting, poller startup
│   ├── settings.py          env vars + validate_for_runtime() + bot login set
│   ├── signature.py         HMAC verify (security-critical, unit tested)
│   ├── dispatcher.py        label-based event routing
│   ├── handlers.py          handle_security_issue + handle_ci_failure + escalation
│   ├── orchestrator.py      poller: drain_queue + poll_once + merge detection
│   ├── stage.py             workflow Stage derivation (detected → … → verified)
│   ├── metrics.py           PR-honest success rate, needs_human bucket, by_label
│   ├── serializers.py       db.SessionRow → API dict
│   ├── github_client.py     minimal GH REST: PR, commits, workflow logs, PR comment
│   ├── prompts/
│   │   ├── cve.py           uv-aware CVE prompt + parser + conventional-commits title
│   │   ├── ci_fix.py        CI-failure follow-up prompt (with untrusted-log block)
│   │   └── structured.py    CVE_REMEDIATION_SCHEMA (Devin structured_output)
│   ├── devin/
│   │   ├── types.py         status mapping + SessionDetails (parses structured_output)
│   │   ├── http.py          retry / backoff transport
│   │   ├── client.py        v3 endpoints (create, get, send_message, terminate)
│   │   └── factory.py       lazy thread-safe singleton
│   ├── db/
│   │   ├── connection.py    sqlite + WAL + idempotent migrations (collision-aware)
│   │   ├── webhook_events.py PK dedupe + list_queued for queue drain
│   │   └── sessions.py      atomic try_reserve, column allow-list,
│   │                        max_fix_attempt_for_parent, find_active_children
│   └── routes/
│       ├── health.py        /healthz (db + devin + config)
│       ├── webhook.py       POST /webhook/github
│       ├── metrics.py       /metrics + /sessions
│       └── admin.py         /admin/{replay,reset} — gated by mount flag + token
├── dashboard/
│   └── streamlit_app.py     Hero, sessions table with Stage column,
│                            Remediation reports expander (structured output),
│                            native st.fragment(run_every=N) refresh — no flicker
├── scripts/
│   ├── scan_osv.py          OSV vulnerability scan (used in planning + GH Action)
│   ├── simulate_issue.py    Sign + POST a real webhook locally (no ngrok)
│   ├── install_fork_workflows.sh   Idempotent install of fork-side workflows
│   └── example-issue-body.md       Sample CVE issue body for the quickstart
├── superset-fork-scripts/   Lives on the FORK after install_fork_workflows.sh:
│   ├── cve-scan.yml                 Nightly OSV scan → files issues (idempotent)
│   ├── uv-lock-consistency.yml      pip install --dry-run --no-deps check
│   └── file_issues.py               OSV findings → GH issues with 2-layer dedup
├── tests/                   87 pytest tests on boundary logic
├── Dockerfile               python:3.13-slim, non-root, single image
├── docker-compose.yml       api + dashboard, shared SQLite volume, healthchecks
└── pyproject.toml
```

### Module responsibilities

| Module | One reason to change |
|---|---|
| `signature.py` | GitHub changes webhook signing scheme |
| `devin/types.py` | Devin changes its status enum or response shape |
| `devin/http.py` | retry / backoff / rate-limit policy changes |
| `devin/client.py` | Devin adds / changes endpoints |
| `db/sessions.py` | sessions schema or query shape changes |
| `db/connection.py` | new migration step (idempotent, no version table) |
| `prompts/cve.py` | CVE prompt wording or repo conventions change |
| `prompts/structured.py` | Devin remediation-report schema changes |
| `handlers.py` | new safety rail or sub-label routing logic |
| `dispatcher.py` | new event type or sub-label |
| `orchestrator.py` | poller cadence, queue policy, or merge-detection rules |
| `stage.py` | dashboard lifecycle vocabulary changes |
| `metrics.py` | dashboard aggregation buckets change |
| `github_client.py` | new GH REST endpoint needed (e.g. checks, reviews) |
| `routes/*` | HTTP shape changes |

No module has two reasons to change — that's the test for "modular enough."

### Load-bearing properties

| Property | Mechanism |
|---|---|
| **Idempotent** | webhook `delivery_id` PK + sessions `work_key` UNIQUE — same event arriving twice spawns one Devin session, not two |
| **Bounded cost** | per-session `max_acu_limit` + global `MAX_CONCURRENT_SESSIONS` + `MAX_SESSIONS_PER_DAY` + per-session inactivity timeout + `MAX_FIX_ATTEMPTS` chain cap |
| **No silent drops** | events over concurrency cap go to a queue (`handler_result='queued:...'`); poller drains FIFO on each tick as capacity frees |
| **Observable** | every webhook recorded with `handler_result`; every session has a row even if the Devin call fails; `/metrics` + dashboard surface |
| **Resumable** | poller is stateless — pick up wherever we left off after restart |
| **Verifiable** | HMAC signature on every webhook; admin routes require token AND mount-time enable flag |
| **Escalation-aware** | CI-fix cap-hit terminates active children, posts a PR comment, surfaces parent in `needs_human` dashboard bucket — bounded silence, never |
| **Doesn't race Devin** | CI-fix handler only fires when the parent Devin session is in a terminal state — Devin's own task-list step "Wait for CI checks and fix any failures" owns it while the parent is alive |

---

## Demo runbook

The 5-minute Loom story:

1. **Frame the problem** — every engineering team has a shadow backlog of toil (CVE patches, flaky tests, deprecation cleanups). Detection isn't the bottleneck; the *capacity to act* is.
2. **Show the dashboard** — point at the contrast pair: one session ✅ `verified` (a real merged PR), one session 🟡 `needs human` (cap-hit escalation). One screen tells the success + failure story.
3. **Trigger live** — add `devin-security` to a CVE issue. Within seconds the dashboard row appears: `🟣 detected → 🔵 devin running`. Click through to Devin's UI showing it work.
4. **The merge moment** — when CI is green, merge the PR. Within 30s the dashboard row flips to `✅ verified`, the poller terminates the Devin session, the Remediation reports expander populates with Devin's structured output (summary / risk / files / tests).
5. **Why Devin specifically** — open Devin's own session UI showing its task list with "Wait for CI checks and fix any failures." "Dependabot stops at the PR. Cursor needs a human. Devin reads logs, makes architectural decisions, iterates. That's the autonomous primitive this control plane is built on."
6. **Next steps** — multi-repo, SSO + RBAC, per-team budgets, more sub-labels (`devin-flaky-test`, `devin-doc-gap`) — same control plane, new handlers.

---

## Environment variables

Every var the service reads is here. Same list lives in `.env.example`.

| Var | Required | Default | Purpose |
|---|---|---|---|
| `DEVIN_API_KEY` | ✓ | — | Service-user token from app.devin.ai (`cog_*`) |
| `DEVIN_ORG_ID` | ✓ | — | Org id from URL (`org-*`) |
| `DEVIN_BASE_URL` | — | `https://api.devin.ai/v3` | API endpoint |
| `DEVIN_MODE` | — | `normal` | `normal` or `fast` |
| `DEVIN_MAX_ACU_PER_SESSION` | — | `30` | Per-session cost ceiling |
| `GH_TOKEN` | ✓ | — | GitHub PAT with `repo` + `workflow` scopes |
| `GH_WEBHOOK_SECRET` | ✓ | — | HMAC secret; 32+ bytes of random |
| `FORK_URL` | — | — | `https://github.com/<you>/superset` |
| `FORK_REPO` | — | — | `<you>/superset` (owner/name) |
| `DEVIN_BOT_LOGINS` | — | `devin-ai-integration[bot],devin-ai[bot],devin[bot]` | Comma-separated GH logins that count as Devin commits (extend if your install uses a different app slug) |
| `MAX_CONCURRENT_SESSIONS` | — | `3` | Globally active Devin sessions cap (excess events queue) |
| `MAX_SESSIONS_PER_DAY` | — | `25` | Daily session count cap (hard skip, no queue) |
| `SESSION_TIMEOUT_SECONDS` | — | `2700` | Inactivity timeout (based on Devin's own `updated_at`, not session age) |
| `MAX_FIX_ATTEMPTS` | — | `3` | CI-fix chain cap — escalation fires after this |
| `POLL_INTERVAL_SECONDS` | — | `30` | Poller cadence |
| `HOURS_SAVED_PER_COMPLETED_SESSION` | — | `4` | For dashboard $ saved calc |
| `ENGINEER_HOURLY_RATE_USD` | — | `150` | For dashboard $ saved calc |
| `DB_PATH` | — | `/data/automation.db` (docker) / `./data/automation.db` | SQLite file location |
| `ENABLE_ADMIN_ROUTES` | — | `false` | Mount /admin/* — defaults off for safety |
| `ADMIN_TOKEN` | conditional | — | Required when `ENABLE_ADMIN_ROUTES=true` |

---

## API surface

```
GET  /healthz                    db + devin + config status
POST /webhook/github             HMAC-verified, deduped, dispatched in background
GET  /metrics                    PR-honest aggregations (JSON, used by dashboard)
GET  /sessions?limit=100         raw session list (JSON)

# Admin (only mounted when ENABLE_ADMIN_ROUTES=true; require X-Admin-Token)
POST /admin/replay/{delivery_id} re-fire a captured webhook (demo aid)
POST /admin/reset                wipe SQLite (preserves GitHub state)
```

### Curl examples

```bash
curl -s http://localhost:8000/healthz | jq
curl -s http://localhost:8000/metrics | jq
curl -s -X POST http://localhost:8000/admin/replay/<delivery-id> \
     -H "X-Admin-Token: $ADMIN_TOKEN"
```

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
# 87 passed in 0.45s
```

Coverage focus: the boundaries that prevent wasted money and false reporting.

| Suite | What it locks down |
|---|---|
| `test_signature.py` | HMAC happy + 6 attack vectors (wrong secret, tampered body, missing header, wrong prefix, empty secret, format) |
| `test_devin_types.py` | All v3 statuses map explicitly; unknown → `needs_attention` (no silent drops) |
| `test_db.py` | Webhook idempotency, atomic work_key reservation, column allow-list (SQL-injection defense), terminal transitions |
| `test_migration.py` | Backfill matches runtime work_key format; collision dedup with `:dup:<id>` suffix; respects existing keys; unparseable trigger_ref fallback |
| `test_prompts.py` | Parser strictness, `latest` placeholder rejection, untrusted-block delimiters, `uv pip compile` + `superset-core/pyproject.toml` guidance present |
| `test_metrics.py` | Only PR-producing TERMINAL sessions count as success (cannot exceed 100%); `needs_human` bucket; `by_label` slice |
| `test_stage.py` | Lifecycle Stage derivation: detected → devin running → PR open → ci fix running → verified, plus needs_human / failed / cancelled transitions |
| `test_dispatcher.py` | Gate label required; sub-label routing; workflow_run filters; ping handled |
| `test_handlers.py` | Happy path, dedup, missing-fields skip, concurrency QUEUE (not drop), Devin failure marks reservation `failed` (no orphans) |
| `test_ci_fix_handler.py` | All 6 attribution guards; chain-walking attempt cap; webhook-rerun dedup; **escalation: parent → needs_attention, children terminated, PR comment posted; parent-terminal gate (don't race Devin)** |
| `test_queue.py` | Concurrency-capped events queue (not skip); drain on capacity free; respects remaining capacity per tick; daily cap stays a skip |
| `test_merge_detection.py` | `pr_state='merged'` → completed + terminate Devin; `closed` → cancelled; open PR keeps session active; idempotent after terminal |

`ruff check app scripts tests dashboard superset-fork-scripts` — clean.

---

## Operations

### Logs
```bash
docker compose logs -f api dashboard
```

### Reset state for a clean demo (preserves GitHub)
```bash
curl -X POST http://localhost:8000/admin/reset -H "X-Admin-Token: $ADMIN_TOKEN"
```

### Replay a webhook
```bash
# Find a delivery_id from the webhook_events table or `gh api repos/<you>/superset/hooks/<id>/deliveries`
curl -X POST http://localhost:8000/admin/replay/<id> -H "X-Admin-Token: $ADMIN_TOKEN"
```

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/healthz` says `devin: fail` | Token expired / org mismatch | Regenerate service user; verify org id |
| Webhook returns 401 | Wrong `GH_WEBHOOK_SECRET` | Confirm secret matches what GitHub UI shows |
| Webhook returns 200 but no session | Look at `webhook_events.handler_result` | Common values: `skipped:no_gate_label`, `skipped:already_reserved`, `queued:concurrency_cap:*`, `skipped:parent_still_active`, `error:devin_create_failed:*` |
| Session stuck in `waiting_for_user` | Devin asked a clarifying question | Open the Devin URL, answer, or send a follow-up via `client.send_message()` |
| All Devin calls 403 | App not installed on fork OR token lacks org access | Install Devin GitHub App, or regenerate service user under the right org |
| CI-fix handler returns `skipped:parent_still_active` | Devin's own session is alive and handling CI itself | Expected — our handler is a FALLBACK that fires only after Devin's session exits |
| Success rate stuck below 100% with PRs visible | Some completions exited without a PR | Check the `completed_without_pr` count in `/metrics` and the Devin session messages |

---

## What's next (for a real customer engagement)

This take-home demonstrates the spine. In a real Stripe/Ramp engagement you'd extend with:

- **SSO + RBAC** on the dashboard and admin routes
- **Multi-repo** — `FORK_REPO` becomes a list; per-repo configuration; per-repo Devin org binding
- **Jira / Linear integration** — bidirectional sync of issues so non-GitHub teams stay aligned; escalation comments also post to ticket
- **Per-team cost guardrails** — daily ACU budgets per team / per label
- **CI-green tracking** — second polling loop against GitHub workflow_runs to surface "PR merged green" vs "PR merged red" as the real success metric
- **More sub-labels** — `devin-doc-gap`, `devin-flaky-test`, `devin-perf` — same dispatcher, new handlers
- **PR review automation** — when a Devin PR is mergeable but unreviewed for N days, ping a CODEOWNERS-derived reviewer in Slack

---

## Submission

- **Orchestrator repo (this code):** https://github.com/rakshith48/cognition-devin-automation
- **Superset fork (with our workflows installed):** https://github.com/rakshith48/superset
- **Proof artifact — real merged PR by Devin:** https://github.com/rakshith48/superset/pull/11
- **Loom walkthrough:** *(recording link)*

Built by Rakshith Ramprakash for Cognition's Deployment Engineer take-home, May 2026.
