# CVE Selection for the Demo

Fork: https://github.com/rakshith48/superset
Scanned: `requirements/base.txt` (157 pinned packages) via OSV.dev
Total findings: 9 vulnerabilities across 5 packages; 8 fixable, 1 unfixable (paramiko)

## Selected for remediation

Picked to balance: (a) demo storytelling, (b) showcase the CI-fix loop, (c) realistic enterprise risk profile.

| # | Package | Installed | Fix | CVE | Severity | Why this one |
|---|---|---|---|---|---|---|
| 1 | **flask** | 2.3.3 | 3.1.3 | CVE-2026-27205 | LOW | **Major version jump (2→3).** Will almost certainly break tests in Superset's flask-heavy codebase. This is the PR that drives the CI-fix loop demo — Devin opens it, CI fails, Devin patches, CI goes green. |
| 2 | **urllib3** | 2.6.3 | 2.7.0 | CVE-2026-44431, CVE-2026-44432 | MEDIUM (CVSS 3.1) | **Two CVEs killed by one minor bump.** Decompression-bomb + sensitive header leakage across proxy origins. Strong "throughput" story for the dashboard: one PR, two vulns closed. |
| 3 | **mako** | 1.3.11 | 1.3.12 | CVE-2026-44307 | HIGH | **Highest severity in the set.** Path traversal in template engine. Patch-level bump (low risk of breakage) but high business urgency — the VP sees "HIGH" and the patch is trivial. Easy win that anchors the security story. |
| 4 | **idna** | 3.10 | 3.15 | CVE-2026-45409 | MEDIUM | Internationalized domain name spec compliance issue. Patch-level bump, very low breakage risk. Demonstrates the system handles routine patches without ceremony. |
| 5 | **pyarrow** *(backup)* | 20.0.0 | 23.0.1 | CVE-2026-25087 | MEDIUM (CVSS 3.1) | **Three major version jumps** (20→23). Held back in case flask proves too gnarly for Devin in the time we have — pyarrow is a viable substitute for the "major upgrade that breaks CI" slot. |

## Deliberately excluded

- **paramiko 3.5.1 (CVE-2026-44405)** — No fix version available yet. Devin can't fix what upstream hasn't released. The scanner skips these.

## Demo narrative

> "Superset is a CNCF graduate, ~60k stars, deployed at thousands of orgs. Right now it ships with a flask version that's two major releases behind — published with a CVE this year. Why? Because flask 3 breaks things, and patching it costs an engineer a day. Watch what Devin does."
>
> *[trigger scan → 4 issues filed → 4 PRs open in parallel → flask PR's CI fails → Devin auto-fixes → green]*
>
> "Four CVEs remediated end-to-end. Four engineer-days saved. Cost: ~$8 in Devin sessions."

## What "fixable" actually means

OSV reports the fix version published upstream. It does NOT guarantee:
- That the upgrade is API-compatible with Superset's usage (flask 2→3 has known breakage)
- That Superset's test suite will pass after the upgrade
- That transitive dependencies will still resolve

The CI-fix loop exists precisely because of these unknowns. If Devin's upgrade PR turns green without intervention, great. If it doesn't, Devin tries again with the failure logs in hand — that's the autonomy story.

## Re-running the scan

```bash
cd superset-fork
python3 ../scripts/scan_osv.py requirements/base.txt
```

Output is JSON to stdout. The production version of this script (in `superset-fork-scripts/file_issues.py`, built in Task #11) will also file GitHub issues with proper labels and dedup against `known_vulnerabilities`.
