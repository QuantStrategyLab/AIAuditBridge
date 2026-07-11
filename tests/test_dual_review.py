"""Tests for dual-review arbitration foundation."""

from __future__ import annotations

import unittest

from service.dual_review import (
    DEFAULT_ESCALATION_THRESHOLD,
    VERDICT_DISAGREEMENT,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_UNAVAILABLE,
    DualReviewTrigger,
    compare_reviews,
    compare_three_reviews,
    should_escalate,
)


class TestShouldEscalate(unittest.TestCase):
    def test_high_confidence_does_not_escalate(self) -> None:
        self.assertFalse(should_escalate(0.8))
        self.assertFalse(should_escalate(0.95))

    def test_low_confidence_escalates(self) -> None:
        self.assertTrue(should_escalate(0.79))
        self.assertTrue(should_escalate(0.0))

    def test_custom_threshold(self) -> None:
        self.assertFalse(should_escalate(0.7, threshold=0.6))
        self.assertTrue(should_escalate(0.59, threshold=0.6))

    def test_invalid_confidence_fails_closed(self) -> None:
        self.assertTrue(should_escalate("oops"))  # type: ignore[arg-type]
        self.assertTrue(should_escalate(float("nan")))
        self.assertTrue(should_escalate("nan"))  # type: ignore[arg-type]

    def test_non_finite_threshold_falls_back(self) -> None:
        self.assertFalse(should_escalate(0.9, threshold=float("nan")))
        self.assertTrue(should_escalate(0.7, threshold="nan"))  # type: ignore[arg-type]

    def test_default_threshold_constant(self) -> None:
        self.assertEqual(DEFAULT_ESCALATION_THRESHOLD, 0.8)


class TestCompareReviews(unittest.TestCase):
    def test_both_pass(self) -> None:
        result = compare_reviews({"verdict": "approve"}, {"consensus": "pass"})
        self.assertEqual(result["verdict"], VERDICT_PASS)
        self.assertTrue(result["agreement"])

    def test_both_fail(self) -> None:
        result = compare_reviews({"verdict": "reject"}, {"decision": "fail"})
        self.assertEqual(result["verdict"], VERDICT_FAIL)
        self.assertTrue(result["agreement"])

    def test_disagreement(self) -> None:
        result = compare_reviews({"verdict": "approve"}, {"verdict": "reject"})
        self.assertEqual(result["verdict"], VERDICT_DISAGREEMENT)
        self.assertFalse(result["agreement"])
        self.assertEqual(result["primary_verdict"], VERDICT_PASS)
        self.assertEqual(result["secondary_verdict"], VERDICT_FAIL)

    def test_missing_verdict_is_disagreement(self) -> None:
        result = compare_reviews({"confidence": 0.9}, {"verdict": "pass"})
        self.assertEqual(result["verdict"], VERDICT_DISAGREEMENT)
        self.assertIsNone(result["primary_verdict"])

    def test_confidence_is_preserved(self) -> None:
        result = compare_reviews(
            {"verdict": "pass", "confidence": 0.72},
            {"verdict": "pass", "ai_confidence": 0.91},
        )
        self.assertEqual(result["primary_confidence"], 0.72)
        self.assertEqual(result["secondary_confidence"], 0.91)


class TestCompareThreeReviews(unittest.TestCase):
    def test_unanimous_pass(self) -> None:
        result = compare_three_reviews(
            {"verdict": "approve", "confidence": 0.6},
            {"verdict": "pass", "confidence": 0.85},
            {"verdict": "approved", "confidence": 0.82},
        )
        self.assertEqual(result["verdict"], VERDICT_PASS)
        self.assertTrue(result["agreement"])

    def test_split_decision(self) -> None:
        result = compare_three_reviews(
            {"verdict": "approve", "confidence": 0.6},
            {"verdict": "pass", "confidence": 0.85},
            {"verdict": "reject", "confidence": 0.9},
        )
        self.assertEqual(result["verdict"], VERDICT_DISAGREEMENT)
        self.assertIn("split decision", result["reason"])

    def test_all_unavailable_is_not_unanimous_rejection(self) -> None:
        unavailable = {"verdict": "unavailable", "confidence": 0.0, "error": "provider unavailable"}
        result = compare_three_reviews(unavailable, unavailable, unavailable)
        self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)
        self.assertFalse(result["agreement"])

    def test_two_available_reviewers_form_quorum(self) -> None:
        result = compare_three_reviews(
            {"verdict": "approve", "confidence": 0.9},
            {"verdict": "unavailable", "confidence": 0.0, "error": "provider unavailable"},
            {"verdict": "approve", "confidence": 0.9},
        )
        self.assertEqual(result["verdict"], VERDICT_PASS)
        self.assertIn("available reviewer quorum", result["reason"])

    def test_one_available_reviewer_is_not_a_quorum(self) -> None:
        result = compare_three_reviews(
            {"verdict": "approve", "confidence": 0.9},
            {"verdict": "unavailable", "confidence": 0.0, "error": "provider unavailable"},
            {"verdict": "unavailable", "confidence": 0.0, "error": "provider unavailable"},
        )
        self.assertEqual(result["verdict"], VERDICT_DISAGREEMENT)

    def test_parse_error_is_not_dropped_from_quorum(self) -> None:
        result = compare_three_reviews(
            {"verdict": "approve", "confidence": 0.9},
            {"verdict": "invalid", "confidence": 0.0, "parse_error": "empty_output"},
            {"verdict": "approve", "confidence": 0.9},
        )
        self.assertEqual(result["verdict"], VERDICT_DISAGREEMENT)


class TestDualReviewTrigger(unittest.TestCase):
    def test_trigger_values(self) -> None:
        self.assertEqual(DualReviewTrigger.PROMOTION.value, "promotion")
        self.assertEqual(DualReviewTrigger.HIT_RATE.value, "hit_rate")
        self.assertEqual(DualReviewTrigger.DRIFT.value, "drift")

    def test_trigger_is_string_enum(self) -> None:
        self.assertIsInstance(DualReviewTrigger.PROMOTION, str)
        self.assertEqual(DualReviewTrigger("drift"), DualReviewTrigger.DRIFT)


if __name__ == "__main__":
    unittest.main()
