from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from service.adapters.llm_adapter import LlmAdapter, LlmAdapterError


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class LlmAdapterIdentityTests(unittest.TestCase):
    def test_openai_result_carries_actual_model_from_provider_response(self) -> None:
        response = _FakeResponse({
            "model": "gpt-5.4-mini-2026-08-01",
            "choices": [{"message": {"content": "review output"}}],
        })
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}), patch(
            "service.adapters.llm_adapter.urllib.request.urlopen",
            return_value=response,
        ):
            result = LlmAdapter().complete(model="gpt-5.4-mini", user="review")

        self.assertTrue(result.success)
        self.assertEqual(result.model, "gpt-5.4-mini")
        self.assertEqual(result.actual_provider, "openai")
        self.assertEqual(result.actual_model, "gpt-5.4-mini-2026-08-01")

    def test_anthropic_result_carries_actual_model_from_provider_response(self) -> None:
        response = _FakeResponse({
            "model": "claude-sonnet-4-6-20260801",
            "content": [{"type": "text", "text": "review output"}],
        })
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-only"}), patch(
            "service.adapters.llm_adapter.urllib.request.urlopen",
            return_value=response,
        ):
            result = LlmAdapter().complete(model="claude-sonnet-4-6", user="review")

        self.assertTrue(result.success)
        self.assertEqual(result.model, "claude-sonnet-4-6")
        self.assertEqual(result.actual_provider, "anthropic")
        self.assertEqual(result.actual_model, "claude-sonnet-4-6-20260801")


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
