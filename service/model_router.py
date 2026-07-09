"""Task-aware model routing (roadmap task 12).

Routes automation tasks to model + effort via the auto-maintained model catalog.
"""

from __future__ import annotations

from typing import Mapping

from service.model_resolver import list_task_routes as _list_task_routes
from service.model_resolver import recommend_model
from service.model_resolver import resolve_model as _resolve_model


def route_model(
    task_type: str,
    *,
    budget_remaining: float | None = None,
    quota_status: str | None = None,
) -> dict[str, str]:
    """Return ``{model, effort, task_type, tier, source}`` for an automation task."""
    route = _resolve_model(
        task_type=task_type,
        budget_remaining=budget_remaining,
        quota_status=quota_status,
    )
    quota = str(quota_status or "ok").strip().lower()
    if quota in {"low", "constrained", "exhausted", "blocked"}:
        route["quota_override"] = quota
    if budget_remaining is not None and float(budget_remaining) < 0.01:
        route["budget_override"] = "true"
    return route


def list_task_routes() -> Mapping[str, Mapping[str, str]]:
    return _list_task_routes()


__all__ = ["list_task_routes", "recommend_model", "route_model"]
