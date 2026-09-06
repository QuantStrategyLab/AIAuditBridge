from __future__ import annotations

import json
import os
import unittest
from unittest.mock import Mock, patch

from scripts.run_dual_review_pipeline import _exit_code
from service.adapters.llm_adapter import LlmResult
from service.dual_review import (
    VERDICT_DISAGREEMENT,
    VERDICT_INVALID,
    VERDICT_PASS,
    VERDICT_UNAVAILABLE,
    DualReviewTrigger,
)
from service.dual_review_orchestrator import DualReviewRequest, orchestrate_dual_review
from service.dual_review_primary import parse_primary_review_output
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

    def test_invalid_confidence_blocks_primary_and_secondary_review_paths(self) -> None:
        invalid_values = {
            "nan": float("nan"), "infinity": float("inf"), "negative_infinity": -float("inf"),
            "above_one": 1.5, "negative": -0.2, "true": True, "false": False,
            "nan_string": "NaN", "infinity_string": "Infinity", "above_one_string": "1.5",
            "overflow_integer": 10**400, "null": None,
        }
        for case, confidence in invalid_values.items():
            for role in ("primary", "gpt", "claude"):
                with self.subTest(case=case, role=role):
                    output = json.dumps({"verdict": "approve", "confidence": confidence})
                    invalid = (
                        parse_primary_review_output(output) if role == "primary"
                        else parse_llm_review_output(output, provider=role, model="synthetic")
                    )
                    primary = invalid if role == "primary" else {"verdict": "approve", "confidence": 0.5}
                    secondary = {"mode": "dual_api", "gpt": {"verdict": "approve", "confidence": 0.9},
                                 "claude": {"verdict": "approve", "confidence": 0.9}}
                    if role != "primary":
                        secondary[role] = invalid
                    reviewer = Mock(return_value=secondary)
                    result = orchestrate_dual_review(
                        DualReviewRequest(trigger=DualReviewTrigger.DRIFT, strategy_profile="synthetic",
                                          primary_review=primary),
                        secondary_reviewer=reviewer,
                    )
                    self.assertEqual(result.outcome, VERDICT_DISAGREEMENT)
                    self.assertEqual(_exit_code({"ok": True, **result.to_dict()}), 2)
                    reviewer.assert_called_once()
                    self.assertEqual(invalid["verdict"], VERDICT_INVALID)
                    self.assertEqual(invalid["confidence"], 0.0)

    def test_valid_confidence_aliases_and_defaults_preserve_review_behavior(self) -> None:
        cases = [({"verdict": "approve", "confidence": value}, float(value))
                 for value in (0, 1, 0.87, "0", "1", "0.87")]
        cases.extend([
            ({"decision": "approve", "ai_confidence": "0.87", "reason": "synthetic"}, 0.87),
            ({"verdict": "approve"}, 0.5),
        ])
        for payload, expected in cases:
            with self.subTest(payload=payload):
                primary = parse_primary_review_output(json.dumps(payload))
                self.assertEqual(primary["verdict"], "approve")
                self.assertEqual(primary["confidence"], expected)
                reviewer = Mock(return_value={
                    "mode": "dual_api", "gpt": {"verdict": "approve", "confidence": 0.9},
                    "claude": {"verdict": "approve", "confidence": 0.9},
                })
                result = orchestrate_dual_review(
                    DualReviewRequest(trigger=DualReviewTrigger.DRIFT, strategy_profile="synthetic",
                                      primary_review=primary),
                    secondary_reviewer=reviewer,
                )
                self.assertEqual(result.outcome, VERDICT_PASS)
                self.assertEqual(_exit_code({"ok": True, **result.to_dict()}), 0)
                self.assertEqual(reviewer.call_count, int(expected < 0.8))

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
