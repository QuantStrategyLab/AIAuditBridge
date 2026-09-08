"""Synthetic regressions for patch, API cost and diagnostic boundaries."""

import http.client as http_client
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import run_monthly_codex_audit as audit
from service.adapters.llm_adapter import LlmAdapter
from service.quota import QuotaManager


class PatchBoundaryTests(unittest.TestCase):
    def test_monthly_rejects_symlinks_before_any_write(self):
        for target in (".env", ".git/config", "normal.txt"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                protected = root / target
                protected.parent.mkdir(parents=True, exist_ok=True)
                protected.write_text("synthetic baseline")
                (root / "alias.py").symlink_to(protected)
                with self.assertRaises(audit.BridgeError):
                    audit.apply_service_changes(root, [
                        {"path": "first.txt", "content": "first"},
                        {"path": "alias.py", "content": "changed"},
                    ], task="monthly_snapshot_audit")
                self.assertFalse((root / "first.txt").exists())
                self.assertEqual(protected.read_text(), "synthetic baseline")

    def test_monthly_rejects_directory_symlink_and_invalid_parent_atomically(self):
        for symlink in (True, False):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "real").mkdir()
                if symlink:
                    (root / "alias").symlink_to(root / "real", target_is_directory=True)
                else:
                    (root / "alias").write_text("not a directory")
                with self.assertRaises(audit.BridgeError):
                    audit.apply_service_changes(root, [
                        {"path": "first.txt", "content": "first"},
                        {"path": "alias/new.txt", "content": "changed"},
                    ], task="monthly_snapshot_audit")
                self.assertFalse((root / "first.txt").exists())

    def test_service_entry_rejects_alias_patch_without_partial_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".codex-audit").mkdir()
            (root / ".env").write_text("synthetic baseline")
            (root / "alias.py").symlink_to(root / ".env")
            response = json.dumps({"final_message": "synthetic", "changes": [
                {"path": "first.txt", "content": "first"},
                {"path": "alias.py", "content": "changed"},
            ]})
            with patch.object(audit, "build_service_prompt", return_value="synthetic"), patch.object(
                audit, "request_codex_service", return_value=response,
            ):
                code, _, _ = audit.run_codex_service(root, "synthetic", 1,
                    source_repo="Synthetic/caller", source_ref="main", task="monthly_snapshot_audit",
                    mode="review_and_fix")
            self.assertNotEqual(code, 0)
            self.assertFalse((root / "first.txt").exists())
            self.assertEqual((root / ".env").read_text(), "synthetic baseline")

    def test_direct_api_bypass_is_disabled_before_network(self):
        for request in (audit.request_openai_completion, audit.request_anthropic_completion):
            with self.subTest(request=request.__name__), patch.dict(os.environ, {
                "OPENAI_API_KEY": "synthetic-only", "ANTHROPIC_API_KEY": "synthetic-only",
            }), patch.object(audit.urllib.request, "urlopen") as send:
                send.return_value.__enter__.return_value.read.return_value = json.dumps({
                    "choices": [{"message": {"content": "synthetic"}}],
                    "content": [{"type": "text", "text": "synthetic"}],
                }).encode()
                with self.assertRaisesRegex(audit.BridgeError, "budgeted service"):
                    request(system="synthetic", user="synthetic")
                send.assert_not_called()


class BudgetBoundaryTests(unittest.TestCase):
    def test_configured_prices_and_weekly_budget_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({
                "default_daily_budget_usd": 1000, "default_weekly_budget_usd": 0,
                "model_costs_per_1k_tokens": {"gpt-5.4-mini": {"input": 2, "output": 3}},
            }))
            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_QUOTA_CONFIG": str(config)}, clear=True), patch(
                "service.quota.recommend_model", return_value="gpt-5.4-mini",
            ):
                quota = QuotaManager()
                check = quota.check("Synthetic/caller", "gpt-5.4-mini", "x" * 4000, 1000)
                self.assertFalse(check["allowed"])
                self.assertEqual(check["cost_estimate_usd"], 5)
                quota.record("Synthetic/caller", "gpt-5.4-mini", "x" * 4000,
                             reported_tokens_input=1000, reported_tokens_output=0,
                             reported_usage_complete=True)
                self.assertEqual(quota._records["Synthetic/caller"].api_key_cost_usd, 2)

    def test_corrupt_existing_store_blocks_api_and_is_not_overwritten(self):
        for content in ("{", "[]", '{"records":[]}', '{"records":{"Synthetic/caller":null}}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp) / "quota.json"
                store.write_text(content)
                with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_QUOTA_STORE": str(store)}, clear=True):
                    quota = QuotaManager()
                    check = quota.check("Synthetic/caller", "gpt-5.4-mini", "synthetic")
                    self.assertFalse(check["allowed"])
                    self.assertIn("unavailable", check["reason"])
                    quota.record_execute("Synthetic/caller")
                    self.assertEqual(store.read_text(), content)

    def test_unreadable_store_blocks_api_but_not_subscription_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "quota.json"
            store.write_text('{"records":{}}')
            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_QUOTA_STORE": str(store)}, clear=True), patch.object(
                Path, "read_text", side_effect=PermissionError("synthetic"),
            ):
                quota = QuotaManager()
            self.assertFalse(quota.check("Synthetic/caller", "gpt-5.4-mini")["allowed"])
            with patch.object(quota, "_codex_account_snapshot", return_value={
                "status": "available", "rate_limits": {"primary": {"used_percent": 5}},
            }):
                self.assertTrue(quota.check("Synthetic/caller", "codex-cli", codex_account=True)["allowed"])

    def test_nonfinite_budget_cannot_be_hidden_by_other_window(self):
        for window in ("_daily_budget", "_weekly_budget"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(window=window, value=value), patch.dict(os.environ, {}, clear=True):
                    quota = QuotaManager()
                    setattr(quota, window, value)
                    self.assertFalse(quota.check("Synthetic/caller", "gpt-5.4-mini")["allowed"])

    def test_invalid_reported_count_blocks_admission(self):
        for count in ("invalid", {}, -1, True):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp) / "quota.json"
                store.write_text(json.dumps({"records": {"Synthetic/caller": {"reported_tokens_input": count}}}))
                with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_QUOTA_STORE": str(store)}, clear=True):
                    quota = QuotaManager()
                    self.assertFalse(quota.check("Synthetic/caller", "gpt-5.4-mini")["allowed"])

    def test_new_or_valid_empty_store_allows_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "quota.json"
            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_QUOTA_STORE": str(store)}, clear=True):
                self.assertTrue(QuotaManager().check("Synthetic/caller", "gpt-5.4-mini")["allowed"])
                store.write_text('{"records":{}}')
                self.assertTrue(QuotaManager().check("Synthetic/caller", "gpt-5.4-mini")["allowed"])


class ErrorBoundaryTests(unittest.TestCase):
    def test_unusual_transport_and_body_read_errors_are_sanitized(self):
        marker = "SYNTHETIC_PRIVATE_CONTEXT"
        broken = unittest.mock.Mock()
        broken.read.side_effect = OSError(marker)
        errors = [http_client.BadStatusLine(marker),
                  urllib.error.HTTPError("https://synthetic.invalid", 400, "synthetic", {}, broken)]
        for error in errors:
            with self.subTest(kind=type(error).__name__), patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-only"}), patch(
                "service.adapters.llm_adapter.urllib.request.urlopen", side_effect=error,
            ):
                results = LlmAdapter().parallel_review(reviewers=[("gpt", "gpt-5.4-mini")])
            self.assertFalse(results[0].success)
            self.assertNotIn(marker, results[0].error)

    def test_provider_error_is_categorical_but_keeps_known_usage(self):
        for model, key, usage in (
            ("gpt-5.4-mini", "OPENAI_API_KEY", {"prompt_tokens": 10, "completion_tokens": 0}),
            ("claude-sonnet-4-6", "ANTHROPIC_API_KEY", {"input_tokens": 10, "output_tokens": 0}),
        ):
            for http in (True, False):
                with self.subTest(model=model, http=http):
                    marker = "SYNTHETIC_PRIVATE_CONTEXT"
                    error = urllib.error.HTTPError("https://synthetic.invalid", 400, "synthetic", {},
                        io.BytesIO(json.dumps({"error": {"message": marker}, "usage": usage}).encode())) if http else urllib.error.URLError(marker)
                    with patch.dict(os.environ, {key: "synthetic-only"}), patch(
                        "service.adapters.llm_adapter.urllib.request.urlopen", side_effect=error,
                    ):
                        result = LlmAdapter().complete(model=model, user="synthetic")
                    self.assertFalse(result.success)
                    self.assertNotIn(marker, result.error)
                    if http:
                        self.assertEqual((result.tokens_input, result.tokens_output), (10, 0))
                        self.assertTrue(result.usage_complete)
