"""Auto-maintained model catalog — runtime source for tier → model resolution."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

CATALOG_VERSION = 1
DEFAULT_SYNC_INTERVAL_DAYS = 30
DEFAULT_STALE_THRESHOLD_DAYS = 35
DEFAULT_STICKY_DAYS = 35
DEFAULT_DEPRECATION_MISSES = 2

TIER_NAMES = ("nano", "fast", "standard", "capable", "flagship")

_DEFAULT_REPO_CATALOG = (
    Path(__file__).resolve().parents[1] / "generated" / "model_catalog.json"
)


def catalog_path() -> Path:
    raw = os.environ.get("MODEL_CATALOG_PATH", "").strip()
    if raw:
        return Path(raw)
    vps = Path("/var/lib/codex-audit-bridge/model_catalog.json")
    if vps.is_file():
        return vps
    return _DEFAULT_REPO_CATALOG


@dataclass(frozen=True)
class ModelRecord:
    model_id: str
    provider: str
    created_at: int | None = None
    capability_score: float = 0.0
    input_cost_per_1m: float | None = None
    output_cost_per_1m: float | None = None
    available_on_subscription: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TierAssignment:
    tier: str
    model: str
    provider: str
    effort: str = "medium"

    def to_dict(self) -> dict[str, str]:
        return {"tier": self.tier, "model": self.model, "provider": self.provider, "effort": self.effort}


@dataclass
class ModelCatalog:
    version: int = CATALOG_VERSION
    synced_at: str = ""
    sync_interval_days: int = DEFAULT_SYNC_INTERVAL_DAYS
    stale_threshold_days: int = DEFAULT_STALE_THRESHOLD_DAYS
    sticky_days: int = DEFAULT_STICKY_DAYS
    deprecation_misses: int = DEFAULT_DEPRECATION_MISSES
    catalog_source: str = "live"
    last_sync_attempt_at: str = ""
    tiers: dict[str, TierAssignment] = field(default_factory=dict)
    models: dict[str, ModelRecord] = field(default_factory=dict)
    deprecated: list[str] = field(default_factory=list)
    absence_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "synced_at": self.synced_at,
            "sync_interval_days": self.sync_interval_days,
            "stale_threshold_days": self.stale_threshold_days,
            "sticky_days": self.sticky_days,
            "deprecation_misses": self.deprecation_misses,
            "catalog_source": self.catalog_source,
            "last_sync_attempt_at": self.last_sync_attempt_at,
            "tiers": {name: assignment.to_dict() for name, assignment in self.tiers.items()},
            "models": {model_id: record.to_dict() for model_id, record in self.models.items()},
            "deprecated": list(self.deprecated),
            "absence_counts": dict(self.absence_counts),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ModelCatalog:
        tiers_raw = payload.get("tiers") or {}
        tiers: dict[str, TierAssignment] = {}
        for tier_name, spec in tiers_raw.items():
            if not isinstance(spec, Mapping):
                continue
            tiers[str(tier_name)] = TierAssignment(
                tier=str(tier_name),
                model=str(spec.get("model") or ""),
                provider=str(spec.get("provider") or ""),
                effort=str(spec.get("effort") or "medium"),
            )
        models_raw = payload.get("models") or {}
        models: dict[str, ModelRecord] = {}
        for model_id, spec in models_raw.items():
            if not isinstance(spec, Mapping):
                continue
            models[str(model_id)] = ModelRecord(
                model_id=str(model_id),
                provider=str(spec.get("provider") or ""),
                created_at=int(spec["created_at"]) if spec.get("created_at") is not None else None,
                capability_score=float(spec.get("capability_score") or 0.0),
                input_cost_per_1m=(
                    float(spec["input_cost_per_1m"]) if spec.get("input_cost_per_1m") is not None else None
                ),
                output_cost_per_1m=(
                    float(spec["output_cost_per_1m"]) if spec.get("output_cost_per_1m") is not None else None
                ),
                available_on_subscription=bool(spec.get("available_on_subscription", True)),
            )
        return cls(
            version=int(payload.get("version") or CATALOG_VERSION),
            synced_at=str(payload.get("synced_at") or ""),
            sync_interval_days=int(payload.get("sync_interval_days") or DEFAULT_SYNC_INTERVAL_DAYS),
            stale_threshold_days=int(payload.get("stale_threshold_days") or DEFAULT_STALE_THRESHOLD_DAYS),
            sticky_days=int(payload.get("sticky_days") or DEFAULT_STICKY_DAYS),
            deprecation_misses=int(payload.get("deprecation_misses") or DEFAULT_DEPRECATION_MISSES),
            catalog_source=str(payload.get("catalog_source") or "live"),
            last_sync_attempt_at=str(payload.get("last_sync_attempt_at") or ""),
            tiers=tiers,
            models=models,
            deprecated=[str(item) for item in (payload.get("deprecated") or [])],
            absence_counts={
                str(key): int(value)
                for key, value in (payload.get("absence_counts") or {}).items()
            },
        )

    def age_days(self) -> float:
        if not self.synced_at:
            return float("inf")
        synced = datetime.fromisoformat(self.synced_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - synced).total_seconds() / 86400.0

    def is_stale(self) -> bool:
        return self.age_days() > float(self.stale_threshold_days)

    def model_for_tier(self, tier: str) -> str:
        assignment = self.tiers.get(tier) or self.tiers.get("standard")
        if assignment and assignment.model:
            return assignment.model
        if self.models:
            return next(iter(self.models))
        raise RuntimeError("model catalog has no tier assignments")


def load_catalog(path: Path | None = None) -> ModelCatalog:
    target = path or catalog_path()
    if not target.is_file():
        raise FileNotFoundError(f"model catalog not found: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    return ModelCatalog.from_dict(payload)


def save_catalog_atomic(catalog: ModelCatalog, path: Path | None = None) -> Path:
    target = path or catalog_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = target.with_name(target.name + ".prev")
    if target.is_file():
        _write_atomic_text(previous, target.read_text(encoding="utf-8"))
    payload = json.dumps(catalog.to_dict(), indent=2, sort_keys=True) + "\n"
    _write_atomic_text(target, payload)
    return target


def _write_atomic_text(target: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
        handle.write(content)
        temp_name = handle.name
    os.replace(temp_name, target)


_NAME_HINT_SCORES: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"opus|o3|5\.5", re.I), 1.0),
    (re.compile(r"sonnet|fable|5\.4(?!-mini)", re.I), 0.72),
    (re.compile(r"gpt-5|gpt-4o", re.I), 0.68),
    (re.compile(r"mini|haiku", re.I), 0.38),
    (re.compile(r"nano|flash-lite", re.I), 0.22),
)


def estimate_cost_per_1m(model_id: str) -> tuple[float, float]:
    lowered = model_id.lower()
    if "nano" in lowered or "flash-lite" in lowered:
        return 0.10, 0.40
    if "mini" in lowered or "haiku" in lowered:
        return 0.15, 0.60
    if "sonnet" in lowered or "fable" in lowered:
        return 3.0, 15.0
    if "opus" in lowered or "5.5" in lowered or lowered.startswith("o"):
        return 5.0, 25.0
    if "5.4" in lowered:
        return 2.5, 10.0
    return 1.0, 4.0


def capability_score_for(model_id: str, *, created_at: int | None = None) -> float:
    score = 0.35
    for pattern, weight in _NAME_HINT_SCORES:
        if pattern.search(model_id):
            score = max(score, weight)
    if created_at:
        # newer models get a small boost (unix timestamp)
        now = datetime.now(timezone.utc).timestamp()
        age_years = max(0.0, (now - float(created_at)) / (86400.0 * 365.0))
        score += max(0.0, 0.15 - age_years * 0.05)
    input_cost, _output_cost = estimate_cost_per_1m(model_id)
    score += min(0.2, input_cost / 40.0)
    return min(1.0, score)


def is_chat_candidate(model_id: str) -> bool:
    lowered = model_id.lower().strip()
    if any(token in lowered for token in ("embed", "tts", "whisper", "dall-e", "moderation", "realtime", "transcribe")):
        return False
    allowed_prefixes = (
        "gpt-",
        "chatgpt-",
        "claude",
        "fable",
        "o1",
        "o2",
        "o3",
        "o4",
    )
    return lowered.startswith(allowed_prefixes)


def assign_tiers(records: list[ModelRecord]) -> dict[str, TierAssignment]:
    eligible = [record for record in records if record.available_on_subscription and is_chat_candidate(record.model_id)]
    if not eligible:
        raise ValueError("no eligible chat models discovered")

    by_score = sorted(eligible, key=lambda item: (item.capability_score, item.model_id), reverse=True)
    by_cost = sorted(eligible, key=lambda item: (item.input_cost_per_1m or 999.0, item.model_id))

    def _pick_mini() -> ModelRecord:
        mini = [item for item in by_cost if re.search(r"mini|haiku", item.model_id, re.I)]
        return mini[0] if mini else by_cost[0]

    def _pick_nano() -> ModelRecord:
        nano = [item for item in by_cost if re.search(r"nano|flash-lite", item.model_id, re.I)]
        return nano[0] if nano else _pick_mini()

    def _pick_standard() -> ModelRecord:
        mid = [item for item in by_score if 0.45 <= item.capability_score <= 0.8]
        return mid[len(mid) // 2] if mid else by_score[min(2, len(by_score) - 1)]

    flagship = by_score[0]
    capable = by_score[min(2, len(by_score) - 1)]
    standard = _pick_standard()
    fast = _pick_mini()
    nano = _pick_nano()

    effort_map = {
        "nano": "low",
        "fast": "low",
        "standard": "medium",
        "capable": "high",
        "flagship": "xhigh",
    }
    picks = {
        "nano": nano,
        "fast": fast,
        "standard": standard,
        "capable": capable,
        "flagship": flagship,
    }
    return {
        tier: TierAssignment(
            tier=tier,
            model=record.model_id,
            provider=record.provider,
            effort=effort_map[tier],
        )
        for tier, record in picks.items()
    }


def apply_sticky_assignments(
    new_tiers: dict[str, TierAssignment],
    previous: ModelCatalog | None,
    *,
    discovered_ids: set[str],
    sticky_days: int,
) -> dict[str, TierAssignment]:
    if previous is None or previous.age_days() > float(sticky_days):
        return new_tiers
    merged = dict(new_tiers)
    for tier, old_assignment in previous.tiers.items():
        new_assignment = merged.get(tier)
        if new_assignment is None or new_assignment.model == old_assignment.model:
            continue
        model_id = old_assignment.model
        if model_id in discovered_ids and model_id not in previous.deprecated:
            merged[tier] = old_assignment
    return merged


__all__ = [
    "ModelCatalog",
    "ModelRecord",
    "TierAssignment",
    "apply_sticky_assignments",
    "assign_tiers",
    "capability_score_for",
    "catalog_path",
    "is_chat_candidate",
    "load_catalog",
    "save_catalog_atomic",
]
