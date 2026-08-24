from __future__ import annotations

import unittest

from service.strategy_watch import (
    RESEARCH_INPUT_UNAVAILABLE_FINDING_TYPE,
    build_research_input_unavailable_finding,
    STRATEGY_WATCH_REGISTRY,
    build_strategy_monitoring_finding,
    evaluate_strategy_watch,
    finding_to_automation_task,
    issue_for_task,
    resolve_strategy_watch_repository,
    watcher_issue_key,
)


class StrategyWatchTest(unittest.TestCase):

    def test_deferred_research_input_creates_issue_only_data_finding(self) -> None:
        finding = build_research_input_unavailable_finding(
            repo="QuantStrategyLab/UsEquitySnapshotPipelines",
            profile="soxl_soxx_trend_income",
            status="DEFERRED",
            reason_code="ALPACA_SIP_ACCESS_FORBIDDEN",
            candidate_id="soxl_soxx_core_only_p2_v3",
            date_cutoff="2026-08-21",
            source="artifact://p1-status.json",
        )

        task = finding_to_automation_task(finding)

        self.assertEqual(finding.finding_type, RESEARCH_INPUT_UNAVAILABLE_FINDING_TYPE)
        self.assertEqual(task.trigger.kind, "strategy_research_input_unavailable")
        self.assertTrue(task.gate_decision.metadata["issue_only"])
        self.assertFalse(task.gate_decision.metadata["live_impact_allowed"])
    def test_strategy_watch_registry_resolves_known_domains_and_fails_closed(self) -> None:
        self.assertEqual(
            {item.domain for item in STRATEGY_WATCH_REGISTRY},
            {"cn_equity", "hk_equity", "us_equity", "crypto"},
        )
        self.assertEqual(resolve_strategy_watch_repository("us_equity"), "QuantStrategyLab/UsEquityStrategies")
        self.assertEqual(resolve_strategy_watch_repository("unknown"), "")

    def test_monitoring_trigger_becomes_issue_only_optimization_record(self) -> None:
        finding = build_strategy_monitoring_finding(
            domain="us_equity",
            profile="global_etf_rotation",
            severity="high",
            metrics={"overall_score": 14.2, "performance_score": 0.0},
            signals=[
                {
                    "metric": "overall_score",
                    "reason": "overall_score=14.2 is below monitoring threshold 60.0",
                }
            ],
            source="quant-monitor/daily-briefing",
            generated_at="2026-07-31T00:00:00Z",
        )

        task = finding_to_automation_task(finding)
        payload = task.to_dict()

        self.assertEqual(finding.snapshot.repo, "QuantStrategyLab/UsEquityStrategies")
        self.assertEqual(finding.finding_type, "monitoring_trigger")
        self.assertEqual(payload["trigger"]["kind"], "strategy_monitoring_trigger")
        self.assertEqual(payload["proposed_action"]["action"], "open_issue")
        self.assertFalse(payload["gate_decision"]["metadata"]["live_impact_allowed"])
        self.assertIn("bounded, no-order", payload["proposed_action"]["rationale"])

    def test_degraded_snapshot_becomes_issue_only_task(self) -> None:
        findings = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "snapshots": [
                    {
                        "strategy_profile": "mean_reversion_live",
                        "plugin": "mean_reversion",
                        "schema_version": "strategy_performance.v2",
                        "metrics_kind": "performance",
                        "current_metrics": {"sharpe": 0.7, "cagr": 0.11, "calmar": 0.6, "win_rate": 0.52, "max_dd": 0.18},
                        "baseline_metrics": {"sharpe": 1.0, "cagr": 0.18, "calmar": 1.0, "win_rate": 0.58, "max_dd": 0.1},
                        "source": "data/output/strategy_metrics.json",
                    }
                ],
            }
        )

        self.assertEqual(len(findings), 1)
        task = finding_to_automation_task(findings[0])
        payload = task.to_dict()
        self.assertTrue(task.is_actionable)
        self.assertEqual(payload["proposed_action"]["action"], "open_issue")
        self.assertFalse(payload["proposed_action"]["requires_human_review"])
        self.assertFalse(payload["gate_decision"]["human_review_required"])
        self.assertFalse(payload["gate_decision"]["metadata"]["live_impact_allowed"])

    def test_malformed_metrics_snapshot_is_ignored_without_crashing(self) -> None:
        findings = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "snapshots": [
                    {"profile": "bad", "current_metrics": "oops", "baseline_metrics": {"sharpe": 1.0}},
                    {
                        "profile": "live",
                        "schema_version": "strategy_performance.v2",
                        "metrics_kind": "performance",
                        "current_metrics": {"sharpe": 0.5, "cagr": 0.1, "calmar": 0.7, "win_rate": 0.52, "max_dd": 0.12},
                        "baseline_metrics": {"sharpe": 1.0, "cagr": 0.2, "calmar": 1.0, "win_rate": 0.58, "max_dd": 0.08},
                    },
                ],
            }
        )

        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0].finding_type, "data_quality")
        self.assertEqual(findings[1].snapshot.profile, "live")

    def test_healthy_snapshot_creates_no_finding(self) -> None:
        findings = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "schema_version": "strategy_performance.v2",
                "metrics_kind": "performance",
                "current_metrics": {"sharpe": 1.01, "cagr": 0.2, "calmar": 1.0, "win_rate": 0.58, "max_dd": 0.10},
                "baseline_metrics": {"sharpe": 1.0, "cagr": 0.2, "calmar": 1.0, "win_rate": 0.58, "max_dd": 0.1},
            }
        )

        self.assertEqual(findings, [])

    def test_issue_body_states_safety_boundary(self) -> None:
        finding = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "profile": "live",
                "schema_version": "strategy_performance.v2",
                "metrics_kind": "performance",
                "current_metrics": {"sharpe": 0.8, "cagr": 0.1, "calmar": 0.8, "win_rate": 0.55, "max_dd": 0.12},
                "baseline_metrics": {"sharpe": 1.0, "cagr": 0.2, "calmar": 1.0, "win_rate": 0.55, "max_dd": 0.12},
            }
        )[0]

        issue = issue_for_task(finding_to_automation_task(finding))

        self.assertIn("AI strategy optimization proposal", issue["title"])
        self.assertNotRegex(issue["title"], r"\[[a-f0-9]{12}\]$")
        self.assertIn("Event key", issue["body"])
        self.assertIn("<!-- strategy-optimization-watcher:", issue["body"])
        self.assertIn("only opens an issue", issue["body"])
        self.assertIn("does not modify strategy code", issue["body"])
        self.assertIn("sandbox backtest", issue["body"])

    def test_operational_metrics_payload_becomes_data_quality_finding(self) -> None:
        findings = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "schema_version": "strategy_operational_metrics.v1",
                "metrics_kind": "operational_quality",
                "profile": "live",
                "current_metrics": {"pool_size": 12},
                "baseline_metrics": {"pool_size": 10},
            }
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_type, "data_quality")
        task = finding_to_automation_task(findings[0])
        payload = task.to_dict()
        self.assertEqual(payload["trigger"]["kind"], "strategy_metrics_contract_invalid")
        self.assertIn("strategy_performance.v2", payload["trigger"]["reason"])
        self.assertEqual(payload["metadata"]["finding_type"], "data_quality")

    def test_legacy_performance_payload_remains_compatible(self) -> None:
        findings = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "profile": "legacy",
                "current_metrics": {"sharpe": 0.5},
                "baseline_metrics": {"sharpe": 1.0},
            }
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_type, "metric_degradation")

    def test_single_performance_discriminator_remains_compatible(self) -> None:
        base = {
            "repo": "QuantStrategyLab/TestStrategies",
            "profile": "live",
            "current_metrics": {"sharpe": 0.5, "cagr": 0.1, "calmar": 0.7, "win_rate": 0.52, "max_dd": 0.12},
            "baseline_metrics": {"sharpe": 1.0, "cagr": 0.2, "calmar": 1.0, "win_rate": 0.58, "max_dd": 0.08},
        }

        for discriminator in ({"schema_version": "strategy_performance.v2"}, {"metrics_kind": "performance"}):
            findings = evaluate_strategy_watch({**base, **discriminator})
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].finding_type, "metric_degradation")

    def test_invalid_numeric_value_becomes_data_quality_finding(self) -> None:
        findings = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "schema_version": "strategy_performance.v2",
                "metrics_kind": "performance",
                "profile": "live",
                "current_metrics": {"sharpe": "oops", "cagr": 0.1, "calmar": 0.7, "win_rate": 0.52, "max_dd": 0.12},
                "baseline_metrics": {"sharpe": 1.0, "cagr": 0.2, "calmar": 1.0, "win_rate": 0.58, "max_dd": 0.08},
            }
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_type, "data_quality")

    def test_data_quality_issue_key_does_not_collide_with_metric_issue(self) -> None:
        metric_finding = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "profile": "live",
                "current_metrics": {"sharpe": 0.5},
                "baseline_metrics": {"sharpe": 1.0},
            }
        )[0]
        quality_finding = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "schema_version": "strategy_operational_metrics.v1",
                "metrics_kind": "operational_quality",
                "profile": "live",
                "current_metrics": {"pool_size": 10},
                "baseline_metrics": {"pool_size": 9},
            }
        )[0]

        self.assertNotEqual(
            watcher_issue_key(finding_to_automation_task(metric_finding)),
            watcher_issue_key(finding_to_automation_task(quality_finding)),
        )

    def test_malformed_snapshot_entry_becomes_data_quality_finding(self) -> None:
        findings = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "schema_version": "strategy_performance.v2",
                "metrics_kind": "performance",
                "snapshots": [None],
            }
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_type, "data_quality")


if __name__ == "__main__":
    unittest.main()
