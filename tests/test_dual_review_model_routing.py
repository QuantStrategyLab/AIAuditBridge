from __future__ import annotations

import unittest
from unittest.mock import patch

from service.adapters.llm_adapter import LlmResult
from service.dual_review import DualReviewTrigger
from service.dual_review_orchestrator import DualReviewRequest
from service.dual_review_secondary import run_dual_api_secondary_review


class DualReviewModelRoutingTests(unittest.TestCase):
    def test_secondary_review_uses_route_model_for_gpt_default(self) -> None:
        captured: dict[str, object] = {}

        class _FakeAdapter:
            def parallel_review(self, **kwargs):
                captured["reviewers"] = kwargs["reviewers"]
                return [
                    LlmResult(provider="openai", model="gpt-5.6-sol", output='{"verdict":"approve","confidence":0.9,"summary":"gpt"}'),
                    LlmResult(provider="anthropic", model="claude-sonnet-4-6", output='{"verdict":"approve","confidence":0.8,"summary":"claude"}'),
                ]

        request = DualReviewRequest(
            trigger=DualReviewTrigger.DRIFT,
            strategy_profile="demo",
            primary_review={"verdict": "approve", "confidence": 0.5},
            context={"drift_score": 0.9},
        )
        with patch("service.dual_review_secondary.route_model", return_value={"model": "gpt-5.6-sol"}):
            run_dual_api_secondary_review(request, adapter=_FakeAdapter())

        self.assertEqual(captured["reviewers"], [("gpt", "gpt-5.6-sol"), ("claude", "claude-sonnet-4-6")])

    def test_gateway_default_model_helper_prefers_route_model_when_provider_matches(self) -> None:
        from service.dual_review_gateway import _default_model_for_reviewer

        with patch("service.dual_review_gateway.route_model", return_value={"model": "gpt-5.6-sol"}):
            self.assertEqual(_default_model_for_reviewer("gpt"), "gpt-5.6-sol")
            self.assertEqual(_default_model_for_reviewer("claude"), "claude-sonnet-4-6")

    def test_ai_gateway_default_model_helper_prefers_route_model_when_provider_matches(self) -> None:
        from service.ai_gateway_service import _default_model_for_reviewer

        with patch("service.ai_gateway_service.route_model", return_value={"model": "gpt-5.6-sol"}):
            self.assertEqual(_default_model_for_reviewer("gpt"), "gpt-5.6-sol")
            self.assertEqual(_default_model_for_reviewer("claude"), "claude-sonnet-4-6")


if __name__ == "__main__":
    unittest.main()
