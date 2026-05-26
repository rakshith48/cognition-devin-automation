"""JSON-Schema definitions for Devin's structured output.

Devin v3 supports `structured_output_schema` on session creation (JSON
Schema Draft 7, up to 64KB). Returning a strict shape instead of free-text
turns the dashboard from a status list into a remediation report — the
fields below are exactly what an engineering lead would want to see at
a glance per remediation.

Keep schemas small. The cost-per-byte against the model is real and the
dashboard only renders a few fields.
"""
from __future__ import annotations

# CVE remediation. Required fields are the ones we always want to display;
# optional fields are best-effort.
CVE_REMEDIATION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {
            "type": "string",
            "maxLength": 500,
            "description": "One-sentence summary of what was changed.",
        },
        "files_changed": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 50,
            "description": "Relative paths of files modified in the PR.",
        },
        "tests_run": {
            "type": "boolean",
            "description": "Whether the repo's test suite was executed locally.",
        },
        "tests_passing": {
            "type": "boolean",
            "description": (
                "Whether the executed tests passed. Set to false if tests_run "
                "is false."
            ),
        },
        "risk_level": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": (
                "Risk assessment: low = patch-only, medium = minor version bump "
                "or non-breaking refactor, high = major version bump or "
                "breaking-change adjacent."
            ),
        },
        "needs_human_review": {
            "type": "boolean",
            "description": (
                "True if the PR has caveats a reviewer must check beyond the "
                "standard PR review (e.g. test was skipped, fallback used, "
                "compatibility uncertain)."
            ),
        },
        "blockers": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 10,
            "description": (
                "Anything that prevented the task from completing fully. "
                "Empty array if task completed."
            ),
        },
    },
    "required": ["summary", "tests_run", "risk_level", "needs_human_review"],
}
