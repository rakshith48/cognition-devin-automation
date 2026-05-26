"""Scan a requirements file against OSV.dev for known vulnerabilities.

Used during planning to hand-pick CVEs for the Devin take-home demo.
Also a reference implementation for the GitHub Action scanner later.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_QUERY_URL = "https://api.osv.dev/v1/query"

REQ_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)")


def parse_requirements(path: Path) -> list[tuple[str, str]]:
    pkgs = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = REQ_LINE.match(line)
        if m:
            pkgs.append((m.group(1), m.group(2)))
    return pkgs


def osv_batch(pkgs: list[tuple[str, str]]) -> list[dict]:
    """Returns parallel list of {vulns: [...]} dicts."""
    queries = [
        {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
        for name, version in pkgs
    ]
    body = json.dumps({"queries": queries}).encode()
    req = Request(OSV_BATCH_URL, data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["results"]


def osv_details(vuln_id: str) -> dict:
    body = json.dumps({"id": vuln_id}).encode()
    # OSV's /v1/vulns/{id} GET also works; using POST query for batching parity.
    with urlopen(f"https://api.osv.dev/v1/vulns/{vuln_id}", timeout=30) as resp:
        return json.loads(resp.read())


def extract_fix_version(vuln: dict) -> str | None:
    """Pull the smallest 'fixed' version across PyPI ranges."""
    for aff in vuln.get("affected", []):
        if aff.get("package", {}).get("ecosystem") != "PyPI":
            continue
        for r in aff.get("ranges", []):
            for event in r.get("events", []):
                if "fixed" in event:
                    return event["fixed"]
    return None


def severity_of(vuln: dict) -> str:
    # Prefer CVSS v3 base score if present
    for s in vuln.get("severity", []):
        if "CVSS_V3" in s.get("type", ""):
            return s.get("score", "?")
    db = vuln.get("database_specific", {})
    return db.get("severity") or "UNKNOWN"


def main(req_path: str) -> int:
    pkgs = parse_requirements(Path(req_path))
    print(f"Scanning {len(pkgs)} pinned packages from {req_path}...", file=sys.stderr)

    results = osv_batch(pkgs)
    findings = []
    for (name, version), result in zip(pkgs, results):
        for vuln_stub in result.get("vulns", []):
            vid = vuln_stub["id"]
            vuln = osv_details(vid)
            fix = extract_fix_version(vuln)
            aliases = vuln.get("aliases", [])
            cve = next((a for a in aliases if a.startswith("CVE-")), vid)
            findings.append({
                "package": name,
                "installed_version": version,
                "vuln_id": vid,
                "cve_id": cve,
                "aliases": aliases,
                "summary": vuln.get("summary", "")[:200],
                "severity": severity_of(vuln),
                "fix_version": fix,
                "fixable": fix is not None,
            })

    print(json.dumps(findings, indent=2))
    print(
        f"\nTotal findings: {len(findings)} | "
        f"Fixable: {sum(1 for f in findings if f['fixable'])}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "requirements/base.txt"))
