"""Task-aware model routing (roadmap task 12).

Routes automation tasks to model + effort tiers. Budget-aware fallback
delegates to ``quota.recommend_model`` when quota is constrained.
"""

from __future__ import annotations

from typing import Mapping

from service.quota import recommend_model

# Roadmap table: task → (model, effort)
_TASK_ROUTES: dict[str, dict[str, str]] = {
    "pipeline_dispatch": {"model": "gpt-4.1-mini", "effort": "low"},
    "data_fetch": {"model": "gpt-4.1-mini", "effort": "low"},
    "daily_briefing": {"model": "gpt-4.1-nano", "effort": "medium"},
    "daily_monitor": {"model": "gpt-4.1-nano", "effort": "medium"},
    "optimization": {"model": "gpt-5.5", "effort": "medium"},
    "parameter_suggestion": {"model": "gpt-5.5", "effort": "medium"},
    "dual_review": {"model": "gpt-5.5", "effort": "xhigh"},
    "promotion_review": {"model": "gpt-5.5", "effort": "xhigh"},
    "drift_analysis": {"model": "gpt-5.5", "effort": "medium"},
}

_LOW_COST_MODEL = "gpt-4.1-mini"


def route_model(
    task_type: str,
    *,
    budget_remaining: float | None = None,
    quota_status: str | None = None,
) -> dict[str, str]:
    """Return ``{model, effort, task_type}`` for an automation task."""
    normalized = str(task_type or "").strip().lower()
    route = dict(_TASK_ROUTES.get(normalized, _TASK_ROUTES["daily_monitor"]))
    route["task_type"] = normalized or "daily_monitor"

    quota = str(quota_status or "ok").strip().lower()
    if quota in {"low", "constrained", "exhausted", "blocked"}:
        if budget_remaining is None:
            budget_remaining = 0.0
        route["model"] = recommend_model(float(budget_remaining))
        route["effort"] = "low"
        route["quota_override"] = quota
        return route

    if budget_remaining is not None and float(budget_remaining) < 0.01:
        route["model"] = _LOW_COST_MODEL
        route["effort"] = "low"
        route["budget_override"] = "true"

    return route


def list_task_routes() -> Mapping[str, Mapping[str, str]]:
    """Expose configured routes (read-only copy)."""
    return {key: dict(value) for key, value in _TASK_ROUTES.items()}
