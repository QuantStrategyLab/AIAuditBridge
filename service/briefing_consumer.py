"""Consume quant-monitor daily briefing JSON and classify routing.

Roadmap task 10b:
- all normal → quiet
- strategy health / drift degradation → issue-only optimization monitor
- data unavailable / circuit breaker → telegram
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    kind: str = "monitoring"
    severity: str = "medium"
    metrics: dict[str, Any] = field(default_factory=dict)
    signals: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "level": self.level.value,
            "reason": self.reason,
            "strategy_profile": self.strategy_profile,
            "domain": self.domain,
            "kind": self.kind,
            "severity": self.severity,
            "metrics": self.metrics,
            "signals": self.signals,
        }


@dataclass
class BriefingConsumptionResult:
    day: str
    report_dir: str
    findings: list[BriefingFinding] = field(default_factory=list)
    # Captured during the same read as rule classification; never raw report text.
    summary_reports: list[dict[str, Any]] = field(default_factory=list, repr=False)

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
    signals: list[dict[str, Any]] = []
    severity = "medium"
    kind = "strategy_monitoring"

    if status == "critical":
        level = BriefingAction.GITHUB_ISSUE
        severity = "high"
        reasons.append("status=critical")
        signals.append({"metric": "status", "reason": "status=critical"})
    elif status == "review":
        level = _max_level(level, BriefingAction.GITHUB_ISSUE)
        reasons.append("status=review")
        signals.append({"metric": "status", "reason": "status=review"})

    if drift_score is not None:
        if drift_score >= _DRIFT_CRITICAL:
            level = _max_level(level, BriefingAction.GITHUB_ISSUE)
            severity = "high"
            reasons.append(f"drift_score={drift_score:.2f}")
            signals.append(
                {
                    "metric": "drift_score",
                    "reason": f"drift_score={drift_score:.2f} exceeds {_DRIFT_CRITICAL:.2f}",
                }
            )
        elif drift_score >= _DRIFT_WARN:
            level = _max_level(level, BriefingAction.GITHUB_ISSUE)
            reasons.append(f"drift_score={drift_score:.2f}")
            signals.append(
                {
                    "metric": "drift_score",
                    "reason": f"drift_score={drift_score:.2f} exceeds {_DRIFT_WARN:.2f}",
                }
            )

    if overall_score is not None:
        if overall_score <= _SCORE_CRITICAL:
            level = _max_level(level, BriefingAction.GITHUB_ISSUE)
            severity = "high"
            reasons.append(f"overall_score={overall_score:.1f}")
            signals.append(
                {
                    "metric": "overall_score",
                    "reason": f"overall_score={overall_score:.1f} is below {_SCORE_CRITICAL:.1f}",
                }
            )
        elif overall_score <= _SCORE_REVIEW:
            level = _max_level(level, BriefingAction.GITHUB_ISSUE)
            reasons.append(f"overall_score={overall_score:.1f}")
            signals.append(
                {
                    "metric": "overall_score",
                    "reason": f"overall_score={overall_score:.1f} is below {_SCORE_REVIEW:.1f}",
                }
            )

    flags = strategy.get("risk_flags") or strategy.get("alerts") or ()
    if isinstance(flags, Mapping):
        flags = tuple(str(key) for key, enabled in flags.items() if enabled)
    for flag in flags:
        text = str(flag).lower()
        if any(keyword in text for keyword in _CIRCUIT_KEYWORDS):
            level = BriefingAction.TELEGRAM
            kind = "runtime_risk"
            severity = "high"
            reasons.append(f"flag={flag}")
            signals.append({"metric": "risk_flag", "reason": f"flag={flag}"})

    if level == BriefingAction.QUIET:
        return None
    metric_keys = (
        "overall_score",
        "performance_score",
        "risk_score",
        "decay_score",
        "stability_score",
        "operational_score",
        "drift_score",
        "status",
        "as_of",
    )
    return BriefingFinding(
        source=source,
        level=level,
        reason="; ".join(reasons) or "anomaly",
        strategy_profile=profile,
        domain=str(strategy.get("domain") or domain),
        kind=kind,
        severity=severity,
        metrics={key: strategy[key] for key in metric_keys if key in strategy},
        signals=signals,
    )


def _classify_report_payload(
    payload: Mapping[str, Any],
    *,
    source: str,
) -> list[BriefingFinding]:
    findings: list[BriefingFinding] = []

    if payload.get("ok") is False:
        error = str(payload.get("error") or "ok=false")
        data_unavailable = str(payload.get("data_status") or "").strip().lower() == "unavailable"
        level = (
            BriefingAction.TELEGRAM
            if data_unavailable or "circuit" in error.lower()
            else BriefingAction.GITHUB_ISSUE
        )
        findings.append(
            BriefingFinding(
                source=source,
                level=level,
                reason=error,
                domain=str(payload.get("domain") or ""),
                kind="data_unavailable" if data_unavailable else "data_quality",
                severity="high" if level == BriefingAction.TELEGRAM else "medium",
                signals=[{"metric": "data_status", "reason": error}],
            )
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
    if isinstance(summary, Mapping) and not strategies:
        critical = int(summary.get("critical") or 0)
        review = int(summary.get("review") or 0)
        if critical > 0:
            findings.append(
                BriefingFinding(
                    source=source,
                    level=BriefingAction.GITHUB_ISSUE,
                    reason=f"summary critical={critical}",
                    domain=domain,
                    kind="strategy_monitoring_summary",
                    severity="high",
                )
            )
        elif review > 0:
            findings.append(
                BriefingFinding(
                    source=source,
                    level=BriefingAction.GITHUB_ISSUE,
                    reason=f"summary review={review}",
                    domain=domain,
                    kind="strategy_monitoring_summary",
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
    summary_reports: list[dict[str, Any]] = []

    for file_path in sorted(path.glob("*.json")):
        if file_path.name.startswith("_"):
            continue
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            summary_reports.append({})
            findings.append(
                BriefingFinding(
                    source=file_path.name,
                    level=BriefingAction.GITHUB_ISSUE,
                    reason=f"invalid_json: {exc}",
                    kind="data_quality",
                )
            )
            continue
        if not isinstance(payload, Mapping):
            summary_reports.append({})
            continue
        summary_reports.append(_summary_report(payload, source=file_path.name))
        findings.extend(_classify_report_payload(payload, source=file_path.name))

    return BriefingConsumptionResult(
        day=resolved_day, report_dir=str(path), findings=findings, summary_reports=summary_reports,
    )


_SUMMARY_DOMAINS = ("cn_equity", "hk_equity", "us_equity", "crypto")
_SUMMARY_STATUSES = ("healthy", "watch", "review", "critical", "unavailable", "unknown")


def _summary_report(payload: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Whitelist counts and source clocks, excluding profiles, errors and free text."""
    domain = payload.get("domain")
    rows = payload.get("strategies")
    if domain not in _SUMMARY_DOMAINS or source != f"{domain}.json" or not isinstance(rows, list):
        return {}
    try:
        generated = datetime.fromisoformat(payload["as_of"])
        if generated.tzinfo is None:
            return {}
        generated = generated.astimezone(timezone.utc)
        counts = dict.fromkeys(_SUMMARY_STATUSES, 0)
        observations: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                return {}
            observed = date.fromisoformat(row["as_of"]).isoformat()
            observations[observed] = observations.get(observed, 0) + 1
            status = row.get("status")
            counts[status if status in _SUMMARY_STATUSES else "unknown"] += 1
    except (KeyError, TypeError, ValueError, OverflowError):
        return {}
    return {
        "source": source, "domain": domain,
        "report_generated_at": generated.isoformat(),
        "data_ready": payload.get("ok") is True and payload.get("data_status") == "ready",
        "strategy_counts": counts, "observation_dates": observations,
    }


def _summary_now() -> datetime:
    return datetime.now(timezone.utc)


def summarize_briefing(result: BriefingConsumptionResult, *, dry_run: bool = False) -> dict[str, Any]:
    """Optional advisory text, never a notification or strategy action authority."""
    def unavailable(reason: str) -> dict[str, Any]:
        return {"status": "unavailable", "reason": reason, "advisory_only": True}

    if dry_run:
        return {"status": "dry_run", "advisory_only": True}
    # A timer's static dashboard token cannot authorize /execute/jobs. Presence
    # only enables the SDK's OIDC flow; the service still validates its claims.
    if not all(os.environ.get(key, "").strip() for key in (
        "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    )):
        return unavailable("github_oidc_required")
    now = _summary_now()
    reports = result.summary_reports
    if not reports or any(not report or not report["data_ready"] for report in reports):
        return unavailable("briefing_input_unavailable")
    # Report generation is a separate clock from the strategy observations.
    # 36 hours and 7 natural days are conservative summary limits, not a
    # trading-calendar validation or a change to existing risk classifications.
    for report in reports:
        age = (now - datetime.fromisoformat(report["report_generated_at"])).total_seconds()
        if not 0 <= age <= 36 * 3600 or any(
            not 0 <= (now.date() - date.fromisoformat(observed)).days <= 7
            for observed in report["observation_dates"]
        ):
            return unavailable("briefing_source_time_unavailable")
    if not any(report["observation_dates"] for report in reports):
        return unavailable("briefing_input_unavailable")
    context = {
        "scope": "reported_domains_only",
        "missing_domains": [domain for domain in _SUMMARY_DOMAINS if domain not in {r["domain"] for r in reports}],
        "reports": reports,
        "rule_action": result.action.value,
    }
    prompt = (
        "用简短中文总结以下日报计数，仅供人工参考。输入只有已加载领域，缺失领域不能视为正常。"
        "strategy_counts 是报告中的状态数量，不是实时健康证明；report_generated_at 是生成时间，"
        "observation_dates 才是策略观测日期，必须分别说明。缺少回测、收益、交易和晋级证据，"
        "不得声称已验证、建议自动执行、解除告警或授予交易权限。不要添加输入之外的事实。\n"
        + json.dumps(context, ensure_ascii=False, sort_keys=True)
    )
    from client.config import GatewayConfig
    from client.gateway_client import AiGatewayClient

    try:
        config = GatewayConfig.from_env()
    except ValueError:
        return unavailable("ai_gateway_not_configured")
    source_repository = config.source_repository or os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not source_repository:
        return unavailable("source_repository_required")
    try:
        response = AiGatewayClient(config).execute(
            prompt, task="daily_briefing", mode="review_only", research_stage="research_summary",
            allowed_providers=list(config.research_providers), timeout=300, source_repository=source_repository,
        )
    except Exception:
        return unavailable("summary_execution_unavailable")
    raw = response.raw
    if (response.success is False and isinstance(raw, dict) and raw.get("status") == "deferred"
        and (response.provider in config.research_providers or not response.provider)):
        retry = raw.get("retry_at")
        if type(retry) not in (int, float) or not math.isfinite(retry) or retry <= 0:
            retry = None
        return {"status": "deferred", "advisory_only": True, "retry_at": retry}
    if not (
        response.success is True and response.provider in config.research_providers
        and isinstance(response.output, str) and response.output.strip() and not response.error and not response.note
        and isinstance(raw, dict) and raw.get("status") == "succeeded"
        and raw.get("provider", "codex") == response.provider
        and raw.get("research_stage") == "research_summary"
        and isinstance(response.model, str) and response.model.strip() and raw.get("model") == response.model
        and raw.get("reasoning_effort") in {"low", "medium", "high", "xhigh"}
        and raw.get("output") == response.output
    ):
        return unavailable("summary_result_unavailable")
    return {
        "status": "available", "advisory_only": True, "text": response.output,
        "provider": response.provider, "model": response.model, "reasoning_effort": raw["reasoning_effort"],
        "input": context,
    }


def merge_findings(groups: Iterable[Iterable[BriefingFinding]]) -> list[BriefingFinding]:
    """Flatten multiple finding iterables."""
    merged: list[BriefingFinding] = []
    for group in groups:
        merged.extend(group)
    return merged
