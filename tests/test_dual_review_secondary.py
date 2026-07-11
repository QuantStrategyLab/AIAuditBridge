from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from service.adapters.llm_adapter import LlmResult
from service.dual_review import VERDICT_INVALID, VERDICT_UNAVAILABLE, DualReviewTrigger
from service.dual_review_orchestrator import DualReviewRequest
from service.dual_review_secondary import (
    build_secondary_prompt,
    parse_llm_review_output,
    run_dual_api_secondary_review,
    secondary_mode,
)


class DualReviewSecondaryTests(unittest.TestCase):
    def test_parse_llm_review_output(self) -> None:
        review = parse_llm_review_output(
            'prefix {"verdict":"approve","confidence":0.87,"summary":"ok"} suffix',
            provider="openai",
            model="gpt-5.4-mini",
        )
        self.assertEqual(review["verdict"], "approve")
        self.assertEqual(review["confidence"], 0.87)

    def test_empty_provider_response_is_invalid_not_unavailable(self) -> None:
        review = parse_llm_review_output("", provider="openai", model="gpt")
        self.assertEqual(review["verdict"], VERDICT_INVALID)
        self.assertEqual(review["parse_error"], "empty_output")

    def test_build_secondary_prompt_excludes_primary_verdict(self) -> None:
        request = DualReviewRequest(
            trigger=DualReviewTrigger.PROMOTION,
            strategy_profile="cn_demo",
            primary_review={"verdict": "approve", "confidence": 0.4},
            context={"old_status": "shadow_candidate", "new_status": "live_candidate"},
        )
        prompt = build_secondary_prompt(request)
        self.assertIn("cn_demo", prompt)
        self.assertNotIn("primary_review", prompt)

    @patch.dict(os.environ, {"DUAL_REVIEW_SECONDARY_MODE": "dual_api"})
    def test_secondary_mode_default(self) -> None:
        self.assertEqual(secondary_mode(), "dual_api")

    def test_run_dual_api_secondary_review_mocked(self) -> None:
        class _FakeAdapter:
            def parallel_review(self, **kwargs):
                return [
                    LlmResult(
                        provider="openai",
                        model="gpt-5.4-mini",
                        output='{"verdict":"approve","confidence":0.9,"summary":"gpt ok"}',
                    ),
                    LlmResult(
                        provider="anthropic",
                        model="claude-sonnet-4-6",
                        output='{"verdict":"approve","confidence":0.88,"summary":"claude ok"}',
                    ),
                ]

        request = DualReviewRequest(
            trigger=DualReviewTrigger.DRIFT,
            strategy_profile="demo",
            primary_review={"verdict": "approve", "confidence": 0.5},
            context={"drift_score": 0.9},
        )
        payload = run_dual_api_secondary_review(request, adapter=_FakeAdapter())
        self.assertEqual(payload["mode"], "dual_api")
        self.assertEqual(payload["gpt"]["verdict"], "approve")
        self.assertEqual(payload["claude"]["verdict"], "approve")

    def test_failed_providers_are_unavailable_not_rejections(self) -> None:
        class _UnavailableAdapter:
            def parallel_review(self, **kwargs):
                return [
                    LlmResult(provider="openai", model="gpt", output="", success=False, error="missing key"),
                    LlmResult(provider="anthropic", model="claude", output="", success=False, error="missing key"),
                ]

        request = DualReviewRequest(
            trigger=DualReviewTrigger.DRIFT,
            strategy_profile="demo",
            primary_review={"verdict": VERDICT_UNAVAILABLE, "confidence": 0.0},
        )
        payload = run_dual_api_secondary_review(request, adapter=_UnavailableAdapter())
        self.assertEqual(payload["gpt"]["verdict"], VERDICT_UNAVAILABLE)
        self.assertEqual(payload["claude"]["verdict"], VERDICT_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
