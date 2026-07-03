"""Tests for service/quota.py — rate limiting and budget tracking."""

from __future__ import annotations

import os
import tempfile
import unittest
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

    def test_recommend_cheapest_for_low_budget(self) -> None:
        model = recommend_model(0.005)
        self.assertEqual(model, "gpt-5.4-mini")

    def test_recommend_standard_for_moderate_budget(self) -> None:
        model = recommend_model(0.02)
        self.assertEqual(model, "claude-sonnet-4-6")

    def test_recommend_default_for_high_budget(self) -> None:
        model = recommend_model(1.0)
        self.assertEqual(model, "claude-sonnet-4-6")


class TestQuotaRecord(unittest.TestCase):
    """QuotaRecord serialization."""

    def test_to_dict_includes_all_fields(self) -> None:
        record = QuotaRecord(
            repo="owner/repo",
            tokens_input=100,
            tokens_output=50,
            api_calls=1,
            codex_calls=2,
            total_cost_usd=0.5,
            api_key_cost_usd=0.4,
            codex_cost_usd=0.1,
        )
        d = record.to_dict()
        self.assertEqual(d["repo"], "owner/repo")
        self.assertEqual(d["tokens_input"], 100)
        self.assertEqual(d["tokens_output"], 50)
        self.assertEqual(d["api_calls"], 1)
        self.assertEqual(d["codex_calls"], 2)
        self.assertEqual(d["api_key_cost_usd"], 0.4)
        self.assertEqual(d["codex_cost_usd"], 0.1)
        self.assertAlmostEqual(d["total_cost_usd"], 0.5)

    def test_from_dict_roundtrip(self) -> None:
        original = QuotaRecord(repo="owner/repo", tokens_input=200, total_cost_usd=1.0)
        d = original.to_dict()
        restored = QuotaRecord.from_dict(d)
        self.assertEqual(restored.repo, original.repo)
        self.assertEqual(restored.tokens_input, original.tokens_input)
        self.assertEqual(restored.total_cost_usd, original.total_cost_usd)


class TestQuotaManager(unittest.TestCase):
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

    def test_record_execute_reduces_remaining_budget(self) -> None:
        self.manager.record_execute("test/repo")
        remaining = self.manager.remaining_daily("test/repo")
        self.assertLess(remaining, DEFAULT_DAILY_BUDGET_USD)

    def test_status_returns_repo_info(self) -> None:
        self.manager.record("test/repo", "claude-sonnet-4-6", "prompt")
        status = self.manager.status("test/repo")
        self.assertEqual(status["repo"], "test/repo")
        self.assertIn("daily_budget", status)
        self.assertIn("remaining_daily", status)
        self.assertEqual(status["api_calls"], 1)

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
        self.assertEqual(summary["codex"]["calls"], 1)
        self.assertGreater(summary["api_key"]["total_cost_usd"], 0)
        self.assertGreater(summary["codex"]["total_cost_usd"], 0)
        self.assertAlmostEqual(
            summary["combined"]["total_cost_usd"],
            summary["api_key"]["total_cost_usd"] + summary["codex"]["total_cost_usd"],
        )

    def test_status_summary_respects_zero_budget(self) -> None:
        self.manager._repo_budgets["blocked/repo"] = {"daily": 0.0}
        self.manager.record_execute("blocked/repo")
        status = self.manager.status()
        self.assertEqual(status["summary"]["combined"]["daily_budget"], 0.0)
        self.assertEqual(status["summary"]["combined"]["remaining_daily"], 0.0)

    def test_daily_budget_resets(self) -> None:
        """Quick test: budget resets when last_reset is old."""
        # Create record with old timestamp (force reset)
        record = QuotaRecord(
            repo="test/repo",
            tokens_input=5000,
            total_cost_usd=3.0,
            last_reset_daily=0,  # long ago
        )
        self.manager._records["test/repo"] = record
        remaining = self.manager.remaining_daily("test/repo")
        self.assertEqual(remaining, DEFAULT_DAILY_BUDGET_USD)

    def test_get_daily_budget_respects_repo_overrides(self) -> None:
        self.manager._repo_budgets["premium/repo"] = {"daily": 50.0}
        self.assertEqual(self.manager.get_daily_budget("premium/repo"), 50.0)

    def test_get_weekly_budget_respects_repo_overrides(self) -> None:
        self.manager._repo_budgets["premium/repo"] = {"weekly": 250.0}
        self.assertEqual(self.manager.get_weekly_budget("premium/repo"), 250.0)

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
