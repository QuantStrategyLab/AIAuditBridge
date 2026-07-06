"""Tests for health-driven automation execution decisions."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from service.automation_decision import (
    EXECUTION_DEFER,
    EXECUTION_HUMAN_REVIEW,
    EXECUTION_RUN,
    MODE_REVIEW_AND_FIX,
    MODE_REVIEW_ONLY,
    consecutive_failure_count,
    decide_automation_execution,
    load_execution_policy,
)
from service.automation_run_ledger import CONTROL_CONTINUE, CONTROL_ESCALATE, CONTROL_PAUSE_AUTO_FIX


class TestAutomationDecision(unittest.TestCase):
    def test_healthy_control_allows_review_and_fix(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="ok",
            org_health_status="ok",
        )

        self.assertEqual(result["action"], EXECUTION_RUN)
        self.assertEqual(result["effective_mode"], MODE_REVIEW_AND_FIX)
        self.assertTrue(result["auto_fix_allowed"])

    def test_degraded_health_forces_review_only(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_PAUSE_AUTO_FIX,
            service_health="degraded",
            quota_status="ok",
            org_health_status="ok",
        )

        self.assertEqual(result["effective_mode"], MODE_REVIEW_ONLY)
        self.assertFalse(result["auto_fix_allowed"])
        self.assertTrue(result["human_review_required"])

    def test_exhausted_quota_defers_execution(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_ESCALATE,
            service_health="healthy",
            quota_status={"status": "exhausted"},
            org_health_status="ok",
        )

        self.assertEqual(result["action"], EXECUTION_DEFER)
        self.assertTrue(result["defer"])
        self.assertEqual(result["effective_mode"], MODE_REVIEW_ONLY)

    def test_low_quota_recommends_low_cost_model(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="low",
            org_health_status="ok",
            policy={"default": {"low_cost_model": "gpt-5.4-mini"}},
        )

        self.assertEqual(result["action"], EXECUTION_RUN)
        self.assertEqual(result["effective_model"], "gpt-5.4-mini")
        self.assertTrue(any("low-cost model" in reason for reason in result["reasons"]))

    def test_repo_policy_can_force_review_only(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/CryptoLivePoolPipelines",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="ok",
            org_health_status="ok",
            policy={"repositories": {"QuantStrategyLab/CryptoLivePoolPipelines": {"max_autonomy": "review_only"}}},
        )

        self.assertEqual(result["effective_mode"], MODE_REVIEW_ONLY)
        self.assertTrue(result["human_review_required"])
        self.assertFalse(result["auto_fix_allowed"])

    def test_consecutive_failures_force_human_review(self) -> None:
        runs = [
            {
                "task_name": "monthly",
                "task_state": "failed",
                "metadata": {"source_repository": "QuantStrategyLab/AIAuditBridge"},
            },
            {
                "task_name": "monthly",
                "task_state": "failed",
                "metadata": {"source_repository": "QuantStrategyLab/AIAuditBridge"},
            },
        ]

        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            task_name="monthly",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="ok",
            org_health_status="ok",
            recent_runs=runs,
            policy={"default": {"max_consecutive_failures": 2}},
        )

        self.assertEqual(consecutive_failure_count(runs, repo="QuantStrategyLab/AIAuditBridge", task_name="monthly"), 2)
        self.assertEqual(result["action"], EXECUTION_HUMAN_REVIEW)
        self.assertEqual(result["effective_mode"], MODE_REVIEW_ONLY)

    def test_load_execution_policy_ignores_malformed_files(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text("not-json", encoding="utf-8")

            self.assertEqual(load_execution_policy(path), {})

            path.write_text(json.dumps({"default": {"max_autonomy": "review_only"}}), encoding="utf-8")
            self.assertEqual(load_execution_policy(path)["default"]["max_autonomy"], "review_only")


if __name__ == "__main__":
    unittest.main()
