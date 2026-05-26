# Devin Maintenance Orchestrator

> **An event-driven remediation control plane for dependency vulnerabilities in large repos.**
>
> It detects vulnerable packages, opens GitHub issues, assigns Devin bounded remediation tasks, watches for PRs, reacts to CI failures with capped follow-up sessions, and reports progress / cost / success to engineering leadership.
>
> *Dependabot stops at the PR. Devin closes the loop.*

A take-home for Cognition. Built as the kind of system a Deployment Engineer would hand to a Stripe or Ramp VP of Engineering to prove out Devin as production infrastructure — not as a chatbot, not as a script runner, as the *control plane* that turns one class of recurring engineering toil into a managed, observable pipeline.

[`PLAN.md`](./PLAN.md) — first-principles design doc. [`CVE_SELECTION.md`](./CVE_SELECTION.md) — which CVEs the demo targets and why.

---

## What this does

```
GitHub issue (label: devin-remediate + devin-security)
       │  ← filed by nightly pip-audit scan, or by hand
       ▼
   FastAPI webhook receiver
       │  ← HMAC verified, deduped by delivery_id
       ▼
   Label-based dispatcher
       │  ← routes by sub-label (security / quality / ci-fix)
       ▼
   Atomic work_key reservation (sqlite UNIQUE)
       │  ← prevents duplicate paid sessions on retried webhooks
       ▼
   Devin v3 session
       │  ← uv-aware prompt, max_acu_limit cap
       ▼
   Pull Request on the fork
       │  ← if CI fails (stretch), spawn child fix-session, repeat ≤3x
       ▼
   Dashboard / /metrics
       ← VP-of-Eng-legible: PRs created, success rate, hours saved
```

**Validated end-to-end** on a real CVE: [https://github.com/rakshith48/superset/pull/2](https://github.com/rakshith48/superset/pull/2) (idna 3.10 → 3.16 for CVE-2026-45409).

---

## Quickstart

### 1. Fork Apache Superset, get Devin credentials

```bash
# Fork apache/superset under your GitHub user
gh repo fork apache/superset --clone=false
gh api -X PATCH repos/<you>/superset -f has_issues=true   # forks inherit Apache's "issues disabled"

# Clone your fork — needed for step 1b's workflow install
git clone https://github.com/<you>/superset.git superset-fork

# Install the Devin GitHub App on your fork:
#   https://github.com/apps/devin-ai → Install → select <you>/superset
# Generate a service-user token:
#   app.devin.ai → Settings → Service Users → create → copy cog_*
# Find your org id:
#   app.devin.ai URL bar → org-*
```

### 1b. Install the nightly scanner + CI workflows into the fork

```bash
# Copies superset-fork-scripts/* into the fork's .github/, commits, and pushes.
# Idempotent — re-runs are no-ops when nothing has changed.
./scripts/install_fork_workflows.sh ./superset-fork
```

### 2. Configure `.env`

```bash
cp .env.example .env
# fill in DEVIN_API_KEY (cog_*), DEVIN_ORG_ID (org-*), FORK_REPO,
# GH_TOKEN (gh auth token works), GH_WEBHOOK_SECRET (any 32+ char random),
# and ADMIN_TOKEN if you want to enable /admin/* endpoints.
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

### 5. Trigger the first remediation (without setting up the webhook yet)

```bash
# File a CVE issue with the right labels.
gh label create devin-remediate --color 0E8A16 --description "Gate label"
gh label create devin-security  --color B60205 --description "Security subroute"
gh issue create --repo <you>/superset \
  --title "[CVE] idna 3.10 → 3.15 (CVE-2026-45409)" \
  --label "devin-remediate,devin-security" \
  --body "$(cat scripts/example-issue-body.md)"

# Then either: (a) wait for the webhook (next section), or (b) simulate the
# webhook locally without ngrok. The simulator fetches the real issue from
# GitHub and POSTs a signed webhook to your local /webhook/github:
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
  -f active=true \
  -f 'events[]=issues' \
  -f 'events[]=workflow_run' \
  -F 'config[url]=https://<your-ngrok>.ngrok-free.app/webhook/github' \
  -F 'config[secret]='"$(grep ^GH_WEBHOOK_SECRET .env | cut -d= -f2)" \
  -F 'config[content_type]=json'
```

---

## Architecture

```
cognition-devin-automation/
├── app/
│   ├── main.py              FastAPI app, lifespan, router mounting
│   ├── settings.py          env vars + validate_for_runtime()
│   ├── signature.py         HMAC verify (security-critical, unit tested)
│   ├── dispatcher.py        label-based event routing
│   ├── handlers.py          one handler per sub-label
│   ├── prompts/
│   │   ├── cve.py           uv-aware CVE prompt + parser
│   │   └── ci_fix.py        CI-failure follow-up prompt
│   ├── devin/
│   │   ├── types.py         status mapping (boundary translation)
│   │   ├── http.py          retry / backoff transport
│   │   ├── client.py        v3 endpoints (create, get, send_message, terminate)
│   │   └── factory.py       lazy thread-safe singleton
│   ├── db/
│   │   ├── connection.py    sqlite + WAL + idempotent migrations
│   │   ├── webhook_events.py
│   │   └── sessions.py      atomic try_reserve(), column allow-list update()
│   ├── orchestrator.py      background poller (inactivity-based timeout)
│   ├── metrics.py           PR-honest success rate aggregation
│   ├── serializers.py       db.SessionRow → API dict
│   └── routes/
│       ├── health.py        /healthz (db + devin + config)
│       ├── webhook.py       POST /webhook/github
│       ├── metrics.py       /metrics + /sessions
│       └── admin.py         /admin/{replay,reset} — gated by token
├── dashboard/
│   └── streamlit_app.py     Streamlit — hero, sessions table, throughput
├── scripts/
│   ├── scan_osv.py          OSV vulnerability scan
│   └── file_issues.py       (Task #11) GH Action → issues
├── tests/                   46 pytest tests on boundary logic
├── superset-fork-scripts/   (Task #11) workflows installed on the fork
├── Dockerfile               python:3.13-slim, non-root, single image
├── docker-compose.yml       api + dashboard, shared volume
└── pyproject.toml
```

### Module responsibilities

| Module | One reason to change |
|---|---|
| `signature.py` | GitHub changes signing scheme |
| `devin/types.py` | Devin changes its status enum |
| `devin/http.py` | retry/backoff policy changes |
| `devin/client.py` | Devin adds/changes endpoints |
| `db/sessions.py` | sessions schema or query shape changes |
| `prompts/cve.py` | CVE prompt wording or repo conventions change |
| `handlers.py` | new safety rail or sub-label routing logic |
| `metrics.py` | dashboard buckets change |
| `routes/*` | HTTP shape changes |

No module has two reasons to change — that's the test for "modular enough."

### Load-bearing properties

| Property | Mechanism |
|---|---|
| **Idempotent** | webhook `delivery_id` PK + sessions `work_key` UNIQUE — same event arriving twice spawns one Devin session, not two |
| **Bounded** | per-session `max_acu_limit` + global `MAX_CONCURRENT_SESSIONS` + `MAX_SESSIONS_PER_DAY` + per-session inactivity timeout |
| **Observable** | every webhook recorded with `handler_result`; every session has a row even if Devin call fails; `/metrics` + dashboard |
| **Resumable** | poller is stateless — pick up wherever we left off after restart |
| **Verifiable** | HMAC signature on every webhook; admin routes require token AND mount-time enable flag |

---

## Demo runbook

The 5-minute Loom story:

1. **Open the dashboard** — show the live idna PR Devin produced, the metrics row.
2. **Trigger a new scan** — `gh workflow run cve-scan.yml` (Task #11) fires the nightly OSV scan on-demand; new issues appear; webhooks land; new Devin sessions spawn.
3. **Show the architecture** — open the README architecture section; trace one event through the dispatcher / handler / Devin client. Emphasize the safety rails (work_key reservation, untrusted-block prompt delimiters, admin gate).
4. **Force a CI failure** (if Task #9 wired) — push a bad commit to a Devin PR; the workflow_run webhook fires; CI-fix handler spawns a child session; CI goes green.
5. **Why Devin specifically** — "Dependabot stops at the PR. Cursor needs a human in the loop. Devin reads logs, fixes code, iterates. That's the autonomous primitive this control plane is built on."

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
| `MAX_CONCURRENT_SESSIONS` | — | `3` | Globally active Devin sessions cap |
| `MAX_SESSIONS_PER_DAY` | — | `25` | Daily session count cap |
| `SESSION_TIMEOUT_SECONDS` | — | `2700` | Inactivity timeout (Devin updated_at based) |
| `MAX_FIX_ATTEMPTS` | — | `3` | CI-fix loop cap (stretch — Task #9) |
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
GET  /metrics                    PR-honest aggregations (JSON)
GET  /sessions?limit=100         raw session list (JSON)

# Admin (only mounted when ENABLE_ADMIN_ROUTES=true; require X-Admin-Token)
POST /admin/replay/{delivery_id} re-fire a captured webhook (demo aid)
POST /admin/reset                wipe SQLite (preserves GitHub state)
```

### Curl examples

```bash
# health
curl -s http://localhost:8000/healthz | jq

# metrics for the IC audience
curl -s http://localhost:8000/metrics | jq

# replay a captured webhook (demo reset → replay flow)
curl -s -X POST http://localhost:8000/admin/replay/<delivery-id> \
     -H "X-Admin-Token: $ADMIN_TOKEN"
```

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
# 59 passed in 0.34s
```

Coverage focus: the boundaries that prevent wasted money and false reporting.

| Suite | What it locks down |
|---|---|
| `test_signature.py` | HMAC happy + 6 attack vectors (wrong secret, tampered body, missing header, wrong prefix, empty secret, format) |
| `test_devin_types.py` | All v3 statuses map explicitly; unknown → `needs_attention` (no silent drops) |
| `test_db.py` | Webhook idempotency, atomic work_key reservation, column allow-list (SQL-injection defense), terminal transitions |
| `test_prompts.py` | Parser strictness, `latest` placeholder rejection, untrusted-block delimiters, `uv pip compile` guidance present |
| `test_metrics.py` | Only PR-producing sessions count as success; `needs_human` bucket; `by_label` slice |
| `test_dispatcher.py` | Gate label required; sub-label routing; workflow_run filters |
| `test_handlers.py` | Happy path, dedup, missing-fields skip, concurrency cap, Devin failure marks reservation `failed` (no orphans) |

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
| Webhook returns 200 but no session | Look at `webhook_events.handler_result` | Common values: `skipped:no_gate_label`, `skipped:already_reserved`, `error:devin_create_failed:*` |
| Session stuck in `waiting_for_user` | Devin asked a clarifying question | Open the Devin URL, answer, or send a follow-up via `client.send_message()` |
| All Devin calls 403 | App not installed on fork OR token lacks org access | Install Devin GitHub App, or regenerate service user under the right org |

---

## What's next (for a real customer engagement)

This take-home demonstrates the spine. In a real Stripe/Ramp engagement you'd extend with:

- **SSO + RBAC** on the dashboard and admin routes
- **Multi-repo** — `FORK_REPO` becomes a list; per-repo configuration
- **Jira / Linear integration** — bidirectional sync of issues so non-GitHub teams stay aligned
- **Per-team cost guardrails** — `MAX_ACUS_PER_DAY` and per-team budgets
- **CI-green tracking** — second polling loop against GitHub workflow_runs to surface "PR merged green" as the real success metric
- **More sub-labels** — `devin-doc-gap`, `devin-flaky-test`, `devin-perf` — same dispatcher, new handlers
- **Replay buffer / queue** for events arriving while concurrency is capped

---

## Submission

Loom: *(recording link)*
GitHub repo: *(this repo)*
Superset fork: https://github.com/rakshith48/superset

Built by Rakshith Ramprakash for Cognition's Deployment Engineer take-home, May 2026.
