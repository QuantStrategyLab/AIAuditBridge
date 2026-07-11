"""Tests for Anthropic Admin Usage/Cost snapshot reader."""

from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from service.anthropic_admin_usage import _usage_window, read_anthropic_admin_usage


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class TestAnthropicAdminUsage(unittest.TestCase):
    def test_default_window_uses_configured_billing_timezone(self) -> None:
        end_time = int(datetime(2026, 7, 31, 18, tzinfo=UTC).timestamp())
        start_time, days = _usage_window(end_time, "Asia/Shanghai")

        self.assertEqual(start_time, end_time - 2 * 3600)
        self.assertEqual(days, 1)

    def test_disabled_without_admin_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(read_anthropic_admin_usage(now=1783139561, timeout_seconds=1))

    def test_rejects_insecure_admin_base_url(self) -> None:
        env = {
            "ANTHROPIC_ADMIN_KEY": "admin-key",
            "CODEX_AUDIT_SERVICE_ANTHROPIC_ADMIN_BASE_URL": "http://127.0.0.1:9999",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(read_anthropic_admin_usage(now=1783139561, timeout_seconds=1))

    def test_reads_sanitized_usage_and_costs(self) -> None:
        requests = []

        def fake_urlopen(request, timeout: float):  # type: ignore[no-untyped-def]
            requests.append((request, timeout))
            self.assertEqual(request.get_header("X-api-key"), "admin-key")
            query = parse_qs(urlparse(request.full_url).query)
            if "/organizations/usage_report/messages" in request.full_url:
                value = 50 if query.get("page") == ["u2"] else 100
                return _FakeResponse({"has_more": "page" not in query, "next_page": "u2", "data": [{"results": [{"uncached_input_tokens": value, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30, "output_tokens": 25, "num_model_requests": 3}]}]})
            if "/organizations/cost_report" in request.full_url:
                value = "100.00" if query.get("page") == ["c2"] else "123.45"
                return _FakeResponse({"has_more": "page" not in query, "next_page": "c2", "data": [{"results": [{"amount": value}]}]})
            raise AssertionError(request.full_url)

        env = {
            "ANTHROPIC_ADMIN_KEY": "admin-key",
            "CODEX_AUDIT_SERVICE_ANTHROPIC_USAGE_WINDOW_DAYS": "7",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("service.anthropic_admin_usage.urlopen", side_effect=fake_urlopen):
                snapshot = read_anthropic_admin_usage(now=1783139561, timeout_seconds=2)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["source"], "anthropic_admin_api")
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(snapshot["window_days"], 7)
        self.assertEqual(snapshot["messages"]["input_tokens"], 250)
        self.assertEqual(snapshot["messages"]["output_tokens"], 50)
        self.assertEqual(snapshot["messages"]["total_tokens"], 300)
        self.assertEqual(snapshot["messages"]["num_model_requests"], 6)
        self.assertEqual(snapshot["costs"]["total_cost"], 223.45)
        self.assertEqual(snapshot["costs"]["currency"], "usd")
        self.assertEqual(len(requests), 4)
        self.assertNotIn("admin-key", json.dumps(snapshot))

    def test_api_key_filter_applies_to_usage_and_skips_unfiltered_costs(self) -> None:
        requests = []

        def fake_urlopen(request, timeout: float):  # type: ignore[no-untyped-def]
            requests.append((request, timeout))
            parsed = urlparse(request.full_url)
            query = parse_qs(parsed.query)
            self.assertEqual(parsed.path, "/v1/organizations/usage_report/messages")
            self.assertEqual(query["api_key_ids[]"], ["key_a", "key_b"])
            return _FakeResponse({
                "data": [{
                    "results": [{
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "request_count": 3,
                    }],
                }],
            })

        env = {
            "ANTHROPIC_ADMIN_KEY": "admin-key",
            "CODEX_AUDIT_SERVICE_ANTHROPIC_ADMIN_API_KEY_IDS": "key_a,key_b",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("service.anthropic_admin_usage.urlopen", side_effect=fake_urlopen):
                snapshot = read_anthropic_admin_usage(now=1783139561, timeout_seconds=2)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["filtered_api_key_count"], 2)
        self.assertEqual(snapshot["messages"]["num_model_requests"], 3)
        self.assertIsNone(snapshot["costs"])
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
