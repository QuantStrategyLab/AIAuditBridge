from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from service.dual_review import DualReviewTrigger, VERDICT_DISAGREEMENT
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


if __name__ == "__main__":
    unittest.main()
