"""Consume quant-monitor daily briefing JSON and classify alert severity.

Roadmap task 10b:
- all normal → quiet
- deviation ~2σ (review / elevated drift) → github_issue
- deviation >3σ / circuit breaker / critical → telegram
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

LEVEL_QUIET = "quiet"
LEVEL_GITHUB = "github_issue"
LEVEL_TELEGRAM = "telegram"

# Proxy thresholds when literal σ is unavailable in dashboard JSON.
_DRIFT_WARN = 0.50
_DRIFT_CRITICAL = 0.75
_SCORE_REVIEW = 55.0
_SCORE_CRITICAL = 40.0

_CIRCUIT_KEYWORDS = frozenset(
    {
        "circuit_breaker",
        "stop_loss",
        "熔断",
        "止损",
    }
)


class BriefingAction(str, Enum):
    QUIET = LEVEL_QUIET
    GITHUB_ISSUE = LEVEL_GITHUB
    TELEGRAM = LEVEL_TELEGRAM


@dataclass(frozen=True)
class BriefingFinding:
    source: str
    level: BriefingAction
    reason: str
    strategy_profile: str = ""
    domain: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "level": self.level.value,
            "reason": self.reason,
            "strategy_profile": self.strategy_profile,
            "domain": self.domain,
        }


@dataclass
class BriefingConsumptionResult:
    day: str
    report_dir: str
    findings: list[BriefingFinding] = field(default_factory=list)

    @property
    def action(self) -> BriefingAction:
        if any(f.level == BriefingAction.TELEGRAM for f in self.findings):
            return BriefingAction.TELEGRAM
        if any(f.level == BriefingAction.GITHUB_ISSUE for f in self.findings):
            return BriefingAction.GITHUB_ISSUE
        return BriefingAction.QUIET

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "report_dir": self.report_dir,
            "action": self.action.value,
            "findings": [f.to_dict() for f in self.findings],
        }


def _as_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _max_level(current: BriefingAction, candidate: BriefingAction) -> BriefingAction:
    order = {
        BriefingAction.QUIET: 0,
        BriefingAction.GITHUB_ISSUE: 1,
        BriefingAction.TELEGRAM: 2,
    }
    return candidate if order[candidate] > order[current] else current


def _classify_strategy(
    strategy: Mapping[str, Any],
    *,
    source: str,
    domain: str = "",
) -> BriefingFinding | None:
    profile = str(strategy.get("strategy_profile") or strategy.get("profile") or "").strip()
    status = str(strategy.get("status") or "").strip().lower()
    drift_score = _as_float(strategy.get("drift_score"))
    overall_score = _as_float(strategy.get("overall_score"))

    level = BriefingAction.QUIET
    reasons: list[str] = []

    if status == "critical":
        level = BriefingAction.TELEGRAM
        reasons.append("status=critical")
    elif status == "review":
        level = _max_level(level, BriefingAction.GITHUB_ISSUE)
        reasons.append("status=review")

    if drift_score is not None:
        if drift_score >= _DRIFT_CRITICAL:
            level = BriefingAction.TELEGRAM
            reasons.append(f"drift_score={drift_score:.2f}")
        elif drift_score >= _DRIFT_WARN:
            level = _max_level(level, BriefingAction.GITHUB_ISSUE)
            reasons.append(f"drift_score={drift_score:.2f}")

    if overall_score is not None:
        if overall_score <= _SCORE_CRITICAL:
            level = BriefingAction.TELEGRAM
            reasons.append(f"overall_score={overall_score:.1f}")
        elif overall_score <= _SCORE_REVIEW:
            level = _max_level(level, BriefingAction.GITHUB_ISSUE)
            reasons.append(f"overall_score={overall_score:.1f}")

    flags = strategy.get("risk_flags") or strategy.get("alerts") or ()
    if isinstance(flags, Mapping):
        flags = tuple(str(key) for key, enabled in flags.items() if enabled)
    for flag in flags:
        text = str(flag).lower()
        if any(keyword in text for keyword in _CIRCUIT_KEYWORDS):
            level = BriefingAction.TELEGRAM
            reasons.append(f"flag={flag}")

    if level == BriefingAction.QUIET:
        return None
    return BriefingFinding(
        source=source,
        level=level,
        reason="; ".join(reasons) or "anomaly",
        strategy_profile=profile,
        domain=str(strategy.get("domain") or domain),
    )


def _classify_report_payload(
    payload: Mapping[str, Any],
    *,
    source: str,
) -> list[BriefingFinding]:
    findings: list[BriefingFinding] = []

    if payload.get("ok") is False:
        error = str(payload.get("error") or "ok=false")
        level = BriefingAction.TELEGRAM if "circuit" in error.lower() else BriefingAction.GITHUB_ISSUE
        findings.append(
            BriefingFinding(source=source, level=level, reason=error, domain=str(payload.get("domain") or ""))
        )
        return findings

    domain = str(payload.get("domain") or "")
    strategies = payload.get("strategies")
    if isinstance(strategies, list):
        for strategy in strategies:
            if not isinstance(strategy, Mapping):
                continue
            finding = _classify_strategy(strategy, source=source, domain=domain)
            if finding is not None:
                findings.append(finding)

    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        critical = int(summary.get("critical") or 0)
        review = int(summary.get("review") or 0)
        if critical > 0:
            findings.append(
                BriefingFinding(
                    source=source,
                    level=BriefingAction.TELEGRAM,
                    reason=f"summary critical={critical}",
                    domain=domain,
                )
            )
        elif review > 0:
            findings.append(
                BriefingFinding(
                    source=source,
                    level=BriefingAction.GITHUB_ISSUE,
                    reason=f"summary review={review}",
                    domain=domain,
                )
            )

    return findings


def consume_briefing_report(payload: Mapping[str, Any], *, source: str = "report") -> list[BriefingFinding]:
    """Classify a single briefing JSON document."""
    return _classify_report_payload(payload, source=source)


def consume_briefing_dir(report_dir: str | Path, *, day: str = "") -> BriefingConsumptionResult:
    """Load ``*.json`` reports from a daily briefing directory."""
    path = Path(report_dir)
    resolved_day = day or path.name
    findings: list[BriefingFinding] = []

    for file_path in sorted(path.glob("*.json")):
        if file_path.name.startswith("_"):
            continue
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append(
                BriefingFinding(
                    source=file_path.name,
                    level=BriefingAction.GITHUB_ISSUE,
                    reason=f"invalid_json: {exc}",
                )
            )
            continue
        if not isinstance(payload, Mapping):
            continue
        findings.extend(_classify_report_payload(payload, source=file_path.name))

    return BriefingConsumptionResult(day=resolved_day, report_dir=str(path), findings=findings)


def merge_findings(groups: Iterable[Iterable[BriefingFinding]]) -> list[BriefingFinding]:
    """Flatten multiple finding iterables."""
    merged: list[BriefingFinding] = []
    for group in groups:
        merged.extend(group)
    return merged
