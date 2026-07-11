"""Strategy optimization watcher for issue-only automation proposals."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any

from service.automation_contracts import AutomationTask, EvidenceBundle, GateDecision, ProposedAction, TriggerRecord
from service.strategy_automation_registry import LANE_RESEARCH_BACKLOG, summarize_strategy_registry_context
from service.strategy_optimization_policy import evaluate_strategy_metrics

WATCHER_SCHEMA_VERSION = "strategy_optimization_watch.v1"
ISSUE_ONLY_ACTION = "open_issue"
PERFORMANCE_SCHEMA_VERSION = "strategy_performance.v2"
OPERATIONAL_SCHEMA_VERSION = "strategy_operational_metrics.v1"
METRICS_KIND_PERFORMANCE = "performance"
METRICS_KIND_OPERATIONAL = "operational_quality"
REQUIRED_PERFORMANCE_METRICS = ("sharpe", "cagr", "calmar", "win_rate", "max_dd")


def _dict_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class StrategyWatchSnapshot:
    repo: str
    profile: str
    plugin: str = ""
    schema_version: str = ""
    metrics_kind: str = ""
    current_metrics: dict[str, Any] = field(default_factory=dict)
    baseline_metrics: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    generated_at: str = ""

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        default_repo: str = "",
        default_schema_version: str = "",
        default_metrics_kind: str = "",
    ) -> "StrategyWatchSnapshot":
        profile = str(payload.get("strategy_profile") or payload.get("profile") or "").strip()
        return cls(
            repo=str(payload.get("repo") or payload.get("repository") or default_repo).strip(),
            profile=profile,
            plugin=str(payload.get("plugin") or payload.get("strategy_plugin") or "").strip(),
            schema_version=str(payload.get("schema_version") or default_schema_version).strip(),
            metrics_kind=str(payload.get("metrics_kind") or payload.get("metric_set") or default_metrics_kind).strip(),
            current_metrics=_dict_payload(payload.get("current_metrics") or payload.get("current")),
            baseline_metrics=_dict_payload(payload.get("baseline_metrics") or payload.get("baseline")),
            source=str(payload.get("source") or "").strip(),
            generated_at=str(payload.get("generated_at") or "").strip(),
        )

    def subject(self) -> str:
        parts = [self.repo, self.profile or self.plugin]
        return ":".join(part for part in parts if part)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "profile": self.profile,
            "plugin": self.plugin,
            "schema_version": self.schema_version,
            "metrics_kind": self.metrics_kind,
            "current_metrics": self.current_metrics,
            "baseline_metrics": self.baseline_metrics,
            "source": self.source,
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True)
class StrategyWatchFinding:
    snapshot: StrategyWatchSnapshot
    severity: str
    signals: list[dict[str, Any]]
    finding_type: str = "metric_degradation"
    registry_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WATCHER_SCHEMA_VERSION,
            "snapshot": self.snapshot.to_dict(),
            "severity": self.severity,
            "signals": self.signals,
            "finding_type": self.finding_type,
            "registry_context": self.registry_context,
        }


def _snapshots_from_payload(payload: dict[str, Any]) -> list[StrategyWatchSnapshot]:
    default_repo = str(payload.get("repo") or payload.get("repository") or "").strip()
    default_schema_version = str(payload.get("schema_version") or "").strip()
    default_metrics_kind = str(payload.get("metrics_kind") or payload.get("metric_set") or "").strip()
    raw_snapshots = payload.get("snapshots")
    if not isinstance(raw_snapshots, list):
        raw_snapshots = [payload]
    snapshots: list[StrategyWatchSnapshot] = []
    for item in raw_snapshots:
        if isinstance(item, dict):
            snapshots.append(
                StrategyWatchSnapshot.from_dict(
                    item,
                    default_repo=default_repo,
                    default_schema_version=default_schema_version,
                    default_metrics_kind=default_metrics_kind,
                )
            )
        else:
            snapshots.append(
                StrategyWatchSnapshot(
                    repo=default_repo,
                    profile="",
                    schema_version=default_schema_version,
                    metrics_kind=default_metrics_kind,
                )
            )
    return snapshots


def _data_quality_signal(reason: str, *, metric: str = "data_quality") -> dict[str, Any]:
    return {"metric": metric, "reason": reason}


def _metric_value_issues(metrics: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for metric in REQUIRED_PERFORMANCE_METRICS:
        if metric not in metrics:
            continue
        value = metrics[metric]
        if isinstance(value, bool):
            valid = False
        else:
            try:
                valid = math.isfinite(float(value))
            except (TypeError, ValueError):
                valid = False
        if not valid:
            issues.append(_data_quality_signal(f"{label}.{metric} must be a finite numeric value", metric=metric))
    return issues


def _validate_snapshot_contract(snapshot: StrategyWatchSnapshot) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    schema_version = snapshot.schema_version
    metrics_kind = snapshot.metrics_kind

    if not schema_version and not metrics_kind:
        legacy_metrics = set(snapshot.current_metrics).intersection(snapshot.baseline_metrics, REQUIRED_PERFORMANCE_METRICS)
        if legacy_metrics:
            return _metric_value_issues(snapshot.current_metrics, label="current_metrics") + _metric_value_issues(
                snapshot.baseline_metrics, label="baseline_metrics"
            )
        return [_data_quality_signal("missing versioned performance metrics; no comparable legacy metrics found")]

    if schema_version == PERFORMANCE_SCHEMA_VERSION and not metrics_kind:
        metrics_kind = METRICS_KIND_PERFORMANCE
    elif metrics_kind == METRICS_KIND_PERFORMANCE and not schema_version:
        schema_version = PERFORMANCE_SCHEMA_VERSION

    if not schema_version:
        issues.append(_data_quality_signal("missing schema_version; expected strategy_performance.v2 payload"))
    if not metrics_kind:
        issues.append(_data_quality_signal("missing metrics_kind; expected performance payload"))

    if schema_version == OPERATIONAL_SCHEMA_VERSION or metrics_kind == METRICS_KIND_OPERATIONAL:
        issues.append(
            _data_quality_signal(
                "operational metrics payload is incompatible with optimization watcher; publish strategy_performance.v2 instead"
            )
        )
        return issues

    if schema_version and schema_version != PERFORMANCE_SCHEMA_VERSION:
        issues.append(
            _data_quality_signal(
                f"unsupported schema_version={schema_version!r}; expected {PERFORMANCE_SCHEMA_VERSION}"
            )
        )
    if metrics_kind and metrics_kind != METRICS_KIND_PERFORMANCE:
        issues.append(
            _data_quality_signal(
                f"unsupported metrics_kind={metrics_kind!r}; expected {METRICS_KIND_PERFORMANCE!r}"
            )
        )
    if issues:
        return issues

    missing_current = [metric for metric in REQUIRED_PERFORMANCE_METRICS if metric not in snapshot.current_metrics]
    missing_baseline = [metric for metric in REQUIRED_PERFORMANCE_METRICS if metric not in snapshot.baseline_metrics]
    if missing_current:
        issues.append(
            _data_quality_signal(
                f"current_metrics missing required performance metrics: {', '.join(missing_current)}"
            )
        )
    if missing_baseline:
        issues.append(
            _data_quality_signal(
                f"baseline_metrics missing required performance metrics: {', '.join(missing_baseline)}"
            )
        )
    issues.extend(_metric_value_issues(snapshot.current_metrics, label="current_metrics"))
    issues.extend(_metric_value_issues(snapshot.baseline_metrics, label="baseline_metrics"))
    return issues


def evaluate_strategy_watch(payload: dict[str, Any]) -> list[StrategyWatchFinding]:
    """Evaluate metrics payload and return issue-worthy findings only."""
    registry_payload = payload.get("automation_registry") or payload.get("registry") or {}
    findings: list[StrategyWatchFinding] = []
    for snapshot in _snapshots_from_payload(payload):
        validation_issues = _validate_snapshot_contract(snapshot)
        context = summarize_strategy_registry_context(registry_payload, snapshot.profile) if snapshot.profile else {}
        if validation_issues:
            findings.append(
                StrategyWatchFinding(
                    snapshot=snapshot,
                    severity="medium",
                    signals=validation_issues,
                    finding_type="data_quality",
                    registry_context=context,
                )
            )
            continue
        decision = evaluate_strategy_metrics(snapshot.current_metrics, snapshot.baseline_metrics)
        if not decision["should_open_issue"]:
            continue
        findings.append(
            StrategyWatchFinding(
                snapshot=snapshot,
                severity=str(decision["severity"]),
                signals=list(decision["signals"]),
                finding_type="metric_degradation",
                registry_context=context,
            )
        )
    return findings


def finding_event_key(finding: StrategyWatchFinding) -> str:
    payload = {
        "snapshot": finding.snapshot.to_dict(),
        "severity": finding.severity,
        "signals": finding.signals,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def finding_to_automation_task(finding: StrategyWatchFinding) -> AutomationTask:
    """Convert a deterministic finding into an issue-only automation task."""
    lane = str(finding.registry_context.get("automation_lane") or LANE_RESEARCH_BACKLOG)
    event_key = finding_event_key(finding)
    signal_reasons = [str(signal.get("reason") or signal.get("metric") or "metric degraded") for signal in finding.signals]
    finding_type = str(finding.finding_type or "metric_degradation")
    trigger = TriggerRecord(
        source="strategy_optimization_watcher",
        kind="strategy_metrics_contract_invalid" if finding_type == "data_quality" else "strategy_metric_degradation",
        severity=finding.severity,
        reason="; ".join(signal_reasons) or ("strategy metrics contract invalid" if finding_type == "data_quality" else "strategy metrics degraded"),
        subject=finding.snapshot.subject(),
        metrics=finding.snapshot.current_metrics,
        evidence=signal_reasons,
    )
    evidence = EvidenceBundle(
        summary=(
            "Strategy metrics payload failed watcher contract validation."
            if finding_type == "data_quality"
            else "Deterministic strategy metrics crossed degradation thresholds."
        ),
        artifacts=[finding.snapshot.source] if finding.snapshot.source else [],
        metrics={
            "current": finding.snapshot.current_metrics,
            "baseline": finding.snapshot.baseline_metrics,
        },
        risks=[
            "issue-only: no strategy code, live parameters, broker/order paths, or deployment are changed",
            "sandbox backtest evidence is required before any PR can be proposed",
        ],
    )
    proposed = ProposedAction(
        action=ISSUE_ONLY_ACTION,
        lane=lane,
        target=finding.snapshot.repo,
        rationale=(
            "Open a data-quality issue so the source repo publishes strategy_performance.v2 before optimization automation runs again."
            if finding_type == "data_quality"
            else "Open a research optimization issue for AI diagnosis and sandbox experiment planning."
        ),
        requires_human_review=True,
        metadata={"profile": finding.snapshot.profile, "plugin": finding.snapshot.plugin, "event_key": event_key, "finding_type": finding_type},
    )
    gate = GateDecision(
        allowed=True,
        reason="Issue-only proposal is allowed; code changes and live-impact actions remain gated.",
        required_checks=[
            "human review before strategy code changes",
            "sandbox backtest before optimization PR",
            "strategy registry/authority gate before live impact",
        ],
        human_review_required=True,
        metadata={"issue_only": True, "live_impact_allowed": False},
    )
    return AutomationTask(
        trigger=trigger,
        evidence=evidence,
        proposed_action=proposed,
        gate_decision=gate,
        metadata={"event_key": event_key, "finding_type": finding_type},
    )


def watcher_issue_key(task: AutomationTask) -> str:
    payload = task.to_dict()
    trigger = payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    subject = str(trigger.get("subject") or "")
    key_payload: dict[str, Any] = {"subject": subject}
    finding_type = str(metadata.get("finding_type") or "metric_degradation")
    if finding_type != "metric_degradation":
        key_payload["finding_type"] = finding_type
        key_payload["trigger_kind"] = str(trigger.get("kind") or "")
    raw = json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def issue_for_task(task: AutomationTask) -> dict[str, str]:
    """Build a GitHub issue title/body for a strategy optimization task."""
    payload = task.to_dict()
    trigger = payload["trigger"]
    evidence = payload["evidence"]
    action = payload["proposed_action"]
    event_key = str(payload.get("metadata", {}).get("event_key") or "")
    issue_key = watcher_issue_key(task)
    title = f"AI strategy optimization proposal: {trigger.get('subject') or action.get('target') or 'strategy profile'}"
    signals = "\n".join(f"- {item}" for item in trigger.get("evidence", [])) or "- Strategy metrics degraded."
    checks = "\n".join(f"- [ ] {item}" for item in payload["gate_decision"].get("required_checks", []))
    risks = "\n".join(f"- {item}" for item in evidence.get("risks", []))
    body = "\n".join(
        [
            f"<!-- strategy-optimization-watcher:{issue_key} -->",
            "## Summary",
            str(evidence.get("summary") or "Strategy optimization watcher opened this issue."),
            "",
            "## Trigger",
            f"- Severity: `{trigger.get('severity')}`",
            f"- Subject: `{trigger.get('subject')}`",
            f"- Event key: `{event_key}`",
            "",
            "## Signals",
            signals,
            "",
            "## Safety boundary",
            risks,
            "",
            "## Required gates before code/live impact",
            checks,
            "",
            "This watcher only opens an issue. It does not modify strategy code, tune live parameters, merge PRs, or deploy.",
        ]
    )
    return {"title": title[:240], "body": body}
