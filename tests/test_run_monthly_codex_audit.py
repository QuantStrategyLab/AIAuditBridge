from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_monthly_codex_audit import (
    blocked_paths,
    bootstrap_packages,
    build_api_review_prompt,
    codex_process_env,
    convert_local_markdown_links,
    extract_anthropic_text,
    extract_openai_text,
    auto_fallback_missing_api_key_message,
    format_api_review_comment,
    package_import_name,
    parse_bool,
    pr_closing_line,
    run_configured_api_reviews,
    safe_branch_component,
    strip_audit_heading,
    validate_provider,
    validate_repo,
    validate_task,
)


class RunMonthlyCodexAuditTests(unittest.TestCase):
    def test_parse_bool_accepts_common_true_values(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on", True):
            self.assertTrue(parse_bool(value))
        for value in ("", "false", "0", "no", False, None):
            self.assertFalse(parse_bool(value))

    def test_validate_repo_accepts_owner_repo(self) -> None:
        self.assertEqual(validate_repo("QuantStrategyLab/CryptoSnapshotPipelines"), "QuantStrategyLab/CryptoSnapshotPipelines")
        self.assertEqual(
            validate_repo("QuantStrategyLab/UsEquitySnapshotPipelines"),
            "QuantStrategyLab/UsEquitySnapshotPipelines",
        )
        self.assertEqual(
            validate_repo("QuantStrategyLab/AiLongHorizonSignalPipelines"),
            "QuantStrategyLab/AiLongHorizonSignalPipelines",
        )

    def test_validate_repo_rejects_invalid_values(self) -> None:
        with self.assertRaises(Exception):
            validate_repo("QuantStrategyLab/CryptoSnapshotPipelines/extra")
        with self.assertRaises(Exception):
            validate_repo("OtherOrg/CryptoSnapshotPipelines")

    def test_validate_task_rejects_repo_task_mismatch(self) -> None:
        self.assertEqual(
            validate_task("long-horizon-signal-shadow", "QuantStrategyLab/AiLongHorizonSignalPipelines"),
            "long_horizon_signal_shadow",
        )
        self.assertEqual(
            validate_task("", "QuantStrategyLab/CryptoSnapshotPipelines"),
            "monthly_snapshot_audit",
        )
        with self.assertRaises(Exception):
            validate_task("monthly_snapshot_audit", "QuantStrategyLab/AiLongHorizonSignalPipelines")

    def test_validate_provider_accepts_supported_values(self) -> None:
        self.assertEqual(validate_provider(""), "auto")
        self.assertEqual(validate_provider("codex"), "codex")
        self.assertEqual(validate_provider("OPENAI"), "openai")
        self.assertEqual(validate_provider("anthropic"), "anthropic")
        self.assertEqual(validate_provider("api"), "api")
        self.assertEqual(validate_provider("auto"), "auto")
        with self.assertRaises(Exception):
            validate_provider("claude")

    def test_safe_branch_component_removes_unsafe_characters(self) -> None:
        self.assertEqual(safe_branch_component("issue #12: monthly review"), "issue-12-monthly-review")

    def test_blocked_paths_blocks_data_and_secret_like_files(self) -> None:
        blocked = blocked_paths(["data/output/report.json", "docs/secret-token.md", "scripts/fix.py"])
        self.assertEqual(blocked, ["data/output/report.json", "docs/secret-token.md"])

    def test_blocked_paths_allows_long_horizon_shadow_outputs(self) -> None:
        blocked = blocked_paths(
            [
                "data/output/latest_signal.json",
                "data/output/latest_signal.manifest.json",
                "data/output/signal_history/2026-05-28.json",
                "data/raw/market_history.csv",
            ],
            task="long_horizon_signal_shadow",
        )
        self.assertEqual(blocked, ["data/raw/market_history.csv"])

    def test_codex_process_env_removes_secret_like_variables(self) -> None:
        env = codex_process_env()
        for key in env:
            self.assertNotIn("TOKEN", key.upper())
            self.assertNotIn("SECRET", key.upper())
            self.assertNotIn("PASSWORD", key.upper())
            self.assertNotIn("PRIVATE_KEY", key.upper())
            self.assertNotIn("CREDENTIAL", key.upper())

    def test_strip_audit_heading_removes_only_leading_heading(self) -> None:
        for heading in ("## Crypto Codex Audit", "## Self-hosted Codex Audit"):
            body = f"{heading}\n\n### Verdict\n\nOK"
            self.assertEqual(strip_audit_heading(body), "### Verdict\n\nOK")

    def test_convert_local_markdown_links_rewrites_repo_paths(self) -> None:
        repo_dir = Path("/tmp/selfhosted-codex-audit-abc/source")
        body = "See [script](/tmp/selfhosted-codex-audit-abc/source/scripts/run.py:42)."

        converted = convert_local_markdown_links(
            body,
            repo_dir,
            "QuantStrategyLab/CryptoSnapshotPipelines",
            "codex/monthly-audit-47",
        )

        self.assertEqual(
            converted,
            "See [script](https://github.com/QuantStrategyLab/CryptoSnapshotPipelines/blob/codex/monthly-audit-47/scripts/run.py#L42).",
        )

    def test_convert_local_markdown_links_leaves_external_paths(self) -> None:
        repo_dir = Path("/tmp/selfhosted-codex-audit-abc/source")
        body = "See [outside](/tmp/other/source/scripts/run.py:42)."

        self.assertEqual(
            convert_local_markdown_links(body, repo_dir, "QuantStrategyLab/CryptoSnapshotPipelines", "main"),
            body,
        )

    def test_package_import_name_normalizes_common_specs(self) -> None:
        self.assertEqual(package_import_name("pandas>=3.0"), "pandas")
        self.assertEqual(package_import_name("PyYAML==6.0"), "yaml")

    def test_bootstrap_packages_uses_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bootstrap_packages(), ["pandas"])

    def test_extract_openai_text_reads_chat_completion_content(self) -> None:
        response = {"choices": [{"message": {"content": "review"}}]}
        self.assertEqual(extract_openai_text(response), "review")

    def test_extract_anthropic_text_reads_content_blocks(self) -> None:
        response = {"content": [{"type": "text", "text": "review"}, {"type": "text", "text": "second"}]}
        self.assertEqual(extract_anthropic_text(response), "review\n\nsecond")

    def test_build_api_review_prompt_includes_source_context(self) -> None:
        prompt = build_api_review_prompt(
            "QuantStrategyLab/CryptoSnapshotPipelines",
            "main",
            {"title": "Monthly Report", "body": "Body", "html_url": "https://example.test/issue"},
            [],
        )
        self.assertIn("QuantStrategyLab/CryptoSnapshotPipelines", prompt)
        self.assertIn("Monthly Report", prompt)
        self.assertIn("API Monthly Review", prompt)

    def test_build_api_review_prompt_supports_long_horizon_task(self) -> None:
        prompt = build_api_review_prompt(
            "QuantStrategyLab/AiLongHorizonSignalPipelines",
            "main",
            {"title": "Shadow Signal", "body": "Body", "html_url": "https://example.test/issue"},
            [],
            task="long_horizon_signal_shadow",
        )

        self.assertIn("QuantStrategyLab/AiLongHorizonSignalPipelines", prompt)
        self.assertIn("API Long-Horizon Shadow Signal Review", prompt)
        self.assertIn("Draft Shadow Signal JSON", prompt)

    def test_auto_fallback_missing_api_key_message_mentions_reason(self) -> None:
        message = auto_fallback_missing_api_key_message("Codex setup failed.")
        self.assertIn("Codex setup failed.", message)
        self.assertIn("OPENAI_API_KEY", message)
        self.assertIn("ANTHROPIC_API_KEY", message)
        self.assertIn("No files were pushed", message)
        self.assertNotIn("provider `auto`", message)

    def test_run_configured_api_reviews_uses_both_configured_reviewers(self) -> None:
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "openai-key", "ANTHROPIC_API_KEY": "anthropic-key"}, clear=True),
            patch("scripts.run_monthly_codex_audit.run_openai_review", return_value="openai review"),
            patch("scripts.run_monthly_codex_audit.run_anthropic_review", return_value="anthropic review"),
        ):
            reviews, warnings = run_configured_api_reviews(
                "QuantStrategyLab/CryptoSnapshotPipelines",
                "main",
                {"title": "Monthly Report", "body": "Body"},
                [],
            )

        self.assertEqual(reviews, [("OpenAI", "openai review"), ("Anthropic Claude", "anthropic review")])
        self.assertEqual(warnings, [])

    def test_run_configured_api_reviews_reports_missing_optional_reviewer(self) -> None:
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "openai-key"}, clear=True),
            patch("scripts.run_monthly_codex_audit.run_openai_review", return_value="openai review"),
        ):
            reviews, warnings = run_configured_api_reviews(
                "QuantStrategyLab/CryptoSnapshotPipelines",
                "main",
                {"title": "Monthly Report", "body": "Body"},
                [],
            )

        self.assertEqual(reviews, [("OpenAI", "openai review")])
        self.assertEqual(warnings, ["Anthropic Claude fallback skipped because `ANTHROPIC_API_KEY` is not configured."])

    def test_format_api_review_comment_combines_reviews(self) -> None:
        message = format_api_review_comment(
            "Codex failed.",
            [("OpenAI", "openai review"), ("Anthropic Claude", "anthropic review")],
            ["Anthropic warning"],
        )
        self.assertIn("## API Monthly Review", message)
        self.assertIn("### OpenAI Review", message)
        self.assertIn("### Anthropic Claude Review", message)
        self.assertIn("Anthropic warning", message)

    def test_pr_closing_line_only_closes_long_horizon_signal_issues(self) -> None:
        self.assertEqual(pr_closing_line("long_horizon_signal_shadow", 4), "Closes #4")
        self.assertEqual(pr_closing_line("monthly_snapshot_audit", 4), "")


if __name__ == "__main__":
    unittest.main()
