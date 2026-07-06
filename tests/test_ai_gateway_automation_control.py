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

        self.assertEqual(control["action"], "continue")
        self.assertEqual(control["execution"]["effective_mode"], "review_only")
        self.assertFalse(control["execution"]["auto_fix_allowed"])


if __name__ == "__main__":
    unittest.main()
