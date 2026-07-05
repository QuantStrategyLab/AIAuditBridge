"""Tests for automation authority policy."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from service.automation_authority import (
    CLASS_LIVE_EQUIVALENT_OPTIMIZATION,
    CLASS_PLUGIN_POSITION_CONTROL,
    CLASS_SECURITY_PERMISSION_BOUNDARY,
    POLICY_SCHEMA_VERSION,
    evaluate_automation_authority,
    load_automation_authority_policy,
)
from service.autonomy import ACTION_AUTO_MERGE, ACTION_AUTO_NOTIFY, ACTION_AUTO_PR, ACTION_ESCALATE


LIVE_EQUIVALENT_EVIDENCE = {
    "baseline_profile_runtime_enabled": True,
    "strategy_family_unchanged": True,
    "public_contract_unchanged": True,
    "broker_permission_unchanged": True,
    "risk_limits_not_increased": True,
    "backtest_passed": True,
    "shadow_or_regression_passed": True,
    "rollback_ready": True,
}


class TestAutomationAuthority(unittest.TestCase):
    def test_trusted_live_equivalent_optimization_allows_auto_merge_with_complete_evidence(self) -> None:
        result = evaluate_automation_authority(
            ["src/us_equity_strategies/tqqq.py"],
            trusted_metadata={
                "change_class": CLASS_LIVE_EQUIVALENT_OPTIMIZATION,
                **LIVE_EQUIVALENT_EVIDENCE,
            },
            proposed_action=ACTION_AUTO_MERGE,
        )

        self.assertFalse(result["human_review_required"])
        self.assertEqual(result["final_action"], ACTION_AUTO_MERGE)

    def test_untrusted_live_equivalent_metadata_cannot_relax_strategy_code(self) -> None:
        result = evaluate_automation_authority(
            ["src/us_equity_strategies/tqqq.py"],
            metadata={
                "change_class": CLASS_LIVE_EQUIVALENT_OPTIMIZATION,
                **LIVE_EQUIVALENT_EVIDENCE,
            },
            proposed_action=ACTION_AUTO_MERGE,
        )

        self.assertTrue(result["human_review_required"])
        self.assertEqual(result["final_action"], ACTION_AUTO_PR)
        self.assertEqual(result["change_class"], "unknown_change")

    def test_trusted_live_equivalent_optimization_missing_evidence_requires_review(self) -> None:
        result = evaluate_automation_authority(
            ["src/us_equity_strategies/tqqq.py"],
            trusted_metadata={"change_class": CLASS_LIVE_EQUIVALENT_OPTIMIZATION},
            proposed_action=ACTION_AUTO_MERGE,
        )

        self.assertTrue(result["human_review_required"])
        self.assertEqual(result["final_action"], ACTION_AUTO_PR)
        self.assertIn("backtest_passed", result["missing_evidence"])

    def test_workflow_permission_changes_always_escalate(self) -> None:
        result = evaluate_automation_authority(
            [".github/workflows/deploy.yml"],
            proposed_action=ACTION_AUTO_MERGE,
        )

        self.assertEqual(result["change_class"], CLASS_SECURITY_PERMISSION_BOUNDARY)
        self.assertEqual(result["final_action"], ACTION_ESCALATE)
        self.assertTrue(result["human_review_required"])

    def test_security_path_overrides_are_case_insensitive(self) -> None:
        for path in ("config/SecretRotator.py", "src/BrokerAdapter.py"):
            with self.subTest(path=path):
                result = evaluate_automation_authority([path], proposed_action=ACTION_AUTO_MERGE)

                self.assertIn(
                    result["change_class"],
                    {"security_permission_or_secret", "broker_or_order_execution"},
                )
                self.assertEqual(result["final_action"], ACTION_ESCALATE)
                self.assertTrue(result["human_review_required"])

    def test_mixed_changed_paths_use_most_restrictive_class(self) -> None:
        result = evaluate_automation_authority(
            [
                "src/quant_strategy_plugins/plugin_policies.py",
                ".github/workflows/deploy.yml",
            ],
            proposed_action=ACTION_AUTO_MERGE,
        )

        self.assertEqual(result["change_class"], CLASS_SECURITY_PERMISSION_BOUNDARY)
        self.assertEqual(result["final_action"], ACTION_ESCALATE)
        self.assertTrue(result["human_review_required"])

    def test_plugin_policy_changes_require_human_review(self) -> None:
        result = evaluate_automation_authority(
            ["src/quant_strategy_plugins/plugin_policies.py"],
            proposed_action=ACTION_AUTO_MERGE,
        )

        self.assertEqual(result["change_class"], CLASS_PLUGIN_POSITION_CONTROL)
        self.assertEqual(result["final_action"], ACTION_AUTO_PR)
        self.assertTrue(result["human_review_required"])

    def test_review_gated_path_class_overrides_live_equivalent_metadata(self) -> None:
        result = evaluate_automation_authority(
            ["web/strategy-switch-console/strategy-profiles.example.json"],
            metadata={
                "change_class": CLASS_LIVE_EQUIVALENT_OPTIMIZATION,
                **LIVE_EQUIVALENT_EVIDENCE,
            },
            proposed_action=ACTION_AUTO_MERGE,
        )

        self.assertEqual(result["change_class"], "live_candidate_promotion")
        self.assertEqual(result["final_action"], ACTION_AUTO_PR)
        self.assertTrue(result["human_review_required"])

        stricter = evaluate_automation_authority(
            ["web/strategy-switch-console/strategy-profiles.example.json"],
            trusted_metadata={"change_class": "broker_or_order_execution"},
            proposed_action=ACTION_AUTO_MERGE,
        )
        self.assertEqual(stricter["final_action"], ACTION_ESCALATE)

    def test_human_review_classes_do_not_allow_auto_notify(self) -> None:
        result = evaluate_automation_authority(
            ["src/quant_strategy_plugins/plugin_policies.py"],
            proposed_action=ACTION_AUTO_NOTIFY,
        )

        self.assertEqual(result["change_class"], CLASS_PLUGIN_POSITION_CONTROL)
        self.assertEqual(result["final_action"], ACTION_AUTO_PR)
        self.assertTrue(result["human_review_required"])

    def test_custom_policy_merges_with_default_security_classes(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "authority.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": POLICY_SCHEMA_VERSION,
                        "classes": {
                            "routine_low_risk": {
                                "authority": "auto_allowed",
                                "max_action": "auto_pr",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            policy = load_automation_authority_policy(path)
            result = evaluate_automation_authority(
                [".github/workflows/deploy.yml"],
                policy=policy,
                proposed_action=ACTION_AUTO_MERGE,
            )

        self.assertEqual(result["change_class"], CLASS_SECURITY_PERMISSION_BOUNDARY)
        self.assertEqual(result["final_action"], ACTION_ESCALATE)
