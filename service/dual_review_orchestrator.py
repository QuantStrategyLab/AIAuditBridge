"""Orchestrate primary + secondary dual-review arbitration (roadmap task 11)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from service.dual_review import (
    DEFAULT_ESCALATION_THRESHOLD,
    VERDICT_DISAGREEMENT,
    VERDICT_FAIL,
    VERDICT_PASS,
    DualReviewTrigger,
    compare_reviews,
    extract_confidence,
    extract_verdict,
    should_escalate,
)
from service.dual_review_triggers import resolve_trigger
from service.model_router import route_model

SecondaryReviewer = Callable[["DualReviewRequest"], dict[str, Any]]


@dataclass(frozen=True)
class DualReviewRequest:
    trigger: DualReviewTrigger
    strategy_profile: str
    primary_review: dict[str, Any]
    context: dict[str, Any] = field(default_factory=dict)
    escalation_threshold: float = DEFAULT_ESCALATION_THRESHOLD


@dataclass
class DualReviewResult:
    trigger: DualReviewTrigger
    strategy_profile: str
    primary_review: dict[str, Any]
    secondary_review: dict[str, Any] | None = None
    escalated: bool = False
    comparison: dict[str, Any] | None = None
    outcome: str = ""
    reason: str = ""
    model_route: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger.value,
            "strategy_profile": self.strategy_profile,
            "primary_review": self.primary_review,
            "secondary_review": self.secondary_review,
            "escalated": self.escalated,
            "comparison": self.comparison,
            "outcome": self.outcome,
            "reason": self.reason,
            "model_route": self.model_route,
        }


def build_request_from_payload(payload: dict[str, Any]) -> DualReviewRequest | None:
    trigger = resolve_trigger(payload)
    if trigger is None:
        return None
    strategy_profile = str(payload.get("strategy_profile") or payload.get("profile") or "").strip()
    if not strategy_profile:
        return None
    primary = payload.get("primary_review")
    if not isinstance(primary, dict):
        return None
    threshold = payload.get("escalation_threshold", DEFAULT_ESCALATION_THRESHOLD)
    try:
        cutoff = float(threshold)
    except (TypeError, ValueError):
        cutoff = DEFAULT_ESCALATION_THRESHOLD
    context = {k: v for k, v in payload.items() if k not in {"primary_review", "secondary_review"}}
    return DualReviewRequest(
        trigger=trigger,
        strategy_profile=strategy_profile,
        primary_review=primary,
        context=context,
        escalation_threshold=cutoff,
    )


def default_secondary_reviewer(request: DualReviewRequest) -> dict[str, Any]:
    """Conservative placeholder when no external reviewer is injected."""
    route = route_model("dual_review")
    return {
        "verdict": "reject",
        "confidence": 0.5,
        "source": "secondary_stub",
        "task_type": route.get("task_type", "dual_review"),
        "model": route.get("model", ""),
        "effort": route.get("effort", ""),
        "strategy_profile": request.strategy_profile,
        "trigger": request.trigger.value,
    }


def orchestrate_dual_review(
    request: DualReviewRequest,
    *,
    secondary_reviewer: SecondaryReviewer | None = None,
) -> DualReviewResult:
    """Run primary confidence gate, optional secondary review, and reconciliation."""
    reviewer = secondary_reviewer or default_secondary_reviewer
    route = route_model("dual_review")
    result = DualReviewResult(
        trigger=request.trigger,
        strategy_profile=request.strategy_profile,
        primary_review=request.primary_review,
        model_route=dict(route),
    )

    primary_confidence = extract_confidence(request.primary_review)
    if primary_confidence is None:
        result.escalated = True
        result.reason = "primary confidence missing; escalating"
    elif should_escalate(primary_confidence, threshold=request.escalation_threshold):
        result.escalated = True
        result.reason = f"primary confidence {primary_confidence:.2f} below {request.escalation_threshold:.2f}"
    else:
        primary_verdict = extract_verdict(request.primary_review)
        if primary_verdict == VERDICT_PASS:
            result.outcome = VERDICT_PASS
            result.reason = "primary confidence sufficient; approved without secondary review"
            return result
        if primary_verdict == VERDICT_FAIL:
            result.outcome = VERDICT_FAIL
            result.reason = "primary confidence sufficient; rejected without secondary review"
            return result
        result.escalated = True
        result.reason = "primary verdict unclear; escalating"

    secondary = reviewer(request)
    result.secondary_review = secondary
    comparison = compare_reviews(request.primary_review, secondary)
    result.comparison = comparison
    result.outcome = str(comparison.get("verdict") or VERDICT_DISAGREEMENT)
    if result.outcome == VERDICT_DISAGREEMENT:
        result.reason = str(comparison.get("reason") or "reviews disagree")
    else:
        result.reason = str(comparison.get("reason") or "reviews agree")
    return result


def orchestrate_from_payload(
    payload: dict[str, Any],
    *,
    secondary_reviewer: SecondaryReviewer | None = None,
    secondary_review: dict[str, Any] | None = None,
) -> DualReviewResult | None:
    """Build request from payload and run orchestration."""
    request = build_request_from_payload(payload)
    if request is None:
        return None
    if secondary_review is not None:
        fixed_secondary = dict(secondary_review)

        def reviewer(_req: DualReviewRequest) -> dict[str, Any]:
            return fixed_secondary

        return orchestrate_dual_review(request, secondary_reviewer=reviewer)
    return orchestrate_dual_review(request, secondary_reviewer=secondary_reviewer)
