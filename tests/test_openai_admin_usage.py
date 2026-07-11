"""Tests for OpenAI Admin Usage snapshot reader."""

from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from service.openai_admin_usage import _usage_window, read_openai_admin_usage


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class TestOpenAIAdminUsage(unittest.TestCase):
    def test_default_window_uses_configured_billing_timezone(self) -> None:
        end_time = int(datetime(2026, 7, 31, 18, tzinfo=UTC).timestamp())
        start_time, days = _usage_window(end_time, "Asia/Shanghai")

        self.assertEqual(start_time, end_time - 2 * 3600)
        self.assertEqual(days, 1)

    def test_disabled_without_admin_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(read_openai_admin_usage(now=1783139561, timeout_seconds=1))

    def test_rejects_insecure_admin_base_url(self) -> None:
        env = {
            "OPENAI_ADMIN_KEY": "admin-key",
            "CODEX_AUDIT_SERVICE_OPENAI_ADMIN_BASE_URL": "http://127.0.0.1:9999",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(read_openai_admin_usage(now=1783139561, timeout_seconds=1))

    def test_reads_sanitized_completions_usage_and_separate_org_costs(self) -> None:
        requests = []

        def fake_urlopen(request, timeout: float):  # type: ignore[no-untyped-def]
            requests.append((request, timeout))
            self.assertEqual(request.get_header("Authorization"), "Bearer admin-key")
            body = {
                "/organization/usage/completions": {
                    "data": [{
                        "results": [{
                            "input_tokens": 100,
                            "output_tokens": 25,
                            "input_cached_tokens": 10,
                            "input_audio_tokens": 5,
                            "output_audio_tokens": 2,
                            "num_model_requests": 3,
                        }],
                    }],
                },
                "/organization/costs": {
                    "data": [{
                        "results": [{
                            "amount": {"value": 1.23, "currency": "usd"},
                        }],
                    }],
                },
            }
            for path, payload in body.items():
                if path in request.full_url:
                    return _FakeResponse(payload)
            raise AssertionError(request.full_url)

        env = {
            "OPENAI_ADMIN_KEY": "admin-key",
            "CODEX_AUDIT_SERVICE_OPENAI_USAGE_WINDOW_DAYS": "7",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("service.openai_admin_usage.urlopen", side_effect=fake_urlopen):
                snapshot = read_openai_admin_usage(now=1783139561, timeout_seconds=2)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["source"], "openai_admin_api")
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(snapshot["usage_surface"], "completions")
        self.assertEqual(snapshot["window_days"], 7)
        self.assertEqual(snapshot["filtered_api_key_count"], 0)
        self.assertEqual(snapshot["completions"]["input_tokens"], 100)
        self.assertEqual(snapshot["completions"]["output_tokens"], 25)
        self.assertEqual(snapshot["completions"]["input_audio_tokens"], 5)
        self.assertEqual(snapshot["completions"]["output_audio_tokens"], 2)
        self.assertNotIn("total_tokens", snapshot["completions"])
        self.assertEqual(snapshot["completions"]["num_model_requests"], 3)
        self.assertEqual(snapshot["organization_costs"]["total_cost"], 1.23)
        self.assertEqual(snapshot["organization_costs"]["currency"], "usd")
        self.assertEqual(snapshot["organization_costs"]["scope"], "organization")
        self.assertEqual(len(requests), 2)
        self.assertNotIn("admin-key", json.dumps(snapshot))

    def test_api_key_filter_applies_to_usage_and_separate_org_costs(self) -> None:
        requests = []

        def fake_urlopen(request, timeout: float):  # type: ignore[no-untyped-def]
            requests.append((request, timeout))
            if "/organization/usage/completions" in request.full_url:
                self.assertIn("api_key_ids=key_a", request.full_url)
                return _FakeResponse({
                    "data": [{
                        "results": [{
                            "input_tokens": 100,
                            "output_tokens": 25,
                            "num_model_requests": 3,
                        }],
                    }],
                })
            if "/organization/costs" in request.full_url:
                self.assertNotIn("api_key_ids=", request.full_url)
                return _FakeResponse({
                    "data": [{
                        "results": [{
                            "amount": {"value": 0.42, "currency": "usd"},
                        }],
                    }],
                })
            raise AssertionError(request.full_url)

        env = {
            "OPENAI_ADMIN_KEY": "admin-key",
            "CODEX_AUDIT_SERVICE_OPENAI_ADMIN_API_KEY_IDS": "key_a,key_b",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("service.openai_admin_usage.urlopen", side_effect=fake_urlopen):
                snapshot = read_openai_admin_usage(now=1783139561, timeout_seconds=2)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["filtered_api_key_count"], 2)
        self.assertEqual(snapshot["completions"]["num_model_requests"], 3)
        self.assertEqual(snapshot["organization_costs"]["total_cost"], 0.42)
        self.assertEqual(snapshot["organization_costs"]["scope"], "organization")
        self.assertEqual(len(requests), 2)

    def test_reads_paginated_usage_and_costs(self) -> None:
        requests = []
        usage_page = 0
        cost_page = 0

        def fake_urlopen(request, timeout: float):  # type: ignore[no-untyped-def]
            nonlocal usage_page, cost_page
            requests.append(request.full_url)
            if "/organization/usage/completions" in request.full_url:
                usage_page += 1
                if usage_page == 1:
                    self.assertNotIn("page=", request.full_url)
                    return _FakeResponse({
                        "has_more": True,
                        "next_page": "usage_page_2",
                        "data": [{
                            "results": [{
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "num_model_requests": 1,
                            }],
                        }],
                    })
                self.assertIn("page=usage_page_2", request.full_url)
                return _FakeResponse({
                    "has_more": False,
                    "data": [{
                        "results": [{
                            "input_tokens": 20,
                            "output_tokens": 7,
                            "num_model_requests": 2,
                        }],
                    }],
                })
            if "/organization/costs" in request.full_url:
                cost_page += 1
                if cost_page == 1:
                    self.assertNotIn("page=", request.full_url)
                    return _FakeResponse({
                        "has_more": True,
                        "next_page": "cost_page_2",
                        "data": [{
                            "results": [{
                                "amount": {"value": 0.4, "currency": "usd"},
                            }],
                        }],
                    })
                self.assertIn("page=cost_page_2", request.full_url)
                return _FakeResponse({
                    "has_more": False,
                    "data": [{
                        "results": [{
                            "amount": {"value": 0.6, "currency": "usd"},
                        }],
                    }],
                })
            raise AssertionError(request.full_url)

        with patch.dict(os.environ, {"OPENAI_ADMIN_KEY": "admin-key"}, clear=True):
            with patch("service.openai_admin_usage.urlopen", side_effect=fake_urlopen):
                snapshot = read_openai_admin_usage(now=1783139561, timeout_seconds=2)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["completions"]["input_tokens"], 30)
        self.assertEqual(snapshot["completions"]["output_tokens"], 12)
        self.assertNotIn("total_tokens", snapshot["completions"])
        self.assertEqual(snapshot["completions"]["num_model_requests"], 3)
        self.assertEqual(snapshot["organization_costs"]["total_cost"], 1.0)
        self.assertEqual(usage_page, 2)
        self.assertEqual(cost_page, 2)
        self.assertEqual(len(requests), 4)


if __name__ == "__main__":
    unittest.main()
