from __future__ import annotations

import unittest

from service.strategy_watch import evaluate_strategy_watch, finding_to_automation_task, issue_for_task


class StrategyWatchTest(unittest.TestCase):
    def test_degraded_snapshot_becomes_issue_only_task(self) -> None:
        findings = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "snapshots": [
                    {
                        "strategy_profile": "mean_reversion_live",
                        "plugin": "mean_reversion",
                        "current_metrics": {"sharpe": 0.7, "max_dd": 0.18},
                        "baseline_metrics": {"sharpe": 1.0, "max_dd": 0.1},
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
        self.assertTrue(payload["proposed_action"]["requires_human_review"])
        self.assertTrue(payload["gate_decision"]["human_review_required"])
        self.assertFalse(payload["gate_decision"]["metadata"]["live_impact_allowed"])

    def test_healthy_snapshot_creates_no_finding(self) -> None:
        findings = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "current_metrics": {"sharpe": 1.01, "max_dd": 0.10},
                "baseline_metrics": {"sharpe": 1.0, "max_dd": 0.1},
            }
        )

        self.assertEqual(findings, [])

    def test_issue_body_states_safety_boundary(self) -> None:
        finding = evaluate_strategy_watch(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "profile": "live",
                "current_metrics": {"sharpe": 0.8},
                "baseline_metrics": {"sharpe": 1.0},
            }
        )[0]

        issue = issue_for_task(finding_to_automation_task(finding))

        self.assertIn("AI strategy optimization proposal", issue["title"])
        self.assertIn("only opens an issue", issue["body"])
        self.assertIn("does not modify strategy code", issue["body"])
        self.assertIn("sandbox backtest", issue["body"])


if __name__ == "__main__":
    unittest.main()
