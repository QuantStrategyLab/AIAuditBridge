from __future__ import annotations

import unittest
from unittest.mock import patch

from service.dual_review import VERDICT_INVALID, VERDICT_UNAVAILABLE
from service.dual_review_primary import build_primary_prompt, parse_primary_review_output, run_codex_primary_review


class DualReviewPrimaryTests(unittest.TestCase):
    def test_build_primary_prompt_includes_evidence_summary(self) -> None:
        from pathlib import Path
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.json"
            path.write_text(
                json.dumps({"strategy_profile": "demo", "oos_sharpe": 1.2, "status": "shadow_candidate"}),
                encoding="utf-8",
            )
            prompt = build_primary_prompt(
                trigger="promotion",
                strategy_profile="demo",
                context={"old_status": "shadow_candidate", "new_status": "live_candidate"},
                evidence_path=path,
            )
            self.assertIn("demo", prompt)
            self.assertIn("oos_sharpe", prompt)

    def test_parse_primary_review_output(self) -> None:
        review = parse_primary_review_output('{"verdict":"approve","confidence":0.77,"summary":"ok"}')
        self.assertEqual(review["verdict"], "approve")
        self.assertEqual(review["source"], "codex_primary")

    @patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_URL": "https://service.invalid"})
    @patch("scripts.run_codex_pr_review.run_codex_service_review")
    def test_budget_error_is_unavailable(self, review) -> None:
        from scripts.run_codex_pr_review import ReviewError

        review.side_effect = ReviewError("Daily budget exceeded")
        result = run_codex_primary_review(prompt="review")
        self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)

    @patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_URL": "https://service.invalid"})
    @patch("scripts.run_codex_pr_review.run_codex_service_review")
    def test_protocol_error_is_invalid(self, review) -> None:
        from scripts.run_codex_pr_review import ReviewError

        review.side_effect = ReviewError("response did not contain review JSON")
        result = run_codex_primary_review(prompt="review")
        self.assertEqual(result["verdict"], VERDICT_INVALID)


if __name__ == "__main__":
    unittest.main()
