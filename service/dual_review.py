"""Dual-review foundation for Task 11 fallback arbitration.

When the primary reviewer confidence is below threshold, a secondary
independent review is triggered.  ``compare_reviews`` reconciles the two
outcomes into pass / fail / disagreement.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_DISAGREEMENT = "disagreement"

DEFAULT_ESCALATION_THRESHOLD = 0.8

_PASS_VALUES = frozenset({"pass", "approve", "approved", "accept", "accepted"})
_FAIL_VALUES = frozenset({"fail", "reject", "rejected", "deny", "denied", "block", "blocked"})


class DualReviewTrigger(str, Enum):
    """Scenarios that require dual-review arbitration."""

    PROMOTION = "promotion"
    HIT_RATE = "hit_rate"
    DRIFT = "drift"


def _normalize_verdict(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in _PASS_VALUES:
        return VERDICT_PASS
    if normalized in _FAIL_VALUES:
        return VERDICT_FAIL
    return None


def _extract_verdict(review: dict[str, Any]) -> str | None:
    for key in ("verdict", "consensus", "decision", "status", "recommendation"):
        if key in review:
            parsed = _normalize_verdict(review.get(key))
            if parsed is not None:
                return parsed
    return None


def _extract_confidence(review: dict[str, Any]) -> float | None:
    for key in ("confidence", "ai_confidence", "score"):
        if key not in review:
            continue
        try:
            value = float(review[key])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        return value
    return None


def should_escalate(confidence: float, threshold: float = DEFAULT_ESCALATION_THRESHOLD) -> bool:
    """Return True when confidence is below threshold and a second review is needed.

    Invalid / non-finite confidence fails closed (escalate). Corrupted threshold
    falls back to DEFAULT_ESCALATION_THRESHOLD.
    """
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(value):
        return True
    try:
        cutoff = float(threshold)
    except (TypeError, ValueError):
        cutoff = DEFAULT_ESCALATION_THRESHOLD
    if not math.isfinite(cutoff):
        cutoff = DEFAULT_ESCALATION_THRESHOLD
    return value < cutoff


def compare_reviews(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    """Compare two independent review payloads and return a reconciled verdict."""
    primary_verdict = _extract_verdict(primary)
    secondary_verdict = _extract_verdict(secondary)
    primary_confidence = _extract_confidence(primary)
    secondary_confidence = _extract_confidence(secondary)

    if primary_verdict is None or secondary_verdict is None:
        verdict = VERDICT_DISAGREEMENT
        reason = "missing or unrecognized review verdict"
    elif primary_verdict == secondary_verdict:
        verdict = primary_verdict
        reason = "reviews agree"
    else:
        verdict = VERDICT_DISAGREEMENT
        reason = "reviews disagree"

    return {
        "verdict": verdict,
        "reason": reason,
        "primary_verdict": primary_verdict,
        "secondary_verdict": secondary_verdict,
        "primary_confidence": primary_confidence,
        "secondary_confidence": secondary_confidence,
        "agreement": verdict != VERDICT_DISAGREEMENT,
    }


def extract_verdict(review: dict[str, Any]) -> str | None:
    """Public helper for orchestration layers."""
    return _extract_verdict(review)


def extract_confidence(review: dict[str, Any]) -> float | None:
    """Public helper for orchestration layers."""
    return _extract_confidence(review)
