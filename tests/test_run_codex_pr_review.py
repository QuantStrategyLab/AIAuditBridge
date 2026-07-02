from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from scripts.run_codex_pr_review import ReviewError, run_codex_review_with_fallback


class RunCodexPrReviewTests(unittest.TestCase):
    def test_service_failure_falls_back_to_direct_api(self) -> None:
        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://service.example"}, clear=True),
            patch(
                "scripts.run_codex_pr_review.run_codex_service_review",
                side_effect=ReviewError("HTTP 429 Too Many Requests"),
            ),
            patch("scripts.run_codex_pr_review.run_direct_api_review", return_value="api review") as direct_api,
        ):
            output = run_codex_review_with_fallback(
                "Review this PR.",
                timeout_minutes=20,
                complexity="high",
                changed_file_count=3,
                changed_line_count=120,
            )

        self.assertEqual(output, "api review")
        direct_api.assert_called_once_with("Review this PR.", complexity="high")
