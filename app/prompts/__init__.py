"""Devin prompts. Pure builders — one prompt type per module."""
from app.prompts.ci_fix import CiFixContext, build_ci_fix_prompt
from app.prompts.cve import CveContext, build_cve_prompt, cve_id_slug, parse_issue_to_cve_context

__all__ = [
    "CveContext",
    "build_cve_prompt",
    "cve_id_slug",
    "parse_issue_to_cve_context",
    "CiFixContext",
    "build_ci_fix_prompt",
]
