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


class LlmAdapterUsageTests(unittest.TestCase):
    def complete(self, payload, model="gpt-5.4-mini"):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only", "ANTHROPIC_API_KEY": "test-only"}, clear=True), patch(
            "service.adapters.llm_adapter.urllib.request.urlopen", return_value=_FakeResponse(payload),
        ):
            return LlmAdapter().complete(model=model, user="synthetic")

    def test_openai_reports_usage_even_when_content_parsing_fails(self) -> None:
        result = self.complete({"model": "gpt-5.4-mini", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        self.assertFalse(result.success)
        self.assertEqual((result.tokens_input, result.tokens_output, result.usage_complete), (10, 2, True))

    def test_anthropic_reports_cache_input_and_output(self) -> None:
        result = self.complete({"content": [{"type": "text", "text": "ok"}], "usage": {
            "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5,
        }}, model="claude-sonnet-4-6")
        self.assertTrue(result.success)
        self.assertEqual((result.tokens_input, result.tokens_output, result.usage_complete), (35, 0, True))

    def test_absent_or_invalid_usage_is_not_zero(self) -> None:
        for usage in (None, {}, {"prompt_tokens": True, "completion_tokens": -1}):
            with self.subTest(usage=usage):
                result = self.complete({"choices": [{"message": {"content": "ok"}}], "usage": usage})
                self.assertIsNone(result.tokens_input)
                self.assertIsNone(result.tokens_output)
                self.assertFalse(result.usage_complete)

    def test_timeout_usage_is_unknown(self) -> None:
        with patch("service.adapters.llm_adapter._openai_completion", side_effect=LlmAdapterError("synthetic timeout")):
            result = LlmAdapter().complete(model="gpt-5.4-mini")
        self.assertFalse(result.success)
        self.assertIsNone(result.tokens_input)
        self.assertFalse(result.usage_complete)

    def test_retry_accumulates_reported_usage_and_preserves_unknown_attempt(self) -> None:
        import urllib.error
        from service.adapters.llm_adapter import ProviderCompletion, _retry_with_backoff
        error = urllib.error.HTTPError("https://synthetic.invalid", 503, "synthetic", {}, None)
        error.tokens_input, error.tokens_output, error.usage_complete = 10, 2, True
        final = ProviderCompletion(provider="openai", model="gpt-5.4-mini", output="ok", tokens_input=20, tokens_output=3, usage_complete=True)
        with patch("service.adapters.llm_adapter.time.sleep"), patch("service.adapters.llm_adapter._logger"), patch(
            "service.adapters.llm_adapter._openai_completion", return_value=final,
        ) as call:
            call.side_effect = [error, final]
            result = _retry_with_backoff(call)
        self.assertEqual((result.tokens_input, result.tokens_output, result.usage_complete), (30, 5, True))
        unknown = urllib.error.HTTPError("https://synthetic.invalid", 503, "synthetic", {}, None)
        with patch("service.adapters.llm_adapter.time.sleep"), patch("service.adapters.llm_adapter._logger"), patch(
            "service.adapters.llm_adapter._openai_completion", side_effect=[unknown, final],
        ) as call:
            result = _retry_with_backoff(call)
        self.assertEqual((result.tokens_input, result.tokens_output), (20, 3))
        self.assertFalse(result.usage_complete)

    def test_http_error_keeps_returned_usage_without_enabling_extra_retries(self) -> None:
        import io
        import urllib.error
        for model, key, usage in (
            ("gpt-5.4-mini", "OPENAI_API_KEY", {"prompt_tokens": 7, "completion_tokens": 1}),
            ("claude-sonnet-4-6", "ANTHROPIC_API_KEY", {"input_tokens": 7, "output_tokens": 1}),
        ):
            error = urllib.error.HTTPError("https://synthetic.invalid", 400, "synthetic", {},
                                           io.BytesIO(json.dumps({"usage": usage}).encode()))
            with self.subTest(model=model), patch.dict(os.environ, {key: "test-only"}, clear=True), patch(
                "service.adapters.llm_adapter.urllib.request.urlopen", side_effect=error,
            ) as call:
                result = LlmAdapter().complete(model=model)
            self.assertEqual(call.call_count, 1)
            self.assertFalse(result.success)
            self.assertEqual((result.tokens_input, result.tokens_output, result.usage_complete), (7, 1, True))

    def test_terminal_retry_failure_keeps_known_partial_totals(self) -> None:
        import urllib.error
        from service.adapters.llm_adapter import _retry_with_backoff
        error = urllib.error.HTTPError("https://synthetic.invalid", 503, "synthetic", {}, None)
        error.tokens_input, error.tokens_output, error.usage_complete = 7, None, False
        with patch("service.adapters.llm_adapter.time.sleep"), patch("service.adapters.llm_adapter._logger"), patch(
            "service.adapters.llm_adapter._openai_completion", side_effect=error,
        ) as call, self.assertRaises(LlmAdapterError) as raised:
            _retry_with_backoff(call, max_retries=1)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(raised.exception.tokens_input, 14)
        self.assertIsNone(raised.exception.tokens_output)
        self.assertFalse(raised.exception.usage_complete)


if __name__ == "__main__":
    unittest.main()
