"""Tests for service/autonomy.py — autonomy policy and decision matrix."""

from __future__ import annotations

import unittest

from service.autonomy import (
    ACTION_AUTO_MERGE,
    ACTION_AUTO_PR,
    ACTION_ESCALATE,
    classify_file_risk,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_CRITICAL,
    DEFAULT_DECISION_MATRIX,
    recommended_action,
)


class TestDefaultDecisionMatrix(unittest.TestCase):
    def test_critical_always_escalates(self) -> None:
        self.assertEqual(recommended_action([{"confidence": 0.99}], ["secrets.txt"])["action"], ACTION_ESCALATE)

    def test_high_risk_never_auto_merges(self) -> None:
        self.assertEqual(recommended_action([{"confidence": 0.95}], ["src/quant_strategy.py"])["action"], ACTION_AUTO_PR)
        self.assertEqual(recommended_action([{"confidence": 0.84}], ["src/quant_strategy.py"])["action"], ACTION_ESCALATE)

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
