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
    "monthly_snapshot_audit": "flagship",
    "pr_review": "flagship",
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
    "monthly_snapshot_audit": "xhigh",
    "pr_review": "xhigh",
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
    with target.open("rb") as handle:
        payload = json.loads(handle.read().decode("utf-8"))
        mtime_ns = os.fstat(handle.fileno()).st_mtime_ns
    return CatalogCls.from_dict(payload), int(mtime_ns)


def _load_or_sync_catalog() -> ModelCatalog:
    global _catalog_cache, _catalog_cache_mtime_ns, _catalog_loading
    path = catalog_path()
    with _catalog_ready:
        while _catalog_loading:
            _catalog_ready.wait()
        cached = _catalog_cache
        cached_mtime = _catalog_cache_mtime_ns
        if cached is not None and cached_mtime is not None:
            try:
                if path.stat().st_mtime_ns == cached_mtime:
                    return cached
            except OSError:
                pass
        _catalog_loading = True
        load_path = path
        stale_cache = cached

    catalog: ModelCatalog | None = None
    mtime_ns: int | None = None
    try:
        try:
            catalog, mtime_ns = _load_catalog_with_mtime(load_path)
        except FileNotFoundError:
            seed = seed_catalog_path()
            if load_path.resolve() != seed.resolve() and seed.is_file():
                catalog, _seed_mtime = _load_catalog_with_mtime(seed)
                mtime_ns = None
            else:
                catalog = sync_catalog(output_path=str(load_path), force=True)
                try:
                    mtime_ns = load_path.stat().st_mtime_ns
                except OSError:
                    mtime_ns = None
        except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
            if stale_cache is not None:
                logger.warning("catalog reload failed (%s); serving stale cache", exc)
                return stale_cache
            raise
    finally:
        with _catalog_ready:
            if catalog is not None:
                _catalog_cache = catalog
                _catalog_cache_mtime_ns = mtime_ns
            _catalog_loading = False
            _catalog_ready.notify_all()

    assert catalog is not None
    return catalog


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


# Research routes use the execution host's Codex roster, never the API catalog.
# These are deployment defaults, not a claim that every account has the models.
_CODEX_RESEARCH_LEVELS = {"research_summary": 0, "drift_analysis": 1, "optimization": 2, "promotion_review": 3}
_CODEX_RESEARCH_MODELS = ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra")
_CODEX_RESEARCH_EFFORTS = ("low", "medium", "high", "xhigh")


def resolve_codex_research_route(
    *, stage: str, account: dict | None, now: float,
    complexity: str = "low", requested_model: str = "", requested_effort: str = "",
) -> dict:
    """Choose a supported Codex route, then admit it above the quota reserve.

    Complexity can raise the stage floor. Explicit selections are retained or
    rejected, never silently replaced. Defer is a scheduling result, not a
    failed experiment and never permission for API fallback.
    """
    import math

    result = {"action": "defer", "provider": "codex", "reason": "research_route_unavailable", "retry_at": None}
    if stage not in _CODEX_RESEARCH_LEVELS or complexity not in {"low", "medium", "high"}:
        return result
    level = max(_CODEX_RESEARCH_LEVELS[stage], {"low": 0, "medium": 1, "high": 2}[complexity])
    model = requested_model if requested_model not in {"", "auto"} else _CODEX_RESEARCH_MODELS[level]
    effort = requested_effort if requested_effort not in {"", "auto"} else _CODEX_RESEARCH_EFFORTS[level]
    result.update(model=model, reasoning_effort=effort, research_stage=stage)
    model_levels = {**dict(zip(_CODEX_RESEARCH_MODELS, range(4))),
        "gpt-5.5": 2, "gpt-5.4-mini": 0, "gpt-5.3-codex-spark": 0}
    if model not in model_levels:
        return {**result, "reason": "codex_model_quota_mapping_unavailable"}
    if model_levels[model] < level:
        return {**result, "reason": "research_model_below_floor"}
    if effort not in _CODEX_RESEARCH_EFFORTS or _CODEX_RESEARCH_EFFORTS.index(effort) < level:
        return {**result, "reason": "research_effort_below_floor_or_unsupported"}
    if not isinstance(account, dict) or account.get("status") != "available":
        return {**result, "reason": "codex_account_unavailable"}
    updated = account.get("updated_at")
    if type(updated) not in (int, float) or not math.isfinite(updated) or not 0 <= now - updated <= 180:
        return {**result, "reason": "codex_account_stale"}
    models = account.get("available_models")
    selected = next((item for item in models if isinstance(item, dict) and item.get("model") == model), None) if isinstance(models, list) else None
    supported = selected.get("supported_reasoning_efforts") if selected else None
    if not isinstance(supported, list) or effort not in supported:
        return {**result, "reason": "codex_model_or_effort_unavailable"}

    # Separate buckets cannot be added together. Unknown model families need
    # an explicit bucket mapping before this research policy can admit them.
    if model == "gpt-5.3-codex-spark":
        by_id = account.get("rate_limits_by_limit_id")
        limits = by_id.get("codex_bengalfox") if isinstance(by_id, dict) else None
    else:
        limits = account.get("rate_limits")
    windows = [limits.get(key) for key in ("primary", "secondary") if limits.get(key) is not None] if isinstance(limits, dict) else []
    if not windows:
        return {**result, "reason": "codex_quota_unavailable"}
    retry_at = []
    for window in windows:
        if not isinstance(window, dict):
            return {**result, "reason": "codex_quota_invalid"}
        used, duration, reset = (window.get(key) for key in ("used_percent", "window_duration_mins", "resets_at"))
        if (type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100
                or type(duration) is not int or duration <= 0
                or type(reset) not in (int, float) or not math.isfinite(reset) or reset <= now):
            return {**result, "reason": "codex_quota_invalid_or_expired"}
        # Keep 30% of the weekly account for the owner. New heavy research
        # starts only above 50%; short windows retain 20% headroom.
        reserve = (50 if stage == "optimization" else 30) if duration >= 10080 else 20
        if 100 - used <= reserve:
            retry_at.append(reset)
    if retry_at:
        return {**result, "reason": "codex_quota_reserved", "retry_at": max(retry_at)}
    return {**result, "action": "run", "reason": "codex_research_admitted"}
