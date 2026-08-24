"""Strategy optimization watcher for issue-only automation proposals."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Final

from service.automation_contracts import AutomationTask, EvidenceBundle, GateDecision, ProposedAction, TriggerRecord
from service.research_task import ResearchTaskError, build_strategy_diagnosis_task
from service.strategy_automation_registry import LANE_RESEARCH_BACKLOG, summarize_strategy_registry_context
from service.strategy_optimization_policy import evaluate_strategy_metrics

WATCHER_SCHEMA_VERSION = "strategy_optimization_watch.v1"
ISSUE_ONLY_ACTION = "open_issue"
PERFORMANCE_SCHEMA_VERSION = "strategy_performance.v2"
OPERATIONAL_SCHEMA_VERSION = "strategy_operational_metrics.v1"
METRICS_KIND_PERFORMANCE = "performance"
METRICS_KIND_OPERATIONAL = "operational_quality"
REQUIRED_PERFORMANCE_METRICS = ("sharpe", "cagr", "calmar", "win_rate", "max_dd")
MONITORING_SCHEMA_VERSION = "strategy_monitoring_evidence.v1"
METRICS_KIND_MONITORING = "monitoring_evidence"
MONITORING_FINDING_TYPE = "monitoring_trigger"
RESEARCH_INPUT_UNAVAILABLE_FINDING_TYPE = "research_input_unavailable"
RESEARCH_TASK_SOURCE_SCHEMA_VERSION = "qsl_research_task_source_snapshot.v1"
RESEARCH_TASK_SOURCE_ID = "aiaudit.strategy_optimization_watcher"
@dataclass(frozen=True)
class StrategyWatchRegistration:
    """Trusted source registration used by monitoring findings.

    Keep this registry data-only: adding a domain must not add a new
    execution path.  Unknown domains intentionally resolve to no repository
    so the watcher remains fail-closed.
    """

    domain: str
    repository: str


STRATEGY_WATCH_REGISTRY: Final[tuple[StrategyWatchRegistration, ...]] = (
    StrategyWatchRegistration("cn_equity", "QuantStrategyLab/CnEquityStrategies"),
    StrategyWatchRegistration("hk_equity", "QuantStrategyLab/HkEquityStrategies"),
    StrategyWatchRegistration("us_equity", "QuantStrategyLab/UsEquityStrategies"),
    StrategyWatchRegistration("crypto", "QuantStrategyLab/CryptoStrategies"),
)
# Compatibility view for callers that used the old mapping.  The tuple above
# remains the single source of truth.
STRATEGY_REPOSITORY_BY_DOMAIN: Final[dict[str, str]] = {
    item.domain: item.repository for item in STRATEGY_WATCH_REGISTRY
}


def resolve_strategy_watch_repository(domain: str) -> str:
    """Resolve a registered domain, returning ``""`` for unknown domains."""
    normalized = str(domain or "").strip()
    return next(
        (item.repository for item in STRATEGY_WATCH_REGISTRY if item.domain == normalized),
        "",
    )


def _dict_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class StrategyWatchSnapshot:
    repo: str
    profile: str
    plugin: str = ""
    candidate_kind: str = "individual"
    domain: str = ""
    schema_version: str = ""
    metrics_kind: str = ""
    current_metrics: dict[str, Any] = field(default_factory=dict)
    baseline_metrics: dict[str, Any] = field(default_factory=dict)
    research_task_evidence: dict[str, Any] = field(default_factory=dict)
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
            candidate_kind=str(payload.get("candidate_kind") or "individual").strip(),
            domain=str(payload.get("domain") or "").strip(),
            schema_version=str(payload.get("schema_version") or default_schema_version).strip(),
            metrics_kind=str(payload.get("metrics_kind") or payload.get("metric_set") or default_metrics_kind).strip(),
            current_metrics=_dict_payload(payload.get("current_metrics") or payload.get("current")),
            baseline_metrics=_dict_payload(payload.get("baseline_metrics") or payload.get("baseline")),
            research_task_evidence=_dict_payload(payload.get("research_task_evidence")),
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
            "candidate_kind": self.candidate_kind,
            "domain": self.domain,
            "schema_version": self.schema_version,
            "metrics_kind": self.metrics_kind,
            "current_metrics": self.current_metrics,
            "baseline_metrics": self.baseline_metrics,
            "research_task_evidence": self.research_task_evidence,
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


def build_strategy_monitoring_finding(
    *,
    domain: str,
    profile: str,
    severity: str,
    metrics: dict[str, Any],
    signals: list[dict[str, Any]],
    source: str,
    generated_at: str = "",
    repo: str = "",
) -> StrategyWatchFinding:
    """Build a pre-classified, issue-only finding from trusted monitor evidence."""
    normalized_domain = str(domain or "").strip()
    normalized_profile = str(profile or "").strip()
    resolved_repo = str(repo or resolve_strategy_watch_repository(normalized_domain) or "").strip()
    if not normalized_domain or not normalized_profile:
        raise ValueError("strategy monitoring finding requires domain and profile")
    if not resolved_repo:
        raise ValueError(f"no strategy repository is configured for domain={normalized_domain!r}")
    return StrategyWatchFinding(
        snapshot=StrategyWatchSnapshot(
            repo=resolved_repo,
            profile=normalized_profile,
            schema_version=MONITORING_SCHEMA_VERSION,
            metrics_kind=METRICS_KIND_MONITORING,
            current_metrics=dict(metrics),
            source=str(source or "").strip(),
            generated_at=str(generated_at or "").strip(),
        ),
        severity="high" if str(severity).strip().lower() == "high" else "medium",
        signals=[dict(signal) for signal in signals],
        finding_type=MONITORING_FINDING_TYPE,
    )


def build_research_input_unavailable_finding(
    *,
    repo: str,
    profile: str,
    status: str,
    reason_code: str,
    candidate_id: str = "",
    date_cutoff: str = "",
    source: str = "",
) -> StrategyWatchFinding:
    """Build a high-severity issue-only finding for a deferred research input.

    A deferred P1 source has not produced an observation.  It must be visible
    to operators, but it must never be treated as performance degradation or
    allowed to produce a strategy-change task.
    """
    normalized_repo = str(repo or "").strip()
    normalized_profile = str(profile or candidate_id or "").strip()
    normalized_status = str(status or "").strip().upper()
    normalized_reason = str(reason_code or "").strip()
    if not normalized_repo or not normalized_profile:
        raise ValueError("research input finding requires repository and profile")
    if normalized_status != "DEFERRED" or not normalized_reason:
        raise ValueError("research input finding requires a deferred status and reason code")
    metrics = {
        "p1_status": normalized_status,
        "reason_code": normalized_reason,
        "candidate_id": str(candidate_id or "").strip(),
        "date_cutoff": str(date_cutoff or "").strip(),
    }
    return StrategyWatchFinding(
        snapshot=StrategyWatchSnapshot(
            repo=normalized_repo,
            profile=normalized_profile,
            schema_version="research_input_terminal.v1",
            metrics_kind="research_input_terminal",
            current_metrics=metrics,
            source=str(source or "").strip(),
        ),
        severity="high",
        signals=[
            {
                "metric": "p1_status",
                "reason": (
                    f"P1 research input is deferred: {normalized_reason}; "
                    "no comparable performance observation was published"
                ),
            }
        ],
        finding_type=RESEARCH_INPUT_UNAVAILABLE_FINDING_TYPE,
    )


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
    if finding_type == "data_quality":
        trigger_kind = "strategy_metrics_contract_invalid"
        evidence_summary = "Strategy metrics payload failed watcher contract validation."
        rationale = (
            "Open a data-quality issue so the source repo publishes "
            "strategy_performance.v2 before optimization automation runs again."
        )
    elif finding_type == RESEARCH_INPUT_UNAVAILABLE_FINDING_TYPE:
        trigger_kind = "strategy_research_input_unavailable"
        evidence_summary = "A trusted P1 terminal record deferred the research input."
        rationale = (
            "Restore the trusted research-data input before optimization automation "
            "or any strategy-change proposal resumes."
        )
    elif finding_type == MONITORING_FINDING_TYPE:
        trigger_kind = "strategy_monitoring_trigger"
        evidence_summary = "Strategy monitoring evidence crossed a research-review threshold."
        rationale = "Open a research optimization issue for AI diagnosis and bounded, no-order experiment planning."
    else:
        trigger_kind = "strategy_metric_degradation"
        evidence_summary = "Deterministic strategy metrics crossed degradation thresholds."
        rationale = "Open a research optimization issue for AI diagnosis and sandbox experiment planning."
    trigger = TriggerRecord(
        source="strategy_optimization_watcher",
        kind=trigger_kind,
        severity=finding.severity,
        reason="; ".join(signal_reasons) or (
            "strategy metrics contract invalid"
            if finding_type == "data_quality"
            else "research input unavailable"
            if finding_type == RESEARCH_INPUT_UNAVAILABLE_FINDING_TYPE
            else "strategy metrics degraded"
        ),
        subject=finding.snapshot.subject(),
        metrics=finding.snapshot.current_metrics,
        evidence=signal_reasons,
    )
    evidence = EvidenceBundle(
        summary=evidence_summary,
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
        rationale=rationale,
        requires_human_review=False,
        metadata={"profile": finding.snapshot.profile, "plugin": finding.snapshot.plugin, "event_key": event_key, "finding_type": finding_type},
    )
    gate = GateDecision(
        allowed=True,
        reason="Issue-only proposal is allowed; it may only lead to a separately validated, inactive research task.",
        required_checks=[
            "qsl.research_task.v1 validation before any experiment",
            "offline no-order sandbox backtest evidence before any candidate PR",
            "inactive candidate registry/authority gate before P4-P6 impact",
        ],
        human_review_required=False,
        metadata={"issue_only": True, "live_impact_allowed": False},
    )
    return AutomationTask(
        trigger=trigger,
        evidence=evidence,
        proposed_action=proposed,
        gate_decision=gate,
        metadata={"event_key": event_key, "finding_type": finding_type},
    )


def finding_to_research_task(finding: StrategyWatchFinding) -> dict[str, Any] | None:
    """Build a task only when a verified P3 comparison binds every input.

    Existing legacy/operational watcher lanes remain issue-only.  They must not
    be promoted into a research task merely because they emitted a finding.
    """
    snapshot = finding.snapshot
    if finding.finding_type != "metric_degradation" or snapshot.metrics_kind != METRICS_KIND_PERFORMANCE:
        return None
    strategy_repository = STRATEGY_REPOSITORY_BY_DOMAIN.get(snapshot.domain)
    if not strategy_repository:
        return None
    try:
        return build_strategy_diagnosis_task(
            event_key=finding_event_key(finding),
            created_at=snapshot.generated_at,
            candidate_id=snapshot.profile,
            candidate_kind=snapshot.candidate_kind,
            domain=snapshot.domain,
            strategy_repository=strategy_repository,
            evidence=snapshot.research_task_evidence,
        )
    except ResearchTaskError:
        return None


def research_task_context_available(payload: dict[str, Any]) -> bool:
    """Whether the watcher payload carries the bounded P3 bindings a task needs."""
    snapshot = StrategyWatchSnapshot.from_dict(payload)
    evidence = snapshot.research_task_evidence
    return (
        snapshot.metrics_kind == METRICS_KIND_PERFORMANCE
        and snapshot.candidate_kind in {"individual", "portfolio", "plugin"}
        and snapshot.domain in STRATEGY_REPOSITORY_BY_DOMAIN
        and bool(snapshot.profile)
        and set(evidence) == {"p1_input_digest", "p2_config_digest", "p3_evidence_id", "strategy_revision", "producer_revision"}
    )


def research_task_source_snapshot(
    findings: list[StrategyWatchFinding],
    *,
    context_available: bool,
    computed_at: str,
) -> dict[str, Any]:
    """Project only verified tasks into the separate, source-owned queue index."""
    tasks: list[dict[str, Any]] = []
    errors: list[str] = []
    if not context_available:
        errors.append("research_task_context_unavailable")
    else:
        for finding in findings:
            task = finding_to_research_task(finding)
            if task is None:
                errors.append("research_task_contract_unavailable")
            else:
                tasks.append(task)
    generated_values = [finding.snapshot.generated_at for finding in findings if finding.snapshot.generated_at]
    generated_at = max(generated_values) if generated_values else computed_at
    return {
        "schema_version": RESEARCH_TASK_SOURCE_SCHEMA_VERSION,
        "source_id": RESEARCH_TASK_SOURCE_ID,
        "generated_at": generated_at,
        "computed_at": computed_at,
        "data_status": "unavailable" if errors else "ready",
        "tasks": [] if errors else tasks,
        "errors": sorted(set(errors)),
    }


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
            "## Required gates before candidate or live impact",
            checks,
            "",
            "This watcher only opens an issue. It does not modify strategy code, tune active parameters, submit orders, merge PRs, or deploy.",
        ]
    )
    return {"title": title[:240], "body": body}
