"""Bridge quant-monitor briefing reports into dual-review requests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from service.dual_review_triggers import drift_trigger, hit_rate_trigger, promotion_trigger


def collect_dual_review_payloads(report_dir: Path) -> list[dict[str, Any]]:
    """Scan briefing JSON files for strategies that require dual review."""
    payloads: list[dict[str, Any]] = []
    if not report_dir.is_dir():
        return payloads

    for path in sorted(report_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        domain = str(data.get("domain") or path.stem.replace("_", " "))
        strategies = data.get("strategies")
        if not isinstance(strategies, list):
            continue
        for strategy in strategies:
            if not isinstance(strategy, dict):
                continue
            payload = _payload_from_strategy(strategy, domain=domain)
            if payload is not None:
                payloads.append(payload)
    return payloads


def _payload_from_strategy(strategy: dict[str, Any], *, domain: str) -> dict[str, Any] | None:
    profile = str(strategy.get("strategy_profile") or strategy.get("profile") or "").strip()
    if not profile:
        return None

    primary_review = strategy.get("primary_review")
    if not isinstance(primary_review, dict):
        return None

    payload: dict[str, Any] = {
        "strategy_profile": profile,
        "domain": domain,
        "primary_review": primary_review,
    }

    old_status = strategy.get("old_status")
    new_status = strategy.get("new_status") or strategy.get("status")
    if promotion_trigger(old_status=str(old_status or ""), new_status=str(new_status or "")):
        payload["trigger"] = "promotion"
        payload["old_status"] = old_status
        payload["new_status"] = new_status
        return payload

    monthly = strategy.get("monthly_hit_rates")
    if isinstance(monthly, list) and hit_rate_trigger([float(x) for x in monthly]):
        payload["trigger"] = "hit_rate"
        payload["monthly_hit_rates"] = monthly
        return payload

    drift_sigma = strategy.get("drift_sigma")
    drift_score = strategy.get("drift_score")
    if drift_trigger(
        drift_sigma=float(drift_sigma) if drift_sigma is not None else None,
        drift_score=float(drift_score) if drift_score is not None else None,
    ):
        payload["trigger"] = "drift"
        payload["drift_sigma"] = drift_sigma
        payload["drift_score"] = drift_score
        return payload

    return None


def summarize_dual_review_runs(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(results)
    disagreements = sum(1 for item in items if item.get("outcome") == "disagreement")
    return {
        "count": len(items),
        "disagreements": disagreements,
        "results": items,
    }
