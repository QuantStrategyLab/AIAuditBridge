"""Provider-lane contract, adapter selection, Cursor policy trust, and limiter."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from client.config import ProviderConfig
from service.adapters.codex_adapter import CodexAdapter
from service.adapters.cursor_adapter import CursorAdapter
from service.adapters.execution import resolve_execution_adapter
from service.automation_decision import EXECUTION_POLICY_OWNER_ENV
from service.contracts import (
    ALLOWED_EXECUTION_PROVIDER_CHAINS,
    EXECUTION_PROVIDERS,
    ExecuteRequest,
    MODE_REVIEW_AND_FIX,
    MODE_REVIEW_ONLY,
    PROVIDER_CODEX,
    PROVIDER_CURSOR,
)
from service import ai_gateway_service as gateway
from service import cursor_account


class ProviderContractTests(unittest.TestCase):
    def test_execution_provider_constants_include_cursor(self) -> None:
        self.assertEqual(EXECUTION_PROVIDERS, frozenset({PROVIDER_CODEX, PROVIDER_CURSOR}))
        self.assertIn([PROVIDER_CODEX], ALLOWED_EXECUTION_PROVIDER_CHAINS)
        self.assertIn([PROVIDER_CURSOR], ALLOWED_EXECUTION_PROVIDER_CHAINS)
        self.assertIn([PROVIDER_CODEX, PROVIDER_CURSOR], ALLOWED_EXECUTION_PROVIDER_CHAINS)

    def test_execute_request_accepts_documented_provider_chains(self) -> None:
        for chain in ALLOWED_EXECUTION_PROVIDER_CHAINS:
            mode = MODE_REVIEW_ONLY if PROVIDER_CURSOR in chain else MODE_REVIEW_AND_FIX
            ExecuteRequest(prompt="synthetic", mode=mode, allowed_providers=list(chain)).validate()

    def test_execute_request_rejects_unknown_provider_chain(self) -> None:
        with self.assertRaisesRegex(ValueError, "allowed_providers"):
            ExecuteRequest(prompt="synthetic", allowed_providers=["openai"]).validate()

    def test_provider_config_exposes_cursor_factory(self) -> None:
        config = ProviderConfig.cursor("composer-2.5")
        self.assertEqual(config.label, "cursor")
        self.assertEqual(config.model, "composer-2.5")
        self.assertTrue(config.can_execute_code)


class ExecutionAdapterTests(unittest.TestCase):
    def test_resolve_defaults_and_known_providers(self) -> None:
        self.assertIsInstance(resolve_execution_adapter(""), CodexAdapter)
        self.assertIsInstance(resolve_execution_adapter("codex"), CodexAdapter)
        self.assertIsInstance(resolve_execution_adapter("cursor"), CursorAdapter)

    def test_resolve_unknown_provider_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported execution provider"):
            resolve_execution_adapter("openai")


class CursorPolicyTrustTests(unittest.TestCase):
    def _owner_env(self) -> dict[str, str]:
        return {EXECUTION_POLICY_OWNER_ENV: f"{os.getuid()}:{os.getgid()}"}

    def test_symlink_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            target = root / "real.json"
            target.write_text("{}", encoding="utf-8")
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
            link = root / "policy.json"
            link.symlink_to(target)
            with patch.dict(
                os.environ,
                {
                    **self._owner_env(),
                    "AI_GATEWAY_CURSOR_ENABLED": "true",
                    "AI_GATEWAY_CURSOR_POLICY_PATH": str(link),
                },
                clear=False,
            ):
                route = cursor_account.cursor_research_route(
                    {"research_stage": "research_summary", "mode": "review_only"},
                    {"cursor_calls": 0},
                    now=time.time(),
                )
            self.assertEqual(route["action"], "defer")
            self.assertEqual(route["reason"], "cursor_policy_untrusted")

    def test_readiness_reports_disabled_without_enabling_cursor(self) -> None:
        with patch.dict(os.environ, {"AI_GATEWAY_CURSOR_ENABLED": "false"}, clear=False):
            readiness = cursor_account.subscription_research_readiness(now=time.time())
        self.assertEqual(readiness, {"status": "disabled", "reason": "cursor_disabled"})

    def test_group_writable_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            policy_path = Path(raw_dir) / "policy.json"
            policy_path.write_text("{}", encoding="utf-8")
            policy_path.chmod(policy_path.stat().st_mode | stat.S_IWGRP)
            with patch.dict(
                os.environ,
                {
                    **self._owner_env(),
                    "AI_GATEWAY_CURSOR_ENABLED": "true",
                    "AI_GATEWAY_CURSOR_POLICY_PATH": str(policy_path),
                },
                clear=False,
            ):
                route = cursor_account.cursor_research_route(
                    {"research_stage": "research_summary", "mode": "review_only"},
                    {"cursor_calls": 0},
                    now=time.time(),
                )
            self.assertEqual(route["action"], "defer")
            self.assertEqual(route["reason"], "cursor_policy_untrusted")

    def test_readiness_ready_when_trusted_route_admits(self) -> None:
        now = time.time()
        policy = {
            "on_demand_disabled_verified": True,
            "valid_until": now + 3600,
            "max_daily_calls": 10,
            "models": {
                "composer-2": {
                    "quality_level": 0,
                    "supported_reasoning_efforts": ["low"],
                }
            },
        }
        roster = {
            "status": "available",
            "source": "cursor_cli_account",
            "updated_at": now,
            "models": ["composer-2"],
        }
        catalog = type("Catalog", (), {"subscription_rosters": {"cursor": roster}})()
        with patch.dict(
            os.environ,
            {**self._owner_env(), "AI_GATEWAY_CURSOR_ENABLED": "true", "AI_GATEWAY_CURSOR_POLICY_PATH": "/tmp/unused"},
            clear=False,
        ), patch(
            "service.automation_decision._read_trusted_policy_file",
            return_value=(json.dumps(policy), ""),
        ), patch("service.cursor_account.load_catalog", return_value=catalog):
            readiness = cursor_account.subscription_research_readiness(now=now)
        self.assertEqual(readiness, {"status": "ready", "reason": "cursor_route_ready"})


class RateLimitLockTests(unittest.TestCase):
    def test_rate_limit_cap_holds_under_concurrent_callers(self) -> None:
        gateway._analyze_timestamps.clear()
        accepted = 0
        rejected = 0
        lock = threading.Lock()

        def worker() -> None:
            nonlocal accepted, rejected
            try:
                gateway._check_rate_limit(max_per_window=5, window=60.0)
                with lock:
                    accepted += 1
            except PermissionError:
                with lock:
                    rejected += 1

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(accepted, 5)
        self.assertEqual(rejected, 15)
        gateway._analyze_timestamps.clear()


if __name__ == "__main__":
    unittest.main()
