"""Parallel GPT + Claude secondary reviews for dual-review (roadmap task 11, plan B)."""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any

from service.adapters.llm_adapter import LlmAdapter, LlmResult
from service.dual_review import VERDICT_INVALID, VERDICT_UNAVAILABLE, extract_confidence, extract_verdict
from service.model_router import default_dual_review_model_for_reviewer

if TYPE_CHECKING:
    from service.dual_review_orchestrator import DualReviewRequest

_DEFAULT_GPT_MODEL = "gpt-5.4-mini"
_DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
_REVIEW_JSON_RE = re.compile(r"\{[\s\S]*\}")

_SECONDARY_SYSTEM = (
    "You are an independent quantitative strategy reviewer. "
    "You have NOT seen any other model's answer. "
    "Respond with JSON only: "
    '{"verdict":"approve"|"reject","confidence":0.0-1.0,"summary":"..."}'
)


def secondary_mode() -> str:
    """``dual_api`` (default) runs GPT + Claude; ``stub`` keeps the placeholder."""
    return str(os.environ.get("DUAL_REVIEW_SECONDARY_MODE", "dual_api")).strip().lower()


def parse_llm_review_output(output: str, *, provider: str, model: str) -> dict[str, Any]:
    """Parse LLM JSON output into a normalized review dict."""
    parsed: dict[str, Any] = {
        "source": provider,
        "model": model,
        "raw_output": output,
    }
    if not output.strip():
        parsed.update({"verdict": VERDICT_INVALID, "confidence": 0.0, "parse_error": "empty_output"})
        return parsed

    try:
        match = _REVIEW_JSON_RE.search(output)
        if not match:
            raise ValueError("no JSON object in output")
        obj = json.loads(match.group(0))
        if not isinstance(obj, dict):
            raise ValueError("review JSON must be an object")
        verdict = str(obj.get("verdict") or obj.get("decision") or "").strip().lower()
        confidence_raw = obj.get("confidence", obj.get("ai_confidence", 0.5))
        confidence = float(confidence_raw)
        confidence = max(0.0, min(1.0, confidence))
        parsed.update(
            {
                "verdict": verdict or "reject",
                "confidence": confidence,
                "summary": str(obj.get("summary") or obj.get("reason") or "").strip(),
            }
        )
        return parsed
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        parsed.update(
            {
                "verdict": VERDICT_INVALID,
                "confidence": 0.0,
                "parse_error": str(exc),
            }
        )
        return parsed


def build_secondary_prompt(request: DualReviewRequest) -> str:
    """Build an independent secondary-review prompt (no primary verdict leakage)."""
    context = request.context or {}
    lines = [
        f"Strategy profile: {request.strategy_profile}",
        f"Trigger: {request.trigger.value}",
    ]
    for key in ("domain", "old_status", "new_status", "drift_sigma", "drift_score"):
        if key in context and context[key] not in (None, ""):
            lines.append(f"{key}: {context[key]}")
    monthly = context.get("monthly_hit_rates")
    if isinstance(monthly, list) and monthly:
        lines.append(f"monthly_hit_rates: {monthly}")
    evidence = context.get("evidence_summary") or context.get("summary")
    if evidence:
        lines.append(f"context: {evidence}")
    lines.append(
        "Decide whether this strategy change should proceed. "
        "Use approve only when evidence supports promotion or continued operation."
    )
    return "\n".join(lines)


def _default_model_for_reviewer(reviewer: str) -> str:
    return default_dual_review_model_for_reviewer(reviewer)


def _result_to_review(result: LlmResult) -> dict[str, Any]:
    if not result.success:
        return {
            "source": result.provider,
            "model": result.model,
            "verdict": VERDICT_UNAVAILABLE,
            "confidence": 0.0,
            "error": result.error,
            "latency_seconds": result.latency_seconds,
        }
    review = parse_llm_review_output(result.output, provider=result.provider, model=result.model)
    review["latency_seconds"] = result.latency_seconds
    return review


def run_dual_api_secondary_review(
    request: DualReviewRequest,
    *,
    adapter: LlmAdapter | None = None,
) -> dict[str, Any]:
    """Run parallel GPT + Claude API reviews (plan B)."""
    llm = adapter or LlmAdapter()
    gpt_model = _default_model_for_reviewer("gpt")
    claude_model = _default_model_for_reviewer("claude")
    user_prompt = build_secondary_prompt(request)

    results = llm.parallel_review(
        reviewers=[("gpt", gpt_model), ("claude", claude_model)],
        system=_SECONDARY_SYSTEM,
        user=user_prompt,
    )

    gpt_result = next((r for r in results if r.provider == "openai"), None)
    claude_result = next((r for r in results if r.provider == "anthropic"), None)
    if gpt_result is None and results:
        gpt_result = results[0]
    if claude_result is None and len(results) > 1:
        claude_result = results[1]

    gpt_review = _result_to_review(gpt_result) if gpt_result else {"verdict": VERDICT_UNAVAILABLE, "confidence": 0.0, "error": "gpt_missing"}
    claude_review = _result_to_review(claude_result) if claude_result else {"verdict": VERDICT_UNAVAILABLE, "confidence": 0.0, "error": "claude_missing"}

    return {
        "mode": "dual_api",
        "gpt": gpt_review,
        "claude": claude_review,
        "prompt": user_prompt,
    }


def dual_api_secondary_reviewer(request: DualReviewRequest) -> dict[str, Any]:
    """Secondary reviewer hook: parallel GPT + Claude unless keys are unavailable."""
    if secondary_mode() == "stub":
        from service.dual_review_orchestrator import default_secondary_reviewer

        legacy = default_secondary_reviewer(request)
        return {"mode": "stub", "legacy": legacy}

    try:
        return run_dual_api_secondary_review(request)
    except Exception as exc:
        return {
            "mode": "dual_api",
            "gpt": {"verdict": VERDICT_UNAVAILABLE, "confidence": 0.0, "error": str(exc)},
            "claude": {"verdict": VERDICT_UNAVAILABLE, "confidence": 0.0, "error": str(exc)},
        }


def is_dual_api_secondary(secondary: dict[str, Any] | None) -> bool:
    return isinstance(secondary, dict) and "gpt" in secondary and "claude" in secondary


def normalized_verdict_label(review: dict[str, Any]) -> str | None:
    return extract_verdict(review)


def normalized_confidence(review: dict[str, Any]) -> float | None:
    return extract_confidence(review)
