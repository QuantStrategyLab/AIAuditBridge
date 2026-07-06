"""Tests for automation control-plane execution decisions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from service.ai_gateway_service import _automation_control_snapshot


class TestAutomationControlSnapshot(unittest.TestCase):
    def test_control_snapshot_preserves_continue_for_healthy_default_mode(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": []}})()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo")

        self.assertEqual(control["action"], "continue")
        self.assertEqual(control["execution"]["effective_mode"], "review_and_fix")
        self.assertTrue(control["execution"]["auto_fix_allowed"])

    def test_control_snapshot_applies_service_owned_execution_policy(self) -> None:
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "execution_policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "repositories": {
                            "QuantStrategyLab/TargetRepo": {
                                "max_autonomy": "review_only",
                                "max_consecutive_failures": 2,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            health = type("Health", (), {"status": "healthy"})()
            quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
            ledger = type("Ledger", (), {"snapshot": lambda self, limit=20: {"runs": []}})()

            with (
                patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_EXECUTION_POLICY_PATH": str(policy_path)}, clear=False),
                patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
                patch("service.ai_gateway_service.get_health_monitor", return_value=health),
                patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
                patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            ):
                control = _automation_control_snapshot("QuantStrategyLab/TargetRepo")

        self.assertEqual(control["action"], "review_only")
        self.assertEqual(control["execution"]["effective_mode"], "review_only")
        self.assertFalse(control["execution"]["auto_fix_allowed"])

    def test_control_snapshot_scans_full_retained_ledger_for_repo_failure_streak(self) -> None:
        runs = [
            {
                "task_name": f"other-{index}",
                "task_state": "merged",
                "metadata": {"source_repository": f"QuantStrategyLab/Other{index}"},
            }
            for index in range(20)
        ]
        runs.extend(
            [
                {
                    "task_name": "monthly",
                    "task_state": "failed",
                    "metadata": {"source_repository": "QuantStrategyLab/TargetRepo"},
                },
                {
                    "task_name": "monthly",
                    "task_state": "failed",
                    "metadata": {"source_repository": "QuantStrategyLab/TargetRepo"},
                },
            ]
        )
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()

        class Ledger:
            requested_limit = object()

            def snapshot(self, limit=100):
                self.requested_limit = limit
                return {"runs": runs}

        ledger = Ledger()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={"default": {"max_consecutive_failures": 2}}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo", task_name="monthly")

        self.assertIsNone(ledger.requested_limit)
        self.assertEqual(control["action"], "escalate")
        self.assertEqual(control["execution"]["action"], "human_review")
        self.assertEqual(control["execution"]["consecutive_failures"], 2)

    def test_control_snapshot_fails_closed_when_ledger_is_unavailable(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: (_ for _ in ()).throw(RuntimeError("boom"))})()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo")

        self.assertEqual(control["action"], "escalate")
        self.assertEqual(control["execution"]["action"], "human_review")


if __name__ == "__main__":
    unittest.main()
