"""Tests for health-driven automation execution decisions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from service.automation_decision import (
    EXECUTION_DEFER,
    EXECUTION_HUMAN_REVIEW,
    EXECUTION_REVIEW_ONLY,
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

    def test_legacy_autonomy_modes_are_normalized(self) -> None:
        cases = {
            "manual": (MODE_REVIEW_ONLY, "manual", "manual", False),
            "auto_pr": (MODE_REVIEW_AND_FIX, "auto_pr", "auto_pr", False),
            "auto_merge": (MODE_REVIEW_AND_FIX, "auto_merge", "auto_pr", False),
        }
        for requested_mode, (expected_mode, requested_autonomy, effective_autonomy, auto_merge_allowed) in cases.items():
            result = decide_automation_execution(
                repo="QuantStrategyLab/AIAuditBridge",
                requested_mode=requested_mode,
                control_action=CONTROL_CONTINUE,
                service_health="healthy",
                quota_status="ok",
                org_health_status="ok",
            )
            self.assertEqual(result["requested_mode"], expected_mode)
            self.assertEqual(result["requested_autonomy"], requested_autonomy)
            self.assertEqual(result["effective_autonomy"], effective_autonomy)
            self.assertEqual(result["auto_merge_allowed"], auto_merge_allowed)
            self.assertFalse(result["human_review_required"])
        manual_result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode="manual",
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="low",
            org_health_status="ok",
            policy={"default": {"quota_low_behavior": "defer"}},
        )
        self.assertEqual(manual_result["action"], EXECUTION_REVIEW_ONLY)

    def test_auto_merge_requires_matching_repo_autonomy(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode="auto_merge",
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="ok",
            org_health_status="ok",
            policy={"default": {"max_autonomy": "auto_merge"}},
        )

        self.assertEqual(result["effective_autonomy"], "auto_merge")
        self.assertTrue(result["auto_merge_allowed"])

    def test_auto_merge_is_disabled_when_effective_mode_is_review_only(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode="auto_merge",
            control_action=CONTROL_CONTINUE,
            service_health="degraded",
            quota_status="ok",
            org_health_status="ok",
            policy={"default": {"max_autonomy": "auto_merge"}},
        )

        self.assertEqual(result["action"], EXECUTION_REVIEW_ONLY)
        self.assertEqual(result["effective_mode"], MODE_REVIEW_ONLY)
        self.assertFalse(result["auto_fix_allowed"])
        self.assertFalse(result["auto_merge_allowed"])

    def test_degraded_health_forces_review_only(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_PAUSE_AUTO_FIX,
            service_health="degraded",
            quota_status="ok",
            org_health_status="ok",
        )

        self.assertEqual(result["action"], EXECUTION_REVIEW_ONLY)
        self.assertEqual(result["effective_mode"], MODE_REVIEW_ONLY)
        self.assertFalse(result["auto_fix_allowed"])
        self.assertFalse(result["human_review_required"])

    def test_exhausted_quota_defers_execution_without_stronger_guard(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status={"status": "exhausted"},
            org_health_status="ok",
        )

        self.assertEqual(result["action"], EXECUTION_DEFER)
        self.assertTrue(result["defer"])
        self.assertEqual(result["effective_mode"], MODE_REVIEW_ONLY)

    def test_human_review_dominates_exhausted_quota_defer(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_ESCALATE,
            service_health="healthy",
            quota_status={"status": "exhausted"},
            org_health_status="ok",
        )

        self.assertEqual(result["action"], EXECUTION_HUMAN_REVIEW)
        self.assertFalse(result["defer"])
        self.assertEqual(result["effective_mode"], MODE_REVIEW_ONLY)

    def test_low_quota_recommends_low_cost_model(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_PAUSE_AUTO_FIX,
            service_health="healthy",
            quota_status="low",
            org_health_status="ok",
            policy={"default": {"low_cost_model": "gpt-5.4-mini"}},
        )

        self.assertEqual(result["action"], EXECUTION_RUN)
        self.assertEqual(result["effective_provider"], "openai")
        self.assertEqual(result["effective_model"], "gpt-5.4-mini")

    def test_low_quota_overrides_requested_expensive_model(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            requested_model="gpt-5.4-pro",
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="low",
            org_health_status="ok",
            policy={"default": {"low_cost_model": "gpt-5.4-mini"}},
        )

        self.assertEqual(result["effective_provider"], "openai")
        self.assertEqual(result["effective_model"], "gpt-5.4-mini")

    def test_low_quota_does_not_override_model_when_human_review_required(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            requested_model="gpt-5.4-pro",
            control_action=CONTROL_ESCALATE,
            service_health="healthy",
            quota_status="low",
            org_health_status="ok",
            policy={"default": {"low_cost_model": "gpt-5.4-mini", "low_cost_provider": "openai"}},
        )

        self.assertEqual(result["action"], EXECUTION_HUMAN_REVIEW)
        self.assertEqual(result["effective_model"], "gpt-5.4-pro")

    def test_repo_policy_can_force_review_only(self) -> None:
        result = decide_automation_execution(
            repo="quantstrategylab/cryptolivepoolpipelines",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="ok",
            org_health_status="ok",
            policy={"repositories": {"QuantStrategyLab/CryptoLivePoolPipelines": {"max_autonomy": "review_only"}}},
        )

        self.assertEqual(result["action"], EXECUTION_REVIEW_ONLY)
        self.assertEqual(result["effective_mode"], MODE_REVIEW_ONLY)
        self.assertFalse(result["human_review_required"])
        self.assertFalse(result["auto_fix_allowed"])

    def test_failure_streak_matching_is_case_insensitive(self) -> None:
        runs = [
            {"task_name": "monthly", "task_state": "blocked", "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/AIAuditBridge"}},
        ]

        self.assertEqual(consecutive_failure_count(runs, repo="quantstrategylab/aiauditbridge"), 1)

    def test_external_workflow_success_does_not_break_repo_failure_streak(self) -> None:
        runs = [
            {
                "task_name": "monthly",
                "task_state": "merged",
                "metadata": {"origin": "external_workflow", "source_repository": "QuantStrategyLab/AIAuditBridge"},
            },
            {"task_name": "monthly", "task_state": "failed", "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/AIAuditBridge"}},
        ]

        self.assertEqual(consecutive_failure_count(runs, repo="QuantStrategyLab/AIAuditBridge"), 1)

    def test_invalid_repo_autonomy_fails_closed(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/CryptoLivePoolPipelines",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="ok",
            org_health_status="ok",
            policy={"repositories": {"QuantStrategyLab/CryptoLivePoolPipelines": {"max_autonomy": "auto_mrege"}}},
        )

        self.assertEqual(result["max_autonomy"], "manual")
        self.assertEqual(result["action"], EXECUTION_HUMAN_REVIEW)
        self.assertTrue(any("invalid max_autonomy" in reason for reason in result["reasons"]))

    def test_consecutive_failures_force_human_review(self) -> None:
        runs = [
            {"task_name": "monthly", "task_state": "failed", "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/AIAuditBridge"}},
            {
                "task_name": "runtime-health",
                "task_state": "failed",
                "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/AIAuditBridge"},
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

        self.assertEqual(consecutive_failure_count(runs, repo="QuantStrategyLab/AIAuditBridge"), 2)
        self.assertEqual(result["action"], EXECUTION_HUMAN_REVIEW)
        self.assertEqual(result["effective_mode"], MODE_REVIEW_ONLY)

    def test_truncated_failure_history_fails_closed(self) -> None:
        runs = [
            {"task_name": "monthly", "task_state": "failed", "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/AIAuditBridge"}},
        ]

        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="ok",
            org_health_status="ok",
            recent_runs=runs,
            failure_history_complete=False,
            policy={"default": {"max_consecutive_failures": 2}},
        )

        self.assertEqual(consecutive_failure_count(runs, repo="QuantStrategyLab/AIAuditBridge"), 1)
        self.assertEqual(result["action"], EXECUTION_HUMAN_REVIEW)

    def test_running_state_does_not_clear_failure_streak(self) -> None:
        runs = [
            {
                "task_name": "monthly",
                "task_state": "running",
                "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/AIAuditBridge"},
            },
            {"task_name": "monthly", "task_state": "failed", "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/AIAuditBridge"}},
        ]

        self.assertEqual(consecutive_failure_count(runs, repo="QuantStrategyLab/AIAuditBridge"), 1)

    def test_invalid_failure_threshold_falls_back_safely(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="ok",
            org_health_status="ok",
            policy={"default": {"max_consecutive_failures": "oops"}},
        )

        self.assertEqual(result["max_consecutive_failures"], 3)
        self.assertEqual(result["action"], EXECUTION_RUN)

    def test_load_execution_policy_fails_closed_for_malformed_files(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text("not-json", encoding="utf-8")

            self.assertEqual(load_execution_policy(path)["default"]["max_autonomy"], "manual")

            path.write_text(json.dumps({"default": []}), encoding="utf-8")
            self.assertEqual(load_execution_policy(path)["default"]["max_autonomy"], "manual")

            path.write_bytes(b"\xff\xfe\x00")
            self.assertEqual(load_execution_policy(path)["default"]["max_autonomy"], "manual")

            path.write_text(json.dumps({"repositories": {"QuantStrategyLab/AIAuditBridge": "bad"}}), encoding="utf-8")
            self.assertEqual(load_execution_policy(path)["default"]["max_autonomy"], "manual")

            path.write_text(json.dumps({"default": {"max_consecutive_failures": "oops"}, "repositories": {}}), encoding="utf-8")
            self.assertEqual(load_execution_policy(path)["default"]["max_autonomy"], "manual")

            path.write_text(json.dumps({"default": {"max_autnomy": "review_only"}, "repositories": {}}), encoding="utf-8")
            self.assertEqual(load_execution_policy(path)["default"]["max_autonomy"], "manual")

            path.write_text(
                json.dumps(
                    {
                        "default": {
                            "max_autonomy": "review_only",
                            "max_consecutive_failures": 3,
                            "low_cost_model": "gpt-5.4-mini",
                            "low_cost_provider": "openai",
                        },
                        "repositories": {},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_execution_policy(path)["default"]["max_autonomy"], "review_only")

    def test_load_execution_policy_fails_closed_when_path_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_execution_policy()["default"]["max_autonomy"], "manual")

    def test_load_execution_policy_fails_closed_for_relative_env_path(self) -> None:
        env = {
            "CODEX_AUDIT_SERVICE_EXECUTION_POLICY_PATH": "policy.json",
            "CODEX_AUDIT_SERVICE_EXECUTION_POLICY_OWNER": f"{os.getuid()}:{os.getgid()}",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_execution_policy()["default"]["max_autonomy"], "manual")

    def test_load_execution_policy_fails_closed_for_untrusted_env_path(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "default": {
                            "max_autonomy": "auto_pr",
                            "max_consecutive_failures": 3,
                            "low_cost_model": "gpt-5.4-mini",
                            "low_cost_provider": "openai",
                        },
                        "repositories": {},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_EXECUTION_POLICY_PATH": str(path)}, clear=False):
                self.assertEqual(load_execution_policy()["default"]["max_autonomy"], "manual")


if __name__ == "__main__":
    unittest.main()
