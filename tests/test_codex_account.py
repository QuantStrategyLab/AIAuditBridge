"""Tests for Codex account rate-limit snapshot reader."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from service.codex_account import read_codex_rate_limits


class TestCodexAccountRateLimits(unittest.TestCase):
    def test_model_list_is_paginated_and_sanitized_without_generation(self) -> None:
        proc = Mock()
        proc.poll.return_value = 0
        replies = [
            {"result": {}},
            {"result": {"rateLimits": {"primary": {"usedPercent": 14}}}},
            {"result": {"data": [{"model": "gpt-5.6-sol", "private": "discard",
                "supportedReasoningEfforts": [{"reasoningEffort": "high", "description": "discard"}]}], "nextCursor": "page2"}},
            {"result": {"data": [{"model": "gpt-6-astra", "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}]}], "nextCursor": None}},
        ]
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE": "1"}, clear=True), patch(
            "service.codex_account.shutil.which", return_value="synthetic-codex"
        ), patch("service.codex_account.subprocess.Popen", return_value=proc), patch(
            "service.codex_account._read_response", side_effect=replies
        ), patch("service.codex_account._send") as send:
            snapshot = read_codex_rate_limits(timeout_seconds=1, include_models=True)
        self.assertEqual(snapshot["available_models"], [
            {"model": "gpt-5.6-sol", "supported_reasoning_efforts": ["high"]},
            {"model": "gpt-6-astra", "supported_reasoning_efforts": ["xhigh"]},
        ])
        messages = [call.args[1] for call in send.call_args_list]
        self.assertEqual([m["method"] for m in messages], [
            "initialize", "initialized", "account/rateLimits/read", "model/list", "model/list",
        ])
        self.assertEqual(messages[-1]["params"]["cursor"], "page2")
        self.assertNotIn("discard", repr(snapshot))

    def test_invalid_numeric_quota_is_never_normalized_to_available_capacity(self) -> None:
        from service.codex_account import _window

        for value in (True, float("inf"), float("nan"), "14", -0.1, 100.1):
            with self.subTest(value=value):
                self.assertIsNone(_window({"usedPercent": value})["used_percent"])

    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(read_codex_rate_limits(timeout_seconds=1))

    def test_invalid_timeout_env_fails_closed(self) -> None:
        env = {
            "CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE": "1",
            "CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_TIMEOUT_SECONDS": "invalid",
            "CODEX_AUDIT_SERVICE_CODEX_BIN": sys.executable,
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(read_codex_rate_limits())

    def test_reads_sanitized_rate_limits_from_app_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            fake_codex = bin_dir / "codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys

                    if (
                        os.environ.get("CODEX_AUDIT_SERVICE_TOKEN")
                        or os.environ.get("OPENAI_API_KEY")
                        or os.environ.get("OPENAI_ADMIN_KEY")
                        or os.environ.get("ANTHROPIC_ADMIN_KEY")
                    ):
                        sys.exit(7)

                    for line in sys.stdin:
                        message = json.loads(line)
                        if message.get("jsonrpc") != "2.0":
                            sys.exit(8)
                        method = message.get("method")
                        if method == "initialize":
                            print(json.dumps({"id": message["id"], "result": {"userAgent": "fake"}}), flush=True)
                        elif method == "account/rateLimits/read":
                            if message.get("params") is None:
                                sys.exit(9)
                            print(json.dumps({
                                "id": message["id"],
                                "result": {
                                    "rateLimits": {
                                        "limitId": "codex",
                                        "planType": "pro",
                                        "primary": {"usedPercent": 14, "windowDurationMins": 300, "resetsAt": 1783139561},
                                        "secondary": {"usedPercent": 27, "windowDurationMins": 10080, "resetsAt": 1783657152},
                                        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                                        "rateLimitReachedType": None,
                                    },
                                    "rateLimitsByLimitId": {},
                                },
                            }), flush=True)
                            break
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            env = {
                "CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE": "1",
                "CODEX_AUDIT_SERVICE_TOKEN": "service-token-must-not-reach-codex",
                "OPENAI_API_KEY": "openai-key-must-not-reach-codex",
                "OPENAI_ADMIN_KEY": "admin-key-must-not-reach-codex",
                "ANTHROPIC_ADMIN_KEY": "anthropic-admin-key-must-not-reach-codex",
                "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            }
            with patch.dict(os.environ, env, clear=True):
                snapshot = read_codex_rate_limits(timeout_seconds=5)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["source"], "codex_app_server")
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(snapshot["rate_limits"]["plan_type"], "pro")
        self.assertEqual(snapshot["rate_limits"]["primary"]["used_percent"], 14)
        self.assertEqual(snapshot["rate_limits"]["primary"]["remaining_percent"], 86)
        self.assertEqual(snapshot["rate_limits"]["secondary"]["window_duration_mins"], 10080)
        self.assertEqual(snapshot["rate_limits"]["secondary"]["remaining_percent"], 73)
        self.assertEqual(snapshot["rate_limits"]["credits"]["balance"], "0")


if __name__ == "__main__":
    unittest.main()
