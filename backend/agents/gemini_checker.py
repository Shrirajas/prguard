"""Independent checker ("maker != checker").

A model from a different family re-checks each finding against the diff. It is
given the findings and the code only, never the specialists' reasoning.
The spec calls for Gemini; any provider works as long as the family differs
from the reviewers', and the notes say which model actually ran.
"""

from __future__ import annotations

import json
from typing import Any

from backend.agents import llm, prompts
from backend.agents.specialists import extract_items
from backend.config import settings
from backend.models import Finding

_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}


async def check(findings: list[Finding], diff: str) -> tuple[list[Finding], str, dict[str, Any]]:
    """Return (findings with REJECTED ones dropped, notes, llm usage)."""
    if not findings:
        return findings, "", {}
    if settings.checker is None:
        return findings, "independent checker not configured (CHECKER_MODEL); findings are unverified", {}
    provider, model = settings.checker

    payload = [
        {"index": i, **f.model_dump(include={"severity", "category", "summary", "file_path", "line_start", "line_end", "rationale"})}
        for i, f in enumerate(findings)
    ]
    user = f"FINDINGS:\n{json.dumps(payload, indent=1)}\n\n<pr_content>\n{prompts.defang(diff)[:40000]}\n</pr_content>"
    try:
        result = await llm.chat(provider, model, prompts.CHECKER, user, max_tokens=2500)
    except llm.LLMError as exc:
        return findings, f"independent checker failed ({str(exc)[:120]}); findings are unverified", {}
    verdicts = extract_items(result.text, "verdicts")
    if not verdicts:
        return findings, f"independent checker ({result.model or model}) returned no usable verdicts; findings are unverified", result.usage()

    by_index = {v.get("index"): v for v in verdicts}
    kept: list[Finding] = []
    rejected = 0
    for i, finding in enumerate(findings):
        verdict = by_index.get(i)
        if not verdict:
            kept.append(finding)
            continue
        status = str(verdict.get("status", "")).upper()
        reason = str(verdict.get("reason", ""))[:300]
        if status == "REJECTED":
            rejected += 1
            continue
        update: dict[str, Any] = {
            "checker_status": status if status in ("CONFIRMED", "DOWNGRADED") else "UNVERIFIED",
            "checker_reason": reason,
        }
        if status == "DOWNGRADED" and str(verdict.get("severity", "")).upper() in _SEVERITIES:
            update["severity"] = str(verdict["severity"]).upper()
        kept.append(finding.model_copy(update=update))

    notes = f"{result.model or model} checked {len(findings)} findings, rejected {rejected}"
    if not settings.llm_is_mock and llm.family(provider, model) == llm.family(settings.provider, settings.llm_model):
        notes += " (warning: same model family as the reviewers, so this is not an independent check)"
    return kept, notes, result.usage()
