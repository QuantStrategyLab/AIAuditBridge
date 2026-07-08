from __future__ import annotations

import unittest

from service.dual_review import VERDICT_DISAGREEMENT, VERDICT_FAIL, VERDICT_PASS, DualReviewTrigger
from service.dual_review_orchestrator import (
    DualReviewRequest,
    build_request_from_payload,
    orchestrate_dual_review,
    orchestrate_from_payload,
)


class DualReviewOrchestratorTests(unittest.TestCase):
    def test_primary_high_confidence_pass_skips_secondary(self) -> None:
        request = DualReviewRequest(
            trigger=DualReviewTrigger.PROMOTION,
            strategy_profile="demo",
            primary_review={"verdict": "approve", "confidence": 0.91},
        )
        result = orchestrate_dual_review(request)
        self.assertFalse(result.escalated)
        self.assertEqual(result.outcome, VERDICT_PASS)
        self.assertIsNone(result.secondary_review)

    def test_primary_low_confidence_escalates_and_compares(self) -> None:
        request = DualReviewRequest(
            trigger=DualReviewTrigger.DRIFT,
            strategy_profile="demo",
            primary_review={"verdict": "approve", "confidence": 0.55},
        )

        def secondary(_req: DualReviewRequest) -> dict:
            return {"verdict": "reject", "confidence": 0.88}

        result = orchestrate_dual_review(request, secondary_reviewer=secondary)
        self.assertTrue(result.escalated)
        self.assertEqual(result.outcome, VERDICT_DISAGREEMENT)

    def test_agreeing_secondary_passes(self) -> None:
        request = DualReviewRequest(
            trigger=DualReviewTrigger.HIT_RATE,
            strategy_profile="demo",
            primary_review={"verdict": "approve", "confidence": 0.5},
        )

        def secondary(_req: DualReviewRequest) -> dict:
            return {"verdict": "pass", "confidence": 0.82}

        result = orchestrate_dual_review(request, secondary_reviewer=secondary)
        self.assertEqual(result.outcome, VERDICT_PASS)

    def test_build_request_from_payload(self) -> None:
        request = build_request_from_payload(
            {
                "trigger": "promotion",
                "strategy_profile": "cn_demo",
                "primary_review": {"verdict": "approve", "confidence": 0.7},
            }
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.strategy_profile, "cn_demo")

    def test_orchestrate_from_payload_with_fixed_secondary(self) -> None:
        result = orchestrate_from_payload(
            {
                "trigger": "drift",
                "strategy_profile": "cn_demo",
                "drift_score": 0.9,
                "primary_review": {"verdict": "approve", "confidence": 0.4},
            },
            secondary_review={
                "gpt": {"verdict": "reject", "confidence": 0.9},
                "claude": {"verdict": "reject", "confidence": 0.88},
            },
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.outcome, VERDICT_DISAGREEMENT)
        self.assertEqual(result.comparison.get("mode"), "dual_api")

    def test_three_way_unanimous_pass(self) -> None:
        result = orchestrate_from_payload(
            {
                "trigger": "promotion",
                "strategy_profile": "cn_demo",
                "old_status": "shadow_candidate",
                "new_status": "live_candidate",
                "primary_review": {"verdict": "approve", "confidence": 0.55},
            },
            secondary_review={
                "gpt": {"verdict": "pass", "confidence": 0.9},
                "claude": {"verdict": "approve", "confidence": 0.88},
            },
        )
        assert result is not None
        self.assertEqual(result.outcome, VERDICT_PASS)

    def test_primary_high_confidence_fail(self) -> None:
        request = DualReviewRequest(
            trigger=DualReviewTrigger.PROMOTION,
            strategy_profile="demo",
            primary_review={"verdict": "reject", "confidence": 0.95},
        )
        result = orchestrate_dual_review(request)
        self.assertEqual(result.outcome, VERDICT_FAIL)
        self.assertFalse(result.escalated)


if __name__ == "__main__":
    unittest.main()
