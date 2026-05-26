"""File GitHub issues for OSV findings, idempotently.

Run inside the fork's CVE-scan GitHub Action. Reads OSV findings JSON
(format produced by scan_osv.py), and for each fixable finding:

  1. Search existing issues — open OR closed — for any issue whose title
     contains the CVE id. If found, skip (idempotent).
  2. Otherwise, file a structured issue with labels devin-remediate +
     devin-security and a body that the orchestrator's CVE parser can read.

Why search rather than maintain a local known-vulnerabilities table:
GitHub IS the source of truth for "have we seen this CVE." A local table
would couple the GH Action (one process, one machine) to the orchestrator
(separate process, separate machine). Querying the API keeps the two
decoupled and avoids the dual-write inconsistency class.

The orchestrator hears issues.opened, parses the body, and spawns Devin.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = os.environ.get("REPO") or sys.exit("REPO env var required (owner/name)")

GATE_LABEL = "devin-remediate"
SECURITY_LABEL = "devin-security"


def existing_issue_for(cve_id: str) -> str | None:
    """Returns the issue URL if one already exists for this CVE, else None."""
    try:
        out = subprocess.run(
            [
                "gh", "issue", "list", "--repo", REPO,
                "--label", GATE_LABEL,
                "--state", "all",
                "--search", f'"{cve_id}" in:title',
                "--json", "url,title",
                "--limit", "5",
            ],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        print(f"  ! gh issue list failed: {e.stderr}", file=sys.stderr)
        return None
    rows = json.loads(out or "[]")
    # Title-contains is OR matching; verify the CVE id is actually in the title.
    for r in rows:
        if cve_id in r.get("title", ""):
            return r.get("url")
    return None


def file_issue(finding: dict) -> str | None:
    cve = finding["cve_id"]
    title = (
        f"[CVE] {finding['package']} {finding['installed_version']} → "
        f"{finding['fix_version']} ({cve})"
    )
    body = "\n".join([
        "## Vulnerability",
        f"- **CVE:** {cve}",
        f"- **Package:** {finding['package']}",
        f"- **Installed:** {finding['installed_version']}",
        f"- **Fix version:** {finding['fix_version']}",
        f"- **Severity:** {finding['severity']}",
        f"- **Summary:** {finding.get('summary', '')[:300] or '(no summary provided by OSV)'}",
        "",
        "## OSV link",
        f"https://osv.dev/vulnerability/{cve}",
        "",
        "## Remediation",
        "This issue will be auto-remediated by Devin via the "
        f"`{GATE_LABEL}` + `{SECURITY_LABEL}` labels. ",
        "Watch the orchestrator dashboard for status.",
        "",
        "---",
        "*Filed automatically by the nightly CVE scan workflow.*",
    ])
    try:
        out = subprocess.run(
            [
                "gh", "issue", "create", "--repo", REPO,
                "--title", title,
                "--label", f"{GATE_LABEL},{SECURITY_LABEL}",
                "--body", body,
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out
    except subprocess.CalledProcessError as e:
        print(f"  ! gh issue create failed: {e.stderr}", file=sys.stderr)
        return None


def main(findings_path: str) -> int:
    findings = json.loads(Path(findings_path).read_text())
    fixable = [f for f in findings if f.get("fixable")]
    print(f"{len(findings)} OSV findings, {len(fixable)} fixable")

    # OSV can return the same CVE under multiple vuln_id aliases (e.g. both
    # a PYSEC and a GHSA id pointing at the same CVE). Collapse to unique
    # CVE ids within this run before doing anything else, otherwise we'd
    # file duplicate issues on a fresh repo where GitHub search returns
    # nothing for either alias.
    seen_in_run: set[str] = set()
    unique_fixable = []
    for f in fixable:
        cve = f["cve_id"]
        if cve in seen_in_run:
            print(f"  • {cve}: duplicate alias in this scan — collapsing")
            continue
        seen_in_run.add(cve)
        unique_fixable.append(f)
    print(f"{len(unique_fixable)} unique CVEs after in-run dedup")

    filed = 0
    skipped = 0
    failed = 0
    for f in unique_fixable:
        cve = f["cve_id"]
        existing = existing_issue_for(cve)
        if existing:
            print(f"  • {cve}: already tracked at {existing} — skipping")
            skipped += 1
            continue
        url = file_issue(f)
        if url:
            print(f"  + {cve}: filed → {url}")
            filed += 1
        else:
            failed += 1

    print(f"\nSummary: filed={filed}, skipped={skipped}, failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: file_issues.py <findings.json>")
    sys.exit(main(sys.argv[1]))
