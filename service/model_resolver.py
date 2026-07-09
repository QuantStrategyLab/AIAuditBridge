"""Resolve semantic task/tier requests to concrete models via auto catalog."""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Mapping

from service.model_catalog import ModelCatalog, catalog_path, seed_catalog_path
from service.model_catalog_sync import sync_catalog

logger = logging.getLogger(__name__)

_TASK_TIERS: dict[str, str] = {
    "pipeline_dispatch": "fast",
    "data_fetch": "fast",
    "daily_briefing": "nano",
    "daily_monitor": "nano",
    "optimization": "capable",
    "parameter_suggestion": "capable",
    "dual_review": "flagship",
    "promotion_review": "flagship",
    "drift_analysis": "capable",
}

_TASK_EFFORT: dict[str, str] = {
    "pipeline_dispatch": "low",
    "data_fetch": "low",
    "daily_briefing": "medium",
    "daily_monitor": "medium",
    "optimization": "medium",
    "parameter_suggestion": "medium",
    "dual_review": "xhigh",
    "promotion_review": "xhigh",
    "drift_analysis": "medium",
}

_EFFORT_TIER_FALLBACK: dict[str, str] = {
    "low": "fast",
    "medium": "standard",
    "high": "capable",
    "xhigh": "flagship",
}

_catalog_lock = threading.Lock()
_catalog_ready = threading.Condition(_catalog_lock)
_catalog_cache: ModelCatalog | None = None
_catalog_cache_mtime_ns: int | None = None
_catalog_loading = False


def _load_catalog_with_mtime(path) -> tuple[ModelCatalog, int | None]:
    from pathlib import Path

    from service.model_catalog import ModelCatalog as CatalogCls

    target = Path(path)
    backup = target.with_name(target.name + ".prev")

    def _read(candidate: Path) -> tuple[ModelCatalog, int | None]:
        with candidate.open("rb") as handle:
            payload = json.loads(handle.read().decode("utf-8"))
            mtime_ns = os.fstat(handle.fileno()).st_mtime_ns
        return CatalogCls.from_dict(payload), int(mtime_ns)

    try:
        return _read(target)
    except FileNotFoundError:
        if backup.is_file():
            return _read(backup)
        raise
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, OSError, UnicodeDecodeError):
        if backup.is_file():
            return _read(backup)
        raise


def _load_or_sync_catalog() -> ModelCatalog:
    global _catalog_cache, _catalog_cache_mtime_ns, _catalog_loading
    path = catalog_path()
    with _catalog_ready:
        while _catalog_loading:
            _catalog_ready.wait()
        try:
            current_mtime = path.stat().st_mtime_ns
        except OSError:
            current_mtime = None
        if (
            _catalog_cache is not None
            and current_mtime is not None
            and _catalog_cache_mtime_ns == current_mtime
        ):
            return _catalog_cache
        _catalog_loading = True
        load_path = path

    try:
        try:
            catalog, mtime_ns = _load_catalog_with_mtime(load_path)
        except FileNotFoundError:
            seed = seed_catalog_path()
            if load_path.resolve() != seed.resolve() and seed.is_file():
                # Never persist bootstrap into a production/runtime path on cold start.
                catalog, mtime_ns = _load_catalog_with_mtime(seed)
            else:
                catalog = sync_catalog(output_path=str(load_path), force=True)
                try:
                    mtime_ns = load_path.stat().st_mtime_ns
                except OSError:
                    mtime_ns = None
    except Exception:
        with _catalog_ready:
            _catalog_loading = False
            _catalog_ready.notify_all()
        raise

    with _catalog_ready:
        _catalog_cache = catalog
        _catalog_cache_mtime_ns = mtime_ns
        _catalog_loading = False
        _catalog_ready.notify_all()
        return _catalog_cache


def reset_catalog_cache() -> None:
    global _catalog_cache, _catalog_cache_mtime_ns, _catalog_loading
    with _catalog_ready:
        _catalog_cache = None
        _catalog_cache_mtime_ns = None
        _catalog_loading = False
        _catalog_ready.notify_all()


def tier_for_task(task_type: str) -> str:
    normalized = str(task_type or "").strip().lower() or "daily_monitor"
    return _TASK_TIERS.get(normalized, "nano")


def effort_for_task(task_type: str) -> str:
    normalized = str(task_type or "").strip().lower() or "daily_monitor"
    return _TASK_EFFORT.get(normalized, "medium")


def tier_for_budget(budget_remaining: float) -> str:
    if budget_remaining < 0.01:
        return "nano"
    if budget_remaining < 0.05:
        return "fast"
    if budget_remaining < 0.20:
        return "standard"
    return "capable"


def resolve_model(
    *,
    task_type: str | None = None,
    tier: str | None = None,
    effort: str | None = None,
    budget_remaining: float | None = None,
    quota_status: str | None = None,
    requested_model: str = "",
) -> dict[str, str]:
    explicit = str(requested_model or "").strip()
    if explicit and explicit.lower() not in {"auto", "tier:auto"}:
        return {
            "model": explicit,
            "effort": str(effort or effort_for_task(task_type or "") or "medium"),
            "task_type": str(task_type or ""),
            "tier": tier or "",
            "source": "explicit_override",
        }

    catalog = _load_or_sync_catalog()
    normalized_task = str(task_type or "").strip().lower() or "daily_monitor"
    resolved_effort = str(effort or effort_for_task(normalized_task) or "medium")
    resolved_tier = str(tier or tier_for_task(normalized_task) or "standard")

    quota = str(quota_status or "ok").strip().lower()
    if quota in {"low", "constrained", "exhausted", "blocked"}:
        resolved_tier = tier_for_budget(float(budget_remaining or 0.0))
        resolved_effort = "low"
    elif budget_remaining is not None and float(budget_remaining) < 0.01:
        resolved_tier = "nano"
        resolved_effort = "low"

    if resolved_tier not in catalog.tiers:
        resolved_tier = _EFFORT_TIER_FALLBACK.get(resolved_effort, "standard")

    model_id = catalog.model_for_tier(resolved_tier)
    effort_out = resolved_effort
    if quota in {"low", "constrained", "exhausted", "blocked"} or (
        budget_remaining is not None and float(budget_remaining) < 0.01
    ):
        effort_out = "low"
    return {
        "model": model_id,
        "effort": effort_out,
        "task_type": normalized_task,
        "tier": resolved_tier,
        "source": "model_catalog",
    }


def list_task_routes() -> Mapping[str, Mapping[str, str]]:
    catalog = _load_or_sync_catalog()
    output: dict[str, dict[str, str]] = {}
    for task_type, tier_name in _TASK_TIERS.items():
        assignment = catalog.tiers.get(tier_name)
        output[task_type] = {
            "tier": tier_name,
            "model": assignment.model if assignment else catalog.model_for_tier(tier_name),
            "effort": _TASK_EFFORT.get(task_type, "medium"),
        }
    return output


def recommend_model(budget_remaining: float, min_confidence: float = 0.0) -> str:
    _ = min_confidence
    catalog = _load_or_sync_catalog()
    tier = tier_for_budget(float(budget_remaining))
    return catalog.model_for_tier(tier)


__all__ = [
    "effort_for_task",
    "list_task_routes",
    "recommend_model",
    "reset_catalog_cache",
    "resolve_model",
    "tier_for_budget",
    "tier_for_task",
]
