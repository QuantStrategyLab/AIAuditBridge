"""Detect when roadmap task-11 dual-review pipeline should run."""

from __future__ import annotations

from typing import Any, Mapping

from service.dual_review import DualReviewTrigger

_PROMOTION_FROM = frozenset({"shadow_candidate", "shadow", "research"})
_PROMOTION_TO = frozenset({"live_candidate", "live_ready", "live"})

_HIT_RATE_MONTHS = 3
_HIT_RATE_THRESHOLD = 0.6

_DRIFT_SIGMA_CRITICAL = 3.0
_DRIFT_SCORE_CRITICAL = 0.75


def promotion_trigger(*, old_status: str, new_status: str) -> bool:
    old = str(old_status or "").strip().lower()
    new = str(new_status or "").strip().lower()
    return old in _PROMOTION_FROM and new in _PROMOTION_TO


def hit_rate_trigger(
    monthly_hit_rates: list[float],
    *,
    months: int = _HIT_RATE_MONTHS,
    threshold: float = _HIT_RATE_THRESHOLD,
) -> bool:
    if len(monthly_hit_rates) < months:
        return False
    window = monthly_hit_rates[-months:]
    return all(float(rate) < threshold for rate in window)


def drift_trigger(
    *,
    drift_sigma: float | None = None,
    drift_score: float | None = None,
) -> bool:
    if drift_sigma is not None and float(drift_sigma) > _DRIFT_SIGMA_CRITICAL:
        return True
    if drift_score is not None and float(drift_score) >= _DRIFT_SCORE_CRITICAL:
        return True
    return False


def resolve_trigger(payload: Mapping[str, Any]) -> DualReviewTrigger | None:
    """Infer dual-review trigger from a structured request payload."""
    explicit = str(payload.get("trigger") or "").strip().lower()
    if explicit:
        try:
            return DualReviewTrigger(explicit)
        except ValueError:
            return None

    if promotion_trigger(
        old_status=str(payload.get("old_status") or ""),
        new_status=str(payload.get("new_status") or ""),
    ):
        return DualReviewTrigger.PROMOTION

    monthly = payload.get("monthly_hit_rates")
    if isinstance(monthly, list) and hit_rate_trigger([float(x) for x in monthly]):
        return DualReviewTrigger.HIT_RATE

    if drift_trigger(
        drift_sigma=_as_optional_float(payload.get("drift_sigma")),
        drift_score=_as_optional_float(payload.get("drift_score")),
    ):
        return DualReviewTrigger.DRIFT

    return None


def _as_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed
