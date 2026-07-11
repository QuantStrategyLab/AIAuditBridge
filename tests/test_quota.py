"""Tests for service/quota.py — rate limiting and budget tracking."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from service.quota import (
    DEFAULT_DAILY_BUDGET_USD,
    DEFAULT_MODEL_COSTS,
    QuotaManager,
    QuotaRecord,
    estimate_cost,
    estimate_tokens,
    get_quota_manager,
    recommend_model,
)


class TestTokenEstimation(unittest.TestCase):
    """Token count estimation logic."""

    def test_estimate_tokens_empty_string(self) -> None:
        self.assertEqual(estimate_tokens(""), 1)

    def test_estimate_tokens_short_text(self) -> None:
        self.assertGreater(estimate_tokens("Hello world"), 0)

    def test_estimate_tokens_proportional_to_length(self) -> None:
        short = estimate_tokens("A" * 100)
        long = estimate_tokens("A" * 1000)
        self.assertLess(short, long)


class TestCostEstimation(unittest.TestCase):
    """Model cost estimation."""

    def test_estimate_cost_flat_model(self) -> None:
        cost = estimate_cost("codex-cli", 100)
        self.assertEqual(cost, DEFAULT_MODEL_COSTS["codex-cli"]["flat"])

    def test_estimate_cost_tiered_model(self) -> None:
        cost = estimate_cost("claude-sonnet-4-6", 1000, 500)
        self.assertGreater(cost, 0)

    def test_estimate_cost_unknown_model_uses_defaults(self) -> None:
        cost = estimate_cost("unknown-model", 1000)
        self.assertGreater(cost, 0)


class TestRecommendModel(unittest.TestCase):
    """Model recommendation based on remaining budget."""

    @classmethod
    def setUpClass(cls) -> None:
        catalog = Path(__file__).resolve().parents[1] / "generated" / "model_catalog.json"
        if not catalog.is_file():
            raise unittest.SkipTest("generated/model_catalog.json missing")
        cls._catalog_path = str(catalog)

    def setUp(self) -> None:
        os.environ["MODEL_CATALOG_PATH"] = self._catalog_path

    def tearDown(self) -> None:
        os.environ.pop("MODEL_CATALOG_PATH", None)
        from service.model_resolver import reset_catalog_cache

        reset_catalog_cache()

    def test_recommend_cheapest_for_low_budget(self) -> None:
        from service.model_catalog import load_catalog
        from service.model_resolver import tier_for_budget

        catalog = load_catalog(Path(self._catalog_path))
        model = recommend_model(0.005)
        self.assertEqual(model, catalog.model_for_tier(tier_for_budget(0.005)))

    def test_recommend_standard_for_moderate_budget(self) -> None:
        from service.model_catalog import load_catalog
        from service.model_resolver import tier_for_budget

        catalog = load_catalog(Path(self._catalog_path))
        model = recommend_model(0.02)
        self.assertEqual(model, catalog.model_for_tier(tier_for_budget(0.02)))

    def test_recommend_default_for_high_budget(self) -> None:
        from service.model_catalog import load_catalog
        from service.model_resolver import tier_for_budget

        catalog = load_catalog(Path(self._catalog_path))
        model = recommend_model(1.0)
        self.assertEqual(model, catalog.model_for_tier(tier_for_budget(1.0)))


class TestQuotaRecord(unittest.TestCase):
    """QuotaRecord serialization."""

    def test_to_dict_includes_all_fields(self) -> None:
        record = QuotaRecord(
            repo="owner/repo",
            tokens_input=100,
            tokens_output=50,
            api_key_tokens_input=100,
            api_key_tokens_output=50,
            api_calls=1,
            codex_calls=2,
            total_cost_usd=0.5,
            api_key_cost_usd=0.4,
            codex_cost_usd=0.1,
            legacy_tokens_input=25,
            legacy_tokens_output=10,
            legacy_usage_incomplete=True,
            legacy_unknown_cost_usd=0.2,
        )
        d = record.to_dict()
        self.assertEqual(d["repo"], "owner/repo")
        self.assertEqual(d["tokens_input"], 100)
        self.assertEqual(d["tokens_output"], 50)
        self.assertEqual(d["api_key_tokens_input"], 100)
        self.assertEqual(d["api_key_tokens_output"], 50)
        self.assertEqual(d["api_calls"], 1)
        self.assertFalse(d["api_calls_incomplete"])
        self.assertEqual(d["legacy_tokens_input"], 25)
        self.assertEqual(d["legacy_tokens_output"], 10)
        self.assertTrue(d["legacy_usage_incomplete"])
        self.assertEqual(d["legacy_unknown_cost_usd"], 0.2)
        self.assertEqual(d["codex_calls"], 2)
        self.assertEqual(d["api_key_cost_usd"], 0.4)
        self.assertEqual(d["codex_cost_usd"], 0.1)
        self.assertAlmostEqual(d["total_cost_usd"], 0.5)

    def test_from_dict_roundtrip(self) -> None:
        original = QuotaRecord(
            repo="owner/repo",
            tokens_input=200,
            api_key_tokens_input=200,
            total_cost_usd=1.0,
            api_key_cost_usd=1.0,
        )
        d = original.to_dict()
        restored = QuotaRecord.from_dict(d)
        self.assertEqual(restored.repo, original.repo)
        self.assertEqual(restored.tokens_input, original.tokens_input)
        self.assertEqual(restored.api_key_tokens_input, original.tokens_input)
        self.assertEqual(restored.total_cost_usd, original.total_cost_usd)

    def test_legacy_record_starts_a_fresh_weekly_budget_window(self) -> None:
        restored = QuotaRecord.from_dict({
            "repo": "owner/repo",
            "total_cost_usd": 0.5,
            "api_key_cost_usd": 0.5,
            "last_reset_weekly": 0,
        })

        self.assertEqual(restored.weekly_api_key_cost_usd, 0.0)
        self.assertGreater(restored.last_reset_weekly, 0)


class TestQuotaManager(unittest.TestCase):
    def test_recording_failure_requires_explicit_recovery(self) -> None:
        manager = QuotaManager()
        manager.mark_recording_failed("repo/recovery")
        self.assertFalse(manager.check("repo/recovery", "gpt-5.4-mini")["allowed"])
        manager.recover_recording_failure("repo/recovery")
        self.assertTrue(manager.check("repo/recovery", "gpt-5.4-mini")["allowed"])

    def test_recording_failure_persists_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "quota.json")
            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_QUOTA_STORE": store}, clear=False):
                manager = QuotaManager()
                manager.mark_recording_failed("repo/persisted")
                restarted = QuotaManager()
            self.assertFalse(restarted.check("repo/persisted", "gpt-5.4-mini")["allowed"])
    """QuotaManager budget tracking and enforcement."""

    def setUp(self) -> None:
        self.manager = QuotaManager()

    def test_initial_state_empty_repo(self) -> None:
        remaining = self.manager.remaining_daily("unknown/repo")
        self.assertEqual(remaining, DEFAULT_DAILY_BUDGET_USD)

    def test_check_allows_request_within_budget(self) -> None:
        result = self.manager.check("test/repo", "gpt-5.4-mini", "short prompt")
        self.assertTrue(result["allowed"])

    def test_check_allows_when_called_multiple_times(self) -> None:
        for _ in range(10):
            result = self.manager.check("test/repo", "gpt-5.4-mini", "short")
            self.assertTrue(result["allowed"])

    def test_record_reduces_remaining_budget(self) -> None:
        self.manager.record("test/repo", "claude-sonnet-4-6", "A" * 4000, "B" * 1000)
        remaining = self.manager.remaining_daily("test/repo")
        self.assertLess(remaining, DEFAULT_DAILY_BUDGET_USD)

    def test_codex_execute_does_not_consume_api_budget(self) -> None:
        self.manager.record_execute("test/repo")
        remaining = self.manager.remaining_daily("test/repo")
        self.assertEqual(remaining, DEFAULT_DAILY_BUDGET_USD)

    def test_only_trusted_codex_account_checks_ignore_api_budget(self) -> None:
        self.manager._repo_budgets["test/repo"] = {"daily": 0.05, "weekly": 1.0}
        self.manager._records["test/repo"] = QuotaRecord(
            repo="test/repo", total_cost_usd=0.05, api_key_cost_usd=0.05
        )
        self.manager._codex_account_cache = {
            "updated_at": time.time(),
            "rate_limits": {
                "primary": {"remaining_percent": 80},
                "secondary": {"remaining_percent": 80},
            },
        }
        self.manager._codex_account_cache_ts = time.time()

        untrusted_result = self.manager.check("test/repo", "codex-cli", "review this pull request")
        trusted_result = self.manager.check(
            "test/repo",
            "codex-cli",
            "review this pull request",
            codex_account=True,
        )

        self.assertFalse(untrusted_result["allowed"])
        self.assertTrue(trusted_result["allowed"])
        self.assertEqual(trusted_result["quota_scope"], "codex_account")
        self.assertEqual(trusted_result["remaining_usd"], 0.0)

    def test_runtime_status_reflects_daily_budget_pressure(self) -> None:
        self.assertEqual(self.manager.runtime_status("test/repo")["status"], "ok")
        self.manager._repo_budgets["test/repo"] = {"daily": 0.06, "weekly": 1.0}
        self.manager._records["test/repo"] = QuotaRecord(
            repo="test/repo", total_cost_usd=0.05, api_key_cost_usd=0.05
        )
        self.assertEqual(self.manager.runtime_status("test/repo")["status"], "low")
        self.manager._records["test/repo"] = QuotaRecord(
            repo="test/repo", total_cost_usd=0.10, api_key_cost_usd=0.10
        )
        self.assertEqual(self.manager.runtime_status("test/repo")["status"], "exhausted")

    def test_runtime_status_boundary_ratios(self) -> None:
        self.manager._repo_budgets["low/repo"] = {"daily": 1.0, "weekly": 1.0}
        self.manager._records["low/repo"] = QuotaRecord(
            repo="low/repo", total_cost_usd=0.75, api_key_cost_usd=0.75
        )
        self.assertEqual(self.manager.runtime_status("low/repo")["status"], "low")
        self.manager._records["low/repo"] = QuotaRecord(
            repo="low/repo", total_cost_usd=0.96, api_key_cost_usd=0.96
        )
        self.assertEqual(self.manager.runtime_status("low/repo")["status"], "exhausted")
        self.manager._repo_budgets["zero/repo"] = {"daily": 0.0, "weekly": 1.0}
        self.assertEqual(self.manager.runtime_status("zero/repo")["status"], "ok")

    def test_status_returns_repo_info(self) -> None:
        self.manager.record("test/repo", "claude-sonnet-4-6", "prompt")
        status = self.manager.status("test/repo")
        self.assertEqual(status["repo"], "test/repo")
        self.assertIn("daily_budget", status)
        self.assertIn("remaining_daily", status)
        self.assertEqual(status["api_calls"], 1)
        self.assertFalse(status["api_calls_incomplete"])

    def test_status_returns_empty_if_no_records(self) -> None:
        status = self.manager.status("unknown/repo")
        self.assertIn("repo", status)
        self.assertEqual(status["total_cost_usd"], 0.0)
        self.assertEqual(status["api_calls"], 0)
        self.assertEqual(status["codex_calls"], 0)

    def test_status_summary_splits_api_key_and_codex_usage(self) -> None:
        self.manager.record("test/repo", "claude-sonnet-4-6", "A" * 4000, "B" * 1000)
        self.manager.record_execute("test/repo")
        status = self.manager.status()
        summary = status["summary"]
        self.assertEqual(summary["quota_source"], "internal_estimate")
        self.assertIn("combined", summary)
        self.assertEqual(summary["api_key"]["calls"], 1)
        self.assertFalse(summary["api_key"]["calls_incomplete"])
        self.assertEqual(summary["codex"]["calls"], 1)
        self.assertGreater(summary["api_key"]["total_cost_usd"], 0)
        self.assertGreater(summary["codex"]["total_cost_usd"], 0)
        self.assertAlmostEqual(
            summary["combined"]["total_cost_usd"],
            summary["api_key"]["total_cost_usd"] + summary["codex"]["total_cost_usd"],
        )

    def test_status_summary_can_include_live_codex_account_snapshot(self) -> None:
        snapshot = {"source": "codex_app_server", "status": "available", "rate_limits": {"plan_type": "pro"}}
        with patch("service.quota.read_codex_rate_limits", return_value=snapshot):
            status = self.manager.status()
        self.assertEqual(status["summary"]["codex_account"], snapshot)

    def test_status_summary_can_include_live_openai_account_snapshot(self) -> None:
        snapshot = {"source": "openai_admin_api", "status": "available", "costs": {"total_cost": 1.23}}
        with patch("service.quota.read_openai_admin_usage", return_value=snapshot):
            status = self.manager.status()
        self.assertEqual(status["summary"]["openai_account"], snapshot)

    def test_openai_snapshot_uses_budget_guard_billing_timezone(self) -> None:
        guard = type("Guard", (), {"billing_timezone": "Asia/Shanghai"})()
        with (
            patch("service.quota.read_openai_admin_usage", return_value=None) as read_snapshot,
            patch("service.ai_budget_guard.get_ai_budget_guard", return_value=guard),
        ):
            self.manager._openai_account_snapshot(timeout_seconds=1)
        read_snapshot.assert_called_once_with(timeout_seconds=1, billing_timezone="Asia/Shanghai")

    def test_status_summary_can_include_live_anthropic_account_snapshot(self) -> None:
        snapshot = {"source": "anthropic_admin_api", "status": "available", "costs": {"total_cost": 1.23}}
        with patch("service.quota.read_anthropic_admin_usage", return_value=snapshot):
            status = self.manager.status()
        self.assertEqual(status["summary"]["anthropic_account"], snapshot)

    def test_openai_account_failures_are_negative_cached(self) -> None:
        with patch("service.quota.read_openai_admin_usage", return_value=None) as read_snapshot:
            self.manager.status()
            self.manager.status()
        self.assertEqual(read_snapshot.call_count, 1)

    def test_anthropic_account_failures_are_negative_cached(self) -> None:
        with patch("service.quota.read_anthropic_admin_usage", return_value=None) as read_snapshot:
            self.manager.status()
            self.manager.status()
        self.assertEqual(read_snapshot.call_count, 1)

    def test_account_snapshot_reads_use_shared_status_timeout(self) -> None:
        def slow_snapshot(timeout_seconds: float | None = None, billing_timezone: str = "UTC") -> dict[str, object]:
            time.sleep(timeout_seconds or 0.25)
            return {"source": "slow", "status": "available"}

        env = {"CODEX_AUDIT_SERVICE_ACCOUNT_SNAPSHOT_STATUS_TIMEOUT_SECONDS": "0.05"}
        with patch.dict(os.environ, env):
            with (
                patch("service.quota.read_codex_rate_limits", side_effect=slow_snapshot),
                patch("service.quota.read_openai_admin_usage", side_effect=slow_snapshot),
                patch("service.quota.read_anthropic_admin_usage", side_effect=slow_snapshot),
            ):
                started = time.monotonic()
                status = self.manager.status()
                elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2)
        self.assertEqual(status["summary"]["codex_account"]["source"], "slow")
        self.assertEqual(status["summary"]["openai_account"]["source"], "slow")
        self.assertEqual(status["summary"]["anthropic_account"]["source"], "slow")

    def test_openai_account_snapshot_refresh_is_single_flight(self) -> None:
        snapshot = {"source": "openai_admin_api", "status": "available"}

        def slow_snapshot(timeout_seconds: float | None = None, billing_timezone: str = "UTC") -> dict[str, object]:
            time.sleep(0.05)
            return snapshot

        def read_from_manager(_: int) -> dict[str, object] | None:
            return self.manager._openai_account_snapshot(timeout_seconds=0.1)

        with patch("service.quota.read_openai_admin_usage", side_effect=slow_snapshot) as read_usage:
            with ThreadPoolExecutor(max_workers=3) as executor:
                results = list(executor.map(read_from_manager, range(3)))

        self.assertEqual(read_usage.call_count, 1)
        self.assertEqual(results, [snapshot, snapshot, snapshot])

    def test_codex_account_failures_are_negative_cached(self) -> None:
        with patch("service.quota.read_codex_rate_limits", return_value=None) as read_snapshot:
            self.manager.status()
            self.manager.status()
        self.assertEqual(read_snapshot.call_count, 1)

    def test_invalid_codex_account_cache_ttl_does_not_break_status(self) -> None:
        snapshot = {"source": "codex_app_server", "status": "available", "rate_limits": {"plan_type": "pro"}}
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_CACHE_SECONDS": "invalid"}):
            with patch("service.quota.read_codex_rate_limits", return_value=snapshot):
                status = self.manager.status()
        self.assertEqual(status["summary"]["codex_account"], snapshot)

    def test_status_summary_does_not_invent_global_budget(self) -> None:
        self.manager._repo_budgets["blocked/repo"] = {"daily": 0.0}
        self.manager.record_execute("blocked/repo")
        status = self.manager.status()
        self.assertNotIn("daily_budget", status["summary"]["combined"])
        self.assertNotIn("remaining_daily", status["summary"]["combined"])

    def test_codex_record_does_not_count_as_api_key_tokens(self) -> None:
        self.manager.record("test/repo", "codex-cli", "A" * 4000)
        status = self.manager.status()
        self.assertEqual(status["summary"]["api_key"]["calls"], 0)
        self.assertEqual(status["summary"]["api_key"]["tokens_input"], 0)
        self.assertEqual(status["summary"]["codex"]["calls"], 1)

    def test_missing_historical_api_call_count_is_marked_incomplete(self) -> None:
        record = QuotaRecord.from_dict({
            "repo": "old/repo",
            "tokens_input": 1000,
            "tokens_output": 500,
            "codex_calls": 0,
            "total_cost_usd": 0.25,
        })
        self.manager._records["old/repo"] = record
        status = self.manager.status()
        self.assertTrue(status["repos"]["old/repo"]["api_calls_incomplete"])
        self.assertTrue(status["summary"]["api_key"]["calls_incomplete"])
        self.assertEqual(status["summary"]["api_key"]["tokens_input"], 1000)
        self.assertNotIn("legacy_unknown", status["summary"])

    def test_historical_tokens_with_codex_execs_remain_legacy_unknown(self) -> None:
        record = QuotaRecord.from_dict({
            "repo": "old/api-plus-codex-execs",
            "tokens_input": 1000,
            "tokens_output": 500,
            "codex_calls": 2,
            "total_cost_usd": 0.2,
        })
        self.assertEqual(record.api_key_tokens_input, 0)
        self.assertEqual(record.api_key_tokens_output, 0)
        self.assertFalse(record.api_calls_incomplete)
        self.assertEqual(record.legacy_tokens_input, 1000)
        self.assertEqual(record.legacy_tokens_output, 500)
        self.assertTrue(record.legacy_usage_incomplete)
        self.assertEqual(record.codex_calls, 2)
        self.assertAlmostEqual(record.api_key_cost_usd, 0.0)
        self.assertAlmostEqual(record.codex_cost_usd, 0.0)
        self.assertAlmostEqual(record.legacy_unknown_cost_usd, 0.2)

    def test_historical_codex_only_cost_does_not_consume_api_budget(self) -> None:
        record = QuotaRecord.from_dict({
            "repo": "old/codex-only",
            "codex_calls": 100,
            "total_cost_usd": 5.0,
        })
        self.manager._records[record.repo] = record

        self.assertAlmostEqual(record.api_key_cost_usd, 0.0)
        self.assertAlmostEqual(record.codex_cost_usd, 5.0)
        self.assertEqual(self.manager.remaining_daily(record.repo), DEFAULT_DAILY_BUDGET_USD)

    def test_legacy_record_preserves_active_weekly_api_usage(self) -> None:
        now = time.time()
        record = QuotaRecord.from_dict({
            "repo": "old/api",
            "tokens_input": 1000,
            "tokens_output": 500,
            "total_cost_usd": 0.75,
            "last_reset_weekly": now - 60,
        })

        self.assertAlmostEqual(record.weekly_api_key_cost_usd, 0.75)
        self.assertAlmostEqual(record.weekly_legacy_unknown_cost_usd, 0.0)
        self.manager._repo_budgets[record.repo] = {"daily": 5.0, "weekly": 1.0}
        self.manager._records[record.repo] = record
        self.assertAlmostEqual(self.manager.remaining_weekly(record.repo), 0.25)

    def test_explicit_legacy_tokens_are_preserved_as_legacy_unknown(self) -> None:
        record = QuotaRecord.from_dict({
            "repo": "old/legacy-explicit",
            "tokens_input": 1000,
            "tokens_output": 500,
            "legacy_tokens_input": 1000,
            "legacy_tokens_output": 500,
            "legacy_usage_incomplete": True,
            "codex_calls": 2,
            "total_cost_usd": 0.2,
        })
        self.assertEqual(record.api_key_tokens_input, 0)
        self.assertEqual(record.api_key_tokens_output, 0)
        self.assertEqual(record.legacy_tokens_input, 1000)
        self.assertEqual(record.legacy_tokens_output, 500)
        self.assertTrue(record.legacy_usage_incomplete)
        self.assertFalse(record.api_calls_incomplete)
        self.assertAlmostEqual(record.api_key_cost_usd, 0.0)
        self.assertAlmostEqual(record.codex_cost_usd, 0.0)
        self.assertAlmostEqual(record.legacy_unknown_cost_usd, 0.2)

    def test_zero_cost_historical_api_tokens_migrate_to_api_key_tokens(self) -> None:
        record = QuotaRecord.from_dict({
            "repo": "old/zero-cost",
            "tokens_input": 1000,
            "tokens_output": 500,
            "total_cost_usd": 0.0,
        })
        self.manager._records["old/zero-cost"] = record
        status = self.manager.status()
        self.assertTrue(status["repos"]["old/zero-cost"]["api_calls_incomplete"])
        self.assertTrue(status["summary"]["api_key"]["calls_incomplete"])
        self.assertEqual(status["summary"]["api_key"]["tokens_input"], 1000)
        self.assertNotIn("legacy_unknown", status["summary"])

    def test_daily_budget_resets(self) -> None:
        """Quick test: budget resets when last_reset is old."""
        # Create record with old timestamp (force reset)
        record = QuotaRecord(
            repo="test/repo",
            tokens_input=5000, api_key_tokens_input=100, legacy_tokens_input=50,
            api_calls=2, api_calls_incomplete=True, legacy_usage_incomplete=True,
            codex_calls=1, total_cost_usd=3.0, api_key_cost_usd=1.0,
            codex_cost_usd=0.1, legacy_unknown_cost_usd=1.9,
            last_reset_daily=0,  # long ago
        )
        self.manager._records["test/repo"] = record
        remaining = self.manager.remaining_daily("test/repo")
        self.assertEqual(remaining, DEFAULT_DAILY_BUDGET_USD)
        reset = self.manager._records["test/repo"]
        self.assertEqual(reset.api_key_tokens_input, 0)
        self.assertEqual(reset.legacy_tokens_input, 0)
        self.assertEqual(reset.legacy_unknown_cost_usd, 0.0)

    def test_get_daily_budget_respects_repo_overrides(self) -> None:
        self.manager._repo_budgets["premium/repo"] = {"daily": 50.0}
        self.assertEqual(self.manager.get_daily_budget("premium/repo"), 50.0)

    def test_get_weekly_budget_respects_repo_overrides(self) -> None:
        self.manager._repo_budgets["premium/repo"] = {"weekly": 250.0}
        self.assertEqual(self.manager.get_weekly_budget("premium/repo"), 250.0)

    def test_weekly_budget_resets_stale_record(self) -> None:
        self.manager._repo_budgets["test/repo"] = {"daily": 5.0, "weekly": 1.0}
        self.manager._records["test/repo"] = QuotaRecord(
            repo="test/repo",
            total_cost_usd=0.5,
            api_key_cost_usd=0.5,
            last_reset_weekly=0,
            weekly_api_key_cost_usd=0.5,
        )

        self.assertEqual(self.manager.remaining_weekly("test/repo"), 1.0)

    def test_api_record_reduces_weekly_budget(self) -> None:
        self.manager.record("test/repo", "claude-sonnet-4-6", "A" * 4000, "B" * 1000)

        self.assertLess(self.manager.remaining_weekly("test/repo"), DEFAULT_DAILY_BUDGET_USD * 5)

    def test_records_persist_to_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "quota.json")
            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_QUOTA_STORE": store}):
                first = QuotaManager()
                first.record_execute("test/repo")
                second = QuotaManager()
                status = second.status("test/repo")
        self.assertEqual(status["codex_calls"], 1)
        self.assertGreater(status["total_cost_usd"], 0)


class TestQuotaConfigLoading(unittest.TestCase):
    """Quota configuration from JSON file."""

    def test_load_config_from_file(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(
                '{"default_daily_budget_usd": 10.0, '
                '"model_costs_per_1k_tokens": {"custom-model": {"input": 0.01, "output": 0.02}}}'
            )
            config_path = f.name

        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_QUOTA_CONFIG": config_path}):
            manager = QuotaManager()
            self.assertEqual(manager._daily_budget, 10.0)
            self.assertIn("custom-model", manager._model_costs)

        Path(config_path).unlink(missing_ok=True)

    def test_load_config_handles_missing_file(self) -> None:
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_QUOTA_CONFIG": "/nonexistent/quota.json"}):
            manager = QuotaManager()
            self.assertEqual(manager._daily_budget, DEFAULT_DAILY_BUDGET_USD)

    def test_load_config_handles_invalid_json(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid")
            config_path = f.name

        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_QUOTA_CONFIG": config_path}):
            manager = QuotaManager()
            self.assertEqual(manager._daily_budget, DEFAULT_DAILY_BUDGET_USD)

        Path(config_path).unlink(missing_ok=True)


class TestQuotaManagerSingleton(unittest.TestCase):
    """Global quota manager instance."""

    def test_get_quota_manager_returns_singleton(self) -> None:
        q1 = get_quota_manager()
        q2 = get_quota_manager()
        self.assertIs(q1, q2)
