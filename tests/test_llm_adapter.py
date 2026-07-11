from __future__ import annotations

import unittest
from unittest.mock import patch

from service.adapters.llm_adapter import LlmAdapter, LlmAdapterError


class LlmAdapterFailureTests(unittest.TestCase):
    def test_complete_returns_empty_output_on_provider_failure(self) -> None:
        with patch(
            "service.adapters.llm_adapter._openai_completion",
            side_effect=LlmAdapterError("provider unavailable"),
        ):
            result = LlmAdapter().complete(model="gpt-5.4-mini", user="review")

        self.assertFalse(result.success)
        self.assertEqual(result.output, "")
        self.assertEqual(result.error, "provider unavailable")

    def test_parallel_review_returns_empty_output_on_worker_failure(self) -> None:
        with patch.object(LlmAdapter, "complete", side_effect=RuntimeError("worker failed")):
            results = LlmAdapter().parallel_review(
                reviewers=[("gpt", "gpt-5.4-mini")],
                user="review",
            )

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].success)
        self.assertEqual(results[0].output, "")
        self.assertEqual(results[0].error, "worker failed")


if __name__ == "__main__":
    unittest.main()
