"""Discover provider models and rebuild the auto-maintained catalog."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from service.model_catalog import (
    CATALOG_VERSION,
    ModelCatalog,
    ModelRecord,
    apply_sticky_assignments,
    assign_tiers,
    capability_score_for,
    estimate_cost_per_1m,
    is_chat_candidate,
    load_catalog,
    save_catalog_atomic,
)


_MAX_HTTP_RESPONSE_BYTES = 10 * 1024 * 1024
_CODEX_BIN_CANDIDATES = (
    "/usr/local/bin/codex",
    "/usr/bin/codex",
    "/opt/homebrew/bin/codex",
)


def _http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_HTTP_RESPONSE_BYTES:
        raise ValueError(f"response exceeds {_MAX_HTTP_RESPONSE_BYTES} bytes")
    return json.loads(raw.decode("utf-8"))


def _resolve_codex_bin() -> str | None:
    for env_name in ("CODEX_CLI_PATH", "CODEX_BIN"):
        explicit = os.environ.get(env_name, "").strip()
        if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return os.path.realpath(explicit)
    home_candidate = os.path.expanduser("~/.local/bin/codex")
    candidates = (*_CODEX_BIN_CANDIDATES, home_candidate)
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.realpath(candidate)
    return None


def discover_openai_models() -> list[ModelRecord]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        payload = _http_get_json(
            "https://api.openai.com/v1/models",
            {"Authorization": f"Bearer {api_key}"},
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    records: list[ModelRecord] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or not is_chat_candidate(model_id):
            continue
        created_at = int(item["created"]) if item.get("created") is not None else None
        input_cost, output_cost = estimate_cost_per_1m(model_id)
        records.append(
            ModelRecord(
                model_id=model_id,
                provider="openai",
                created_at=created_at,
                capability_score=capability_score_for(model_id, created_at=created_at),
                input_cost_per_1m=input_cost,
                output_cost_per_1m=output_cost,
                available_on_subscription=True,
            )
        )
    return records


def discover_anthropic_models() -> list[ModelRecord]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        payload = _http_get_json(
            "https://api.anthropic.com/v1/models",
            {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    records: list[ModelRecord] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or not is_chat_candidate(model_id):
            continue
        created_at = None
        if item.get("created_at"):
            try:
                created_at = int(
                    datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00")).timestamp()
                )
            except ValueError:
                created_at = None
        input_cost, output_cost = estimate_cost_per_1m(model_id)
        records.append(
            ModelRecord(
                model_id=model_id,
                provider="anthropic",
                created_at=created_at,
                capability_score=capability_score_for(model_id, created_at=created_at),
                input_cost_per_1m=input_cost,
                output_cost_per_1m=output_cost,
                available_on_subscription=True,
            )
        )
    return records


def discover_codex_models() -> list[ModelRecord]:
    codex_bin = _resolve_codex_bin()
    if not codex_bin:
        return []
    try:
        completed = subprocess.run(
            [codex_bin, "models", "list", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    items = payload if isinstance(payload, list) else payload.get("models") or payload.get("data") or []
    records: list[ModelRecord] = []
    for item in items:
        if isinstance(item, str):
            model_id = item.strip()
            provider = "codex"
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("model") or "").strip()
            provider = str(item.get("provider") or "codex")
        else:
            continue
        if not model_id or not is_chat_candidate(model_id):
            continue
        input_cost, output_cost = estimate_cost_per_1m(model_id)
        records.append(
            ModelRecord(
                model_id=model_id,
                provider=provider,
                capability_score=capability_score_for(model_id),
                input_cost_per_1m=input_cost,
                output_cost_per_1m=output_cost,
                available_on_subscription=True,
            )
        )
    return records


def merge_records(*groups: list[ModelRecord]) -> list[ModelRecord]:
    merged: dict[str, ModelRecord] = {}
    for group in groups:
        for record in group:
            existing = merged.get(record.model_id)
            if existing is None or record.capability_score > existing.capability_score:
                merged[record.model_id] = record
    return list(merged.values())


def bootstrap_records() -> list[ModelRecord]:
    """Offline fallback when provider discovery returns nothing."""
    seeds = (
        ("gpt-5.4-mini", "openai"),
        ("gpt-5.4", "openai"),
        ("gpt-5.5", "openai"),
        ("claude-sonnet-4-6", "anthropic"),
        ("claude-fable-5", "anthropic"),
    )
    records: list[ModelRecord] = []
    for model_id, provider in seeds:
        input_cost, output_cost = estimate_cost_per_1m(model_id)
        records.append(
            ModelRecord(
                model_id=model_id,
                provider=provider,
                capability_score=capability_score_for(model_id),
                input_cost_per_1m=input_cost,
                output_cost_per_1m=output_cost,
                available_on_subscription=True,
            )
        )
    return records


def update_absence_counts(
    previous: ModelCatalog | None,
    discovered_ids: set[str],
    *,
    deprecation_misses: int,
) -> tuple[dict[str, int], list[str]]:
    absence_counts: dict[str, int] = {}
    deprecated: list[str] = []
    if previous is not None:
        absence_counts = dict(previous.absence_counts)
        deprecated = list(previous.deprecated)
    for model_id in discovered_ids:
        absence_counts.pop(model_id, None)
        if model_id in deprecated:
            deprecated.remove(model_id)
    if previous is not None:
        for model_id in previous.models:
            if model_id in discovered_ids:
                continue
            absence_counts[model_id] = int(absence_counts.get(model_id, 0)) + 1
            if absence_counts[model_id] >= deprecation_misses and model_id not in deprecated:
                deprecated.append(model_id)
    return absence_counts, deprecated


def build_catalog(
    records: list[ModelRecord],
    *,
    previous: ModelCatalog | None = None,
) -> ModelCatalog:
    if not records:
        raise ValueError("build_catalog requires at least one discovered model record")
    discovered_ids = {record.model_id for record in records}
    tiers = assign_tiers(records)
    sticky_days = previous.sticky_days if previous is not None else 30
    tiers = apply_sticky_assignments(tiers, previous, discovered_ids=discovered_ids, sticky_days=sticky_days)
    absence_counts, deprecated = update_absence_counts(
        previous,
        discovered_ids,
        deprecation_misses=previous.deprecation_misses if previous else 2,
    )
    active_records = [record for record in records if record.model_id not in deprecated]
    for tier_name, assignment in list(tiers.items()):
        if assignment.model in deprecated and active_records:
            try:
                tiers[tier_name] = assign_tiers(active_records)[tier_name]
            except ValueError:
                continue
    return ModelCatalog(
        version=CATALOG_VERSION,
        synced_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        sync_interval_days=previous.sync_interval_days if previous else 30,
        stale_threshold_days=previous.stale_threshold_days if previous else 35,
        sticky_days=sticky_days,
        deprecation_misses=previous.deprecation_misses if previous else 2,
        catalog_source="live",
        tiers=tiers,
        models={record.model_id: record for record in records},
        deprecated=deprecated,
        absence_counts=absence_counts,
    )


def discover_all_records() -> list[ModelRecord]:
    return merge_records(
        discover_openai_models(),
        discover_anthropic_models(),
        discover_codex_models(),
    )


class CatalogSyncError(RuntimeError):
    """Raised when live discovery fails but a prior catalog exists."""


def sync_catalog(*, output_path: str | None = None, force: bool = False) -> ModelCatalog:
    from pathlib import Path

    from service.model_catalog import catalog_path

    target = Path(output_path) if output_path else catalog_path()
    previous: ModelCatalog | None = None
    if target.is_file():
        previous = load_catalog(target)
        if not force and previous.age_days() < float(previous.sync_interval_days):
            return previous
    records = discover_all_records()
    catalog_source = "live"
    if not records:
        if previous is not None:
            return previous
        records = bootstrap_records()
        catalog_source = "bootstrap"
    catalog = build_catalog(records, previous=previous)
    catalog.catalog_source = catalog_source
    save_catalog_atomic(catalog, target)
    return catalog


__all__ = [
    "bootstrap_records",
    "build_catalog",
    "discover_all_records",
    "discover_anthropic_models",
    "discover_codex_models",
    "discover_openai_models",
    "sync_catalog",
]
