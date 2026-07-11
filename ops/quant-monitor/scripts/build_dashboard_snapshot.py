#!/usr/bin/env python3
"""Normalize lifecycle artifacts for the read-only strategy web console.

The web console deliberately consumes a small, stable payload.  Missing or
invalid upstream data is represented as ``unavailable`` instead of being
silently replaced with demo metrics.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUS_ORDER = ("healthy", "watch", "review", "critical")
ALLOWED_DOMAINS = {"us_equity", "hk_equity", "cn_equity", "crypto"}
MAX_STRATEGIES = 100
DECISIONS = {
    "healthy": {
        "code": "auto_advance",
        "label": "系统可自动推进下一阶段",
        "reason": "机器检查通过；仅在预批准的 canary 预算内推进，不自动进入正常 live。",
    },
    "watch": {
        "code": "stay_shadow",
        "label": "继续 shadow 观察",
        "reason": "出现轻度变化，先观察，不扩大资金暴露。",
    },
    "review": {
        "code": "pause_promotion",
        "label": "暂停晋级，人工复核",
        "reason": "健康度进入复核区，先确认数据、成本和风险。",
    },
    "critical": {
        "code": "auto_pause",
        "label": "自动暂停 / 回滚复核",
        "reason": "触发严重告警，系统先控制风险，不等待人工确认。",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "health_file_missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "health_file_invalid"
    if not isinstance(value, dict):
        return None, "health_payload_not_object"
    return value, None


def _review_index(review_dir: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not review_dir.exists():
        return index
    for path in sorted(review_dir.rglob("*.json")):
        payload, _ = _load_json(path)
        if not payload:
            continue
        profile = _clean_id(payload.get("profile") or payload.get("strategy_profile"))
        if profile:
            index[profile] = {
                "requested_stage": _clean_text(payload.get("requested_stage"), 80),
                "validation": _clean_summary(payload.get("validation")),
                "risk": _clean_summary(payload.get("risk")),
                "kelly_readiness": _clean_summary(payload.get("kelly_readiness")),
                "evidence_package_id": _clean_text(payload.get("evidence_package_id"), 120),
            }
    return index


def _clean_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text.lower() if text and len(text) <= 120 and all(char.isalnum() or char in "._=-" for char in text) else None


def _clean_text(value: Any, max_length: int, default: str | None = None) -> str | None:
    if value is None or value == "":
        return default
    text = str(value).strip()
    if (
        not text
        or len(text) > max_length
        or any(char in text for char in "<>\\")
        or text.startswith("/")
        or re.match(r"^[A-Za-z]:[\\/]", text)
        or re.search(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", text, re.IGNORECASE)
        or re.search(
            r"\b(?:token|secret|password|api[_ -]?key|private[_ -]?key|cookie)\s*[:=]\s*[^\s,;]{8,}",
            text,
            re.IGNORECASE,
        )
        or re.search(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,})\b", text)
        or re.search(r"(?:^|[\s(])(?:/Users/|/home/|[A-Za-z]:[\\/])", text)
    ):
        return default
    return text


def _clean_timestamp(value: Any) -> str | None:
    text = _clean_text(value, 64)
    if not text or "T" not in text or not re.match(r"^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$", text):
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _clean_score(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100 else None


def _clean_summary(value: Any) -> dict[str, bool | float | int | str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, bool | float | int | str] = {}
    for key, raw in list(value.items())[:12]:
        if not isinstance(key, str) or len(key) > 48 or not all(char.isalnum() or char in "._-" for char in key):
            continue
        if any(marker in key.lower() for marker in ("token", "secret", "password", "cookie", "private", "path", "key")):
            continue
        if isinstance(raw, bool) or (isinstance(raw, (int, float)) and not isinstance(raw, bool)):
            result[key] = raw
        elif isinstance(raw, str) and _clean_text(raw, 120) is not None:
            result[key] = raw.strip()
    return result


def _clean_freshness(value: Any) -> dict[str, str | int | None]:
    if not isinstance(value, dict):
        return {"status": "unknown", "age_seconds": None}
    status = value.get("status") if value.get("status") in {"fresh", "stale", "unknown"} else "unknown"
    age = value.get("age_seconds")
    if isinstance(age, bool):
        age = None
    try:
        age = round(float(age)) if age is not None else None
    except (TypeError, ValueError):
        age = None
    if age is not None and not 0 <= age <= 315_360_000:
        age = None
    return {"status": status, "age_seconds": age}


def _decision(status: str, requested_stage: str | None = None) -> dict[str, str]:
    if status == "healthy" and requested_stage in {"live_candidate", "runtime_enabled"}:
        return {
            "code": "human_live_gate",
            "label": "机器检查通过，等待你批准正常 live",
            "reason": "进入正常资金暴露前仍需人工确认，不会因健康分自动上线。",
        }
    return dict(DECISIONS.get(status, {
        "code": "evidence_missing",
        "label": "证据不足，保持研究态",
        "reason": "没有可用的机器检查结果，不能据此晋级。",
    }))


def build_payload(
    *,
    health_file: Path,
    review_dir: Path | None = None,
) -> dict[str, Any]:
    health, error = _load_json(health_file)
    reviews = _review_index(review_dir) if review_dir else {}
    strategies: list[dict[str, Any]] = []
    errors: list[str] = [error] if error else []

    upstream_status = (health or {}).get("data_status")
    if upstream_status not in {None, "ready", "stale", "unavailable"}:
        errors.append("data_status_invalid")
        upstream_status = "unavailable"

    raw_strategies = (health or {}).get("strategies", [])
    if upstream_status == "unavailable":
        raw_strategies = []
    if not isinstance(raw_strategies, list):
        errors.append("strategies_not_array")
        raw_strategies = []

    for raw in raw_strategies[:MAX_STRATEGIES]:
        if not isinstance(raw, dict):
            errors.append("strategy_entry_not_object")
            continue
        profile = _clean_id(raw.get("strategy_profile") or raw.get("profile"))
        domain = str(raw.get("domain") or "").strip().lower()
        status = str(raw.get("status") or "").strip().lower()
        if not profile:
            errors.append("strategy_profile_invalid")
            continue
        if domain not in ALLOWED_DOMAINS:
            errors.append("strategy_domain_invalid")
            continue
        if status not in STATUS_ORDER:
            errors.append("strategy_status_invalid")
            continue
        review = reviews.get(profile, {})
        requested_stage = review.get("requested_stage")
        decision = _decision(status, requested_stage)
        strategies.append({
            "profile": profile,
            "domain": domain,
            "as_of": _clean_text(raw.get("as_of"), 64),
            "status": status,
            "decision": decision,
            "score": _clean_score(raw.get("overall_score")),
            "components": {
                "performance": _clean_score(raw.get("performance_score")),
                "risk": _clean_score(raw.get("risk_score")),
                "decay": _clean_score(raw.get("decay_score")),
                "stability": _clean_score(raw.get("stability_score")),
                "operations": _clean_score(raw.get("operational_score")),
            },
            "review": {
                "requested_stage": review.get("requested_stage"),
                "evidence_package_id": review.get("evidence_package_id"),
                "validation": review.get("validation", {}),
                "risk": review.get("risk", {}),
                "kelly_readiness": review.get("kelly_readiness", {}),
            },
            "freshness": _clean_freshness(raw.get("freshness")),
            "source_revision": _clean_text(raw.get("source_revision"), 120),
        })

    counts = {status: sum(1 for item in strategies if item["status"] == status) for status in STATUS_ORDER}
    raw_computed_at = (health or {}).get("computed_at")
    computed_at = _clean_timestamp(raw_computed_at)
    if raw_computed_at not in (None, "") and computed_at is None:
        errors.append("computed_at_invalid")
    return {
        "schema_version": "strategy_health_dashboard.v1",
        "generated_at": _now(),
        "computed_at": computed_at,
        "data_status": upstream_status or ("ready" if health is not None else "unavailable"),
        "errors": list(dict.fromkeys(error_code for error_code in errors if error_code)),
        "summary": {
            "strategy_count": len(strategies),
            **counts,
        },
        "policy": {
            "mode": "read_only",
            "automatic_stages": ["research_backtest_only", "ai_monitored_candidate", "shadow_candidate"],
            "automatic_modes": ["bounded_canary_run"],
            "human_gate_stages": ["live_candidate", "runtime_enabled"],
            "canary_requirements": ["预批准固定预算", "固定最长运行时间", "最大回撤熔断", "禁止自动加仓与加杠杆"],
            "machine_checks": [
                "数据新鲜度与完整性",
                "收益/风险/衰减/稳定性/运维健康度",
                "成本、OOS/WFA 与证据包（如已提供）",
            ],
            "human_actions": ["批准正常 live", "批准提高资金/杠杆", "确认例外覆盖"],
            "notice": "研究、shadow、canary 可按固定规则自动推进；正常 live 与资金/杠杆变更必须人工确认。",
        },
        "strategies": strategies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-dir", type=Path, default=None)
    args = parser.parse_args()

    payload = build_payload(health_file=args.health_file, review_dir=args.review_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": args.output.name, "data_status": payload["data_status"], "strategies": len(payload["strategies"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
