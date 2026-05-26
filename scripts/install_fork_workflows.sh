#!/usr/bin/env bash
# Install the orchestrator's GitHub Actions into a Superset fork.
#
# Usage:
#   ./scripts/install_fork_workflows.sh <path-to-fork-checkout>
#
# Or with env defaults:
#   FORK_PATH=./superset-fork ./scripts/install_fork_workflows.sh
#
# Idempotent: re-running with no source changes produces a no-op commit
# attempt that git rejects with "nothing to commit" (exit 1 from `git
# commit`), which this script handles cleanly.
set -euo pipefail

FORK_PATH="${1:-${FORK_PATH:-}}"
if [[ -z "${FORK_PATH}" ]]; then
  echo "Usage: $0 <path-to-fork-checkout>" >&2
  echo "  or set FORK_PATH=<path>" >&2
  exit 2
fi
if [[ ! -d "${FORK_PATH}/.git" ]]; then
  echo "ERROR: ${FORK_PATH} is not a git checkout" >&2
  exit 2
fi

# Resolve to absolute paths so cd-around-script is safe.
ORCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORK_ABS="$(cd "${FORK_PATH}" && pwd)"

echo "→ Installing workflows from ${ORCH_ROOT} into ${FORK_ABS}"

mkdir -p "${FORK_ABS}/.github/workflows" "${FORK_ABS}/.github/scripts"

# Workflows live in superset-fork-scripts/ in the orchestrator repo; copy
# them into the fork's .github/.
cp "${ORCH_ROOT}/superset-fork-scripts/cve-scan.yml" \
   "${FORK_ABS}/.github/workflows/cve-scan.yml"
cp "${ORCH_ROOT}/superset-fork-scripts/uv-lock-consistency.yml" \
   "${FORK_ABS}/.github/workflows/uv-lock-consistency.yml"

# Scanner scripts: the canonical scan_osv.py lives in scripts/; file_issues.py
# in superset-fork-scripts/. Both end up in .github/scripts/.
cp "${ORCH_ROOT}/scripts/scan_osv.py"                "${FORK_ABS}/.github/scripts/scan_osv.py"
cp "${ORCH_ROOT}/superset-fork-scripts/file_issues.py" \
   "${FORK_ABS}/.github/scripts/file_issues.py"

cd "${FORK_ABS}"
git add .github/workflows/cve-scan.yml \
        .github/workflows/uv-lock-consistency.yml \
        .github/scripts/scan_osv.py \
        .github/scripts/file_issues.py

if git diff --cached --quiet; then
  echo "✓ Already up to date — nothing to commit."
  exit 0
fi

git commit -m "Install Devin Maintenance Orchestrator workflows

Adds nightly CVE-scan + uv-lock-consistency CI to this fork, plus the
scanner scripts they call. See
github.com/rakshith48/cognition-devin-automation for the orchestrator
service that hears the issues these workflows file."

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "→ Pushing ${CURRENT_BRANCH} to origin..."
git push origin "${CURRENT_BRANCH}"

echo ""
echo "✓ Workflows installed and pushed."
echo ""
echo "Trigger the CVE scan now (or wait for the nightly cron):"
echo "  gh workflow run cve-scan.yml --repo \$(gh repo view --json nameWithOwner -q .nameWithOwner)"
