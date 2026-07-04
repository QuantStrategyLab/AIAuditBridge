"""Tests for Codex account rate-limit snapshot reader."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from service.codex_account import read_codex_rate_limits


class TestCodexAccountRateLimits(unittest.TestCase):
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

                    if os.environ.get("CODEX_AUDIT_SERVICE_TOKEN") or os.environ.get("OPENAI_API_KEY"):
                        sys.exit(7)

                    for line in sys.stdin:
                        message = json.loads(line)
                        method = message.get("method")
                        if method == "initialize":
                            print(json.dumps({"id": message["id"], "result": {"userAgent": "fake"}}), flush=True)
                        elif method == "account/rateLimits/read":
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
        self.assertEqual(snapshot["rate_limits"]["secondary"]["window_duration_mins"], 10080)
        self.assertEqual(snapshot["rate_limits"]["credits"]["balance"], "0")


if __name__ == "__main__":
    unittest.main()
