"""Strategy optimization watcher for issue-only automation proposals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from service.automation_contracts import AutomationTask, EvidenceBundle, GateDecision, ProposedAction, TriggerRecord
from service.strategy_automation_registry import LANE_RESEARCH_BACKLOG, summarize_strategy_registry_context
from service.strategy_optimization_policy import evaluate_strategy_metrics

WATCHER_SCHEMA_VERSION = "strategy_optimization_watch.v1"
ISSUE_ONLY_ACTION = "open_issue"


def _dict_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class StrategyWatchSnapshot:
    repo: str
    profile: str
    plugin: str = ""
    current_metrics: dict[str, Any] = field(default_factory=dict)
    baseline_metrics: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    generated_at: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, default_repo: str = "") -> "StrategyWatchSnapshot":
        profile = str(payload.get("strategy_profile") or payload.get("profile") or "").strip()
        return cls(
            repo=str(payload.get("repo") or payload.get("repository") or default_repo).strip(),
            profile=profile,
            plugin=str(payload.get("plugin") or payload.get("strategy_plugin") or "").strip(),
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
    registry_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WATCHER_SCHEMA_VERSION,
            "snapshot": self.snapshot.to_dict(),
            "severity": self.severity,
            "signals": self.signals,
            "registry_context": self.registry_context,
        }


def _snapshots_from_payload(payload: dict[str, Any]) -> list[StrategyWatchSnapshot]:
    default_repo = str(payload.get("repo") or payload.get("repository") or "").strip()
    raw_snapshots = payload.get("snapshots")
    if not isinstance(raw_snapshots, list):
        raw_snapshots = [payload]
    snapshots: list[StrategyWatchSnapshot] = []
    for item in raw_snapshots:
        if isinstance(item, dict):
            snapshots.append(StrategyWatchSnapshot.from_dict(item, default_repo=default_repo))
    return snapshots


def evaluate_strategy_watch(payload: dict[str, Any]) -> list[StrategyWatchFinding]:
    """Evaluate metrics payload and return issue-worthy findings only."""
    registry_payload = payload.get("automation_registry") or payload.get("registry") or {}
    findings: list[StrategyWatchFinding] = []
    for snapshot in _snapshots_from_payload(payload):
        decision = evaluate_strategy_metrics(snapshot.current_metrics, snapshot.baseline_metrics)
        if not decision["should_open_issue"]:
            continue
        context = summarize_strategy_registry_context(registry_payload, snapshot.profile) if snapshot.profile else {}
        findings.append(
            StrategyWatchFinding(
                snapshot=snapshot,
                severity=str(decision["severity"]),
                signals=list(decision["signals"]),
                registry_context=context,
            )
        )
    return findings


def finding_to_automation_task(finding: StrategyWatchFinding) -> AutomationTask:
    """Convert a deterministic finding into an issue-only automation task."""
    lane = str(finding.registry_context.get("automation_lane") or LANE_RESEARCH_BACKLOG)
    signal_reasons = [str(signal.get("reason") or signal.get("metric") or "metric degraded") for signal in finding.signals]
    trigger = TriggerRecord(
        source="strategy_optimization_watcher",
        kind="strategy_metric_degradation",
        severity=finding.severity,
        reason="; ".join(signal_reasons) or "strategy metrics degraded",
        subject=finding.snapshot.subject(),
        metrics=finding.snapshot.current_metrics,
        evidence=signal_reasons,
    )
    evidence = EvidenceBundle(
        summary="Deterministic strategy metrics crossed degradation thresholds.",
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
        rationale="Open a research optimization issue for AI diagnosis and sandbox experiment planning.",
        requires_human_review=True,
        metadata={"profile": finding.snapshot.profile, "plugin": finding.snapshot.plugin},
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
    return AutomationTask(trigger=trigger, evidence=evidence, proposed_action=proposed, gate_decision=gate)


def issue_for_task(task: AutomationTask) -> dict[str, str]:
    """Build a GitHub issue title/body for a strategy optimization task."""
    payload = task.to_dict()
    trigger = payload["trigger"]
    evidence = payload["evidence"]
    action = payload["proposed_action"]
    title = f"AI strategy optimization proposal: {trigger.get('subject') or action.get('target') or 'strategy profile'}"
    signals = "\n".join(f"- {item}" for item in trigger.get("evidence", [])) or "- Strategy metrics degraded."
    checks = "\n".join(f"- [ ] {item}" for item in payload["gate_decision"].get("required_checks", []))
    risks = "\n".join(f"- {item}" for item in evidence.get("risks", []))
    body = "\n".join(
        [
            "## Summary",
            str(evidence.get("summary") or "Strategy optimization watcher opened this issue."),
            "",
            "## Trigger",
            f"- Severity: `{trigger.get('severity')}`",
            f"- Subject: `{trigger.get('subject')}`",
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
