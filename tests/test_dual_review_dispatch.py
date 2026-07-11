from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from service.dual_review import DualReviewTrigger, VERDICT_DISAGREEMENT, VERDICT_UNAVAILABLE
from service.dual_review_dispatch import dispatch_dual_review_result
from service.dual_review_orchestrator import DualReviewResult


class DualReviewDispatchTests(unittest.TestCase):
    def test_dispatch_skips_when_agreement(self) -> None:
        result = DualReviewResult(
            trigger=DualReviewTrigger.PROMOTION,
            strategy_profile="demo",
            primary_review={"verdict": "approve", "confidence": 0.95},
            outcome="pass",
        )
        summary = dispatch_dual_review_result(result)
        self.assertIn("no_disagreement", summary["skipped"])

    def test_dispatch_disagreement_dry_run(self) -> None:
        result = DualReviewResult(
            trigger=DualReviewTrigger.DRIFT,
            strategy_profile="demo",
            primary_review={"verdict": "approve", "confidence": 0.4},
            secondary_review={"verdict": "reject", "confidence": 0.9},
            escalated=True,
            outcome=VERDICT_DISAGREEMENT,
            reason="reviews disagree",
        )
        with patch.dict(os.environ, {"DUAL_REVIEW_GITHUB_ASSIGNEE": "operator"}):
            summary = dispatch_dual_review_result(result, dry_run=True)
        self.assertIn("github_dry_run", summary)
        self.assertIn("operator", summary["github_dry_run"]["body"])

    def test_dispatch_unavailable_creates_durable_alert(self) -> None:
        result = DualReviewResult(
            trigger=DualReviewTrigger.DRIFT,
            strategy_profile="demo",
            primary_review={"verdict": VERDICT_UNAVAILABLE, "confidence": 0.0},
            outcome=VERDICT_UNAVAILABLE,
            reason="all configured reviewers are unavailable",
        )
        summary = dispatch_dual_review_result(result, dry_run=True)
        self.assertIn("github_dry_run", summary)
        self.assertEqual(summary["github_dry_run"]["labels"], [])


if __name__ == "__main__":
    unittest.main()
