"""Tests for service/autonomy.py — autonomy policy and decision matrix."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from service.autonomy import (
    ACTION_AUTO_MERGE,
    ACTION_AUTO_PR,
    ACTION_ESCALATE,
    AutonomyConfig,
    classify_file_risk,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_CRITICAL,
    AUTONOMY_POLICY_PATH_ENV,
    DEFAULT_DECISION_MATRIX,
    load_autonomy_policy,
    recommended_action,
)

LIVE_EQUIVALENT_EVIDENCE = {
    "change_class": "live_equivalent_optimization",
    "baseline_profile_runtime_enabled": True,
    "strategy_family_unchanged": True,
    "public_contract_unchanged": True,
    "broker_permission_unchanged": True,
    "risk_limits_not_increased": True,
    "backtest_passed": True,
    "shadow_or_regression_passed": True,
    "rollback_ready": True,
}


class TestDefaultDecisionMatrix(unittest.TestCase):
    def test_critical_always_escalates(self) -> None:
        self.assertEqual(recommended_action([{"confidence": 0.99}], ["secrets.txt"])["action"], ACTION_ESCALATE)

    def test_high_risk_never_auto_merges(self) -> None:
        self.assertEqual(recommended_action([{"confidence": 0.95}], ["src/quant_strategy.py"])["action"], ACTION_AUTO_PR)
        self.assertEqual(recommended_action([{"confidence": 0.84}], ["src/quant_strategy.py"])["action"], ACTION_ESCALATE)
        self.assertEqual(classify_file_risk("SRC/QUANT_STRATEGY.py"), RISK_HIGH)

    def test_market_strategy_paths_are_high_risk(self) -> None:
        for path in (
            "src/us_equity_strategies/tqqq.py",
            "src/cn_equity_strategies/kc50.py",
            "src/hk_equity_strategies/hsi.py",
            "src/crypto_strategies/btc.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(classify_file_risk(path), RISK_HIGH)

    def test_low_risk_high_confidence_can_auto_merge(self) -> None:
        result = recommended_action([{"confidence": 0.96}], ["docs/README.md"])
        self.assertEqual(result["action"], ACTION_AUTO_MERGE)
        self.assertEqual(result["risk"], RISK_LOW)

    def test_medium_risk_defaults_to_auto_pr_not_merge(self) -> None:
        self.assertEqual(recommended_action([{"confidence": 0.90}], ["scripts/build.py"])["action"], ACTION_AUTO_PR)
        self.assertEqual(recommended_action([{"confidence": 0.69}], ["scripts/build.py"])["action"], ACTION_ESCALATE)

    def test_default_matrix_reflects_safer_thresholds(self) -> None:
        self.assertIn((RISK_LOW, 0.60, ACTION_AUTO_MERGE), DEFAULT_DECISION_MATRIX)
        self.assertIn((RISK_MEDIUM, 0.70, ACTION_AUTO_PR), DEFAULT_DECISION_MATRIX)
        self.assertIn((RISK_HIGH, 0.85, ACTION_AUTO_PR), DEFAULT_DECISION_MATRIX)
        self.assertNotIn((RISK_HIGH, 0.95, ACTION_AUTO_MERGE), DEFAULT_DECISION_MATRIX)

    def test_shared_policy_classifies_blocked_and_low_risk_paths(self) -> None:
        policy = {
            "version": 7,
            "blocked_path_patterns": [r"(^|/).*token.*$"],
            "risk_policy": {
                "low": {"prefixes": ["docs/"], "exact": ["CHANGELOG.md"]},
                "high": {"prefixes": ["src/quant_"]},
            },
        }

        self.assertEqual(classify_file_risk("docs/runbook.md", policy=policy), RISK_LOW)
        self.assertEqual(classify_file_risk("CHANGELOG.md", policy=policy), RISK_LOW)
        self.assertEqual(classify_file_risk("src/quant_alpha.py", policy=policy), RISK_HIGH)
        self.assertEqual(classify_file_risk("config/token.txt", policy=policy), RISK_CRITICAL)
        self.assertEqual(classify_file_risk("config/secret.pem", policy=policy), RISK_CRITICAL)

    def test_policy_load_does_not_depend_on_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(__file__).resolve().parents[1] / ".github" / "codex_auto_merge_policy.json"
            old_cwd = os.getcwd()
            old_env = os.environ.get(AUTONOMY_POLICY_PATH_ENV)
            os.environ[AUTONOMY_POLICY_PATH_ENV] = str(policy_path)
            try:
                os.chdir(tmp)
                policy = load_autonomy_policy()
            finally:
                os.chdir(old_cwd)
                if old_env is None:
                    os.environ.pop(AUTONOMY_POLICY_PATH_ENV, None)
                else:
                    os.environ[AUTONOMY_POLICY_PATH_ENV] = old_env

        self.assertEqual(policy.get("version"), 1)

    def test_policy_is_not_loaded_from_repo_by_default(self) -> None:
        old_env = os.environ.pop(AUTONOMY_POLICY_PATH_ENV, None)
        try:
            self.assertEqual(load_autonomy_policy(), {})
        finally:
            if old_env is not None:
                os.environ[AUTONOMY_POLICY_PATH_ENV] = old_env

    def test_autonomy_policy_file_cannot_be_downgraded_by_policy(self) -> None:
        malicious_policy = {
            "risk_policy": {
                "low": {"exact": [".github/codex_auto_merge_policy.json"]},
            },
        }

        self.assertEqual(
            classify_file_risk(".github/codex_auto_merge_policy.json", policy=malicious_policy),
            RISK_CRITICAL,
        )
        result = recommended_action(
            [{"confidence": 0.99}],
            [".github/codex_auto_merge_policy.json"],
            policy=malicious_policy,
        )
        self.assertEqual(result["action"], ACTION_ESCALATE)

    def test_degraded_health_caps_auto_merge_to_auto_pr(self) -> None:
        result = recommended_action([{"confidence": 0.99}], ["docs/runbook.md"], health_status="degraded")

        self.assertEqual(result["initial_action"], ACTION_AUTO_MERGE)
        self.assertEqual(result["action"], ACTION_AUTO_PR)
        self.assertFalse(result["auto_merge_allowed"])
        self.assertTrue(result["runtime_guards"])

    def test_unhealthy_health_forces_human_review(self) -> None:
        result = recommended_action([{"confidence": 0.99}], ["docs/runbook.md"], health_status="unhealthy")

        self.assertEqual(result["initial_action"], ACTION_AUTO_MERGE)
        self.assertEqual(result["action"], ACTION_ESCALATE)
        self.assertTrue(result["human_review_required"])

    def test_low_quota_caps_auto_merge_to_auto_pr(self) -> None:
        result = recommended_action([{"confidence": 0.99}], ["docs/runbook.md"], quota_status="low")

        self.assertEqual(result["initial_action"], ACTION_AUTO_MERGE)
        self.assertEqual(result["action"], ACTION_AUTO_PR)
        self.assertFalse(result["auto_merge_allowed"])
        self.assertTrue(any("quota status is low" in guard for guard in result["runtime_guards"]))

    def test_exhausted_quota_forces_human_review(self) -> None:
        result = recommended_action([{"confidence": 0.99}], ["docs/runbook.md"], quota_status="exhausted")

        self.assertEqual(result["initial_action"], ACTION_AUTO_MERGE)
        self.assertEqual(result["action"], ACTION_ESCALATE)
        self.assertTrue(result["human_review_required"])

    def test_live_equivalent_optimization_stays_bounded_by_default_policy(self) -> None:
        result = recommended_action(
            [{"confidence": 0.99}],
            ["src/quant_strategy.py"],
            trusted_automation_metadata=LIVE_EQUIVALENT_EVIDENCE,
        )

        self.assertEqual(result["risk"], RISK_HIGH)
        self.assertEqual(result["action"], ACTION_AUTO_PR)
        self.assertTrue(result["human_review_required"])

    def test_live_equivalent_optimization_can_auto_merge_when_policy_allows(self) -> None:
        config = AutonomyConfig(
            decision_matrix=[
                (RISK_HIGH, 0.95, ACTION_AUTO_MERGE),
                (RISK_HIGH, 0.00, ACTION_ESCALATE),
            ]
        )
        result = recommended_action(
            [{"confidence": 0.99}],
            ["src/quant_strategy.py"],
            config=config,
            trusted_automation_metadata=LIVE_EQUIVALENT_EVIDENCE,
        )

        self.assertEqual(result["action"], ACTION_AUTO_MERGE)
        self.assertFalse(result["human_review_required"])

    def test_live_equivalent_optimization_missing_evidence_stays_review_gated(self) -> None:
        result = recommended_action(
            [{"confidence": 0.99}],
            ["src/quant_strategy.py"],
            trusted_automation_metadata={"change_class": "live_equivalent_optimization"},
        )

        self.assertEqual(result["action"], ACTION_AUTO_PR)
        self.assertTrue(result["human_review_required"])
        self.assertIn("backtest_passed", result["automation_authority"]["missing_evidence"])

    def test_live_equivalent_metadata_cannot_override_critical_paths(self) -> None:
        result = recommended_action(
            [{"confidence": 0.99}],
            [".github/workflows/deploy.yml"],
            trusted_automation_metadata=LIVE_EQUIVALENT_EVIDENCE,
        )

        self.assertEqual(result["action"], ACTION_ESCALATE)
        self.assertTrue(result["human_review_required"])

    def test_untrusted_live_equivalent_metadata_cannot_auto_merge_strategy_code(self) -> None:
        result = recommended_action(
            [{"confidence": 0.99}],
            ["src/quant_strategy.py"],
            automation_metadata=LIVE_EQUIVALENT_EVIDENCE,
        )

        self.assertEqual(result["action"], ACTION_AUTO_PR)
        self.assertTrue(result["human_review_required"])

    def test_live_equivalent_auto_merge_blocked_by_org_health_guard(self) -> None:
        config = AutonomyConfig(
            decision_matrix=[
                (RISK_HIGH, 0.95, ACTION_AUTO_MERGE),
                (RISK_HIGH, 0.00, ACTION_ESCALATE),
            ]
        )
        result = recommended_action(
            [{"confidence": 0.99}],
            ["src/quant_strategy.py"],
            config=config,
            trusted_automation_metadata=LIVE_EQUIVALENT_EVIDENCE,
            org_health_status="degraded",
        )

        self.assertEqual(result["action"], ACTION_AUTO_PR)
        self.assertTrue(result["runtime_guards"])
        self.assertFalse(result["auto_merge_allowed"])
