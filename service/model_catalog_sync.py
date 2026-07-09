"""Discover provider models and rebuild the auto-maintained catalog."""

from __future__ import annotations

import json
import logging
import os
import ssl
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from service.model_catalog import (
    CATALOG_VERSION,
    DEFAULT_STALE_THRESHOLD_DAYS,
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

logger = logging.getLogger(__name__)

_MAX_HTTP_RESPONSE_BYTES = 10 * 1024 * 1024
_CODEX_BIN_CANDIDATES = (
    "/usr/local/bin/codex",
    "/usr/bin/codex",
    "/opt/homebrew/bin/codex",
)
_ALLOWED_CODEX_NAMES = frozenset({"codex", "codex-cli"})
_ALLOWED_PROVIDERS = frozenset({"codex", "openai", "anthropic"})


def _allowed_codex_parent_dirs() -> set[str]:
    return {os.path.realpath(os.path.dirname(path)) for path in _CODEX_BIN_CANDIDATES}


def _is_allowed_codex_binary(path: str) -> bool:
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        return False
    if os.path.basename(resolved) not in _ALLOWED_CODEX_NAMES:
        return False
    if os.path.dirname(resolved) not in _allowed_codex_parent_dirs():
        return False
    try:
        if os.stat(resolved).st_uid != os.getuid():
            return False
    except OSError:
        return False
    return True


def _http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    ssl_context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=30, context=ssl_context) as response:
            raw = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if 400 <= int(exc.code) < 500:
            logger.warning("model discovery HTTP %s for %s", exc.code, url)
        raise
    if len(raw) > _MAX_HTTP_RESPONSE_BYTES:
        raise ValueError(f"response exceeds {_MAX_HTTP_RESPONSE_BYTES} bytes")
    return json.loads(raw.decode("utf-8"))


def _resolve_codex_bin() -> str | None:
    for env_name in ("CODEX_CLI_PATH", "CODEX_BIN"):
        explicit = os.environ.get(env_name, "").strip()
        if explicit and _is_allowed_codex_binary(explicit):
            return os.path.realpath(explicit)
    for candidate in _CODEX_BIN_CANDIDATES:
        if _is_allowed_codex_binary(candidate):
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
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
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
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
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
            provider = str(item.get("provider") or "codex").strip().lower()
            if provider not in _ALLOWED_PROVIDERS:
                provider = "codex"
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
        known_models = set(previous.models)
        deprecated = [
            model_id
            for model_id in previous.deprecated
            if model_id not in discovered_ids and model_id in known_models
        ]
    for model_id in discovered_ids:
        absence_counts.pop(model_id, None)
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
    catalog_source: str = "live",
) -> ModelCatalog:
    if not records:
        raise ValueError("build_catalog requires at least one discovered model record")
    discovered_ids = {record.model_id for record in records}
    tiers = assign_tiers(records)
    sticky_days = previous.sticky_days if previous is not None else DEFAULT_STALE_THRESHOLD_DAYS
    tiers = apply_sticky_assignments(tiers, previous, discovered_ids=discovered_ids, sticky_days=sticky_days)
    absence_counts, deprecated = update_absence_counts(
        previous,
        discovered_ids,
        deprecation_misses=previous.deprecation_misses if previous else 2,
    )
    active_records = [record for record in records if record.model_id not in deprecated]
    replacements: dict[str, Any] = {}
    if active_records:
        try:
            replacement_tiers = assign_tiers(active_records)
        except ValueError:
            replacement_tiers = {}
        for tier_name, assignment in tiers.items():
            if assignment.model in deprecated and tier_name in replacement_tiers:
                replacements[tier_name] = replacement_tiers[tier_name]
    if replacements:
        tiers.update(replacements)
    return ModelCatalog(
        version=CATALOG_VERSION,
        synced_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        sync_interval_days=previous.sync_interval_days if previous else 30,
        stale_threshold_days=previous.stale_threshold_days if previous else 35,
        sticky_days=sticky_days,
        deprecation_misses=previous.deprecation_misses if previous else 2,
        catalog_source=catalog_source,
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
    try:
        catalog = build_catalog(records, previous=previous, catalog_source=catalog_source)
    except ValueError:
        logger.warning("build_catalog failed; preserving previous catalog if available")
        if previous is not None:
            return previous
        catalog = build_catalog(bootstrap_records(), catalog_source="bootstrap")
    save_catalog_atomic(catalog, target)
    return catalog


__all__ = [
    "CatalogSyncError",
    "bootstrap_records",
    "build_catalog",
    "discover_all_records",
    "discover_anthropic_models",
    "discover_codex_models",
    "discover_openai_models",
    "sync_catalog",
]
