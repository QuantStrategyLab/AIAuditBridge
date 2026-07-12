from __future__ import annotations

import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from service.adapters.llm_adapter import LlmAdapter, LlmAdapterError, _retry_with_backoff


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


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
        self.assertFalse(result.dispatch_uncertain)

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
        self.assertFalse(results[0].dispatch_uncertain)

    def test_provider_rejection_is_not_ambiguous_dispatch(self) -> None:
        with patch(
            "service.adapters.llm_adapter._openai_completion",
            side_effect=LlmAdapterError("OpenAI HTTP 401: invalid API key", dispatch_started=True),
        ):
            result = LlmAdapter().complete(model="gpt-5.4-mini", user="review")

        self.assertTrue(result.dispatch_started)
        self.assertFalse(result.dispatch_uncertain)

    def test_provider_response_parse_failure_is_confirmed_dispatch(self) -> None:
        with (
            patch.dict("service.adapters.llm_adapter.os.environ", {"OPENAI_API_KEY": "test-key"}, clear=True),
            patch("service.adapters.llm_adapter.urllib.request.urlopen", return_value=_Response(b"not-json")),
        ):
            result = LlmAdapter().complete(model="gpt-5.4-mini", user="review")

        self.assertFalse(result.success)
        self.assertTrue(result.dispatch_started)
        self.assertFalse(result.dispatch_uncertain)

    def test_retry_does_not_downgrade_prior_provider_dispatch(self) -> None:
        attempts = iter((HTTPError("https://provider.test", 500, "error", {}, None), LlmAdapterError("local failure")))
        with self.assertRaises(LlmAdapterError) as raised:
            _retry_with_backoff(lambda: (_ for _ in ()).throw(next(attempts)), max_retries=1, base_seconds=0)

        self.assertTrue(raised.exception.dispatch_started)
        self.assertFalse(raised.exception.dispatch_uncertain)

    def test_retry_uses_wrapped_provider_status(self) -> None:
        attempts = iter((
            LlmAdapterError("OpenAI HTTP 429", dispatch_started=True, status_code=429),
            LlmAdapterError("local failure"),
        ))
        with self.assertRaises(LlmAdapterError) as raised:
            _retry_with_backoff(lambda: (_ for _ in ()).throw(next(attempts)), max_retries=1, base_seconds=0)

        self.assertTrue(raised.exception.dispatch_started)
        self.assertFalse(raised.exception.dispatch_uncertain)

    def test_malformed_provider_choices_is_confirmed_dispatch_failure(self) -> None:
        with (
            patch.dict("service.adapters.llm_adapter.os.environ", {"OPENAI_API_KEY": "test-key"}, clear=True),
            patch("service.adapters.llm_adapter.urllib.request.urlopen", return_value=_Response(b'{"choices":[null]}')),
        ):
            result = LlmAdapter().complete(model="gpt-5.4-mini", user="review")

        self.assertFalse(result.success)
        self.assertTrue(result.dispatch_started)
        self.assertFalse(result.dispatch_uncertain)


if __name__ == "__main__":
    unittest.main()
