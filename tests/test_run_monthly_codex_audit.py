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
    extract_openai_text,
    package_import_name,
    parse_bool,
    safe_branch_component,
    strip_audit_heading,
    validate_provider,
    validate_repo,
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

    def test_validate_repo_rejects_invalid_values(self) -> None:
        with self.assertRaises(Exception):
            validate_repo("QuantStrategyLab/CryptoSnapshotPipelines/extra")
        with self.assertRaises(Exception):
            validate_repo("OtherOrg/CryptoSnapshotPipelines")

    def test_validate_provider_accepts_supported_values(self) -> None:
        self.assertEqual(validate_provider(""), "auto")
        self.assertEqual(validate_provider("codex"), "codex")
        self.assertEqual(validate_provider("OPENAI"), "openai")
        self.assertEqual(validate_provider("auto"), "auto")
        with self.assertRaises(Exception):
            validate_provider("claude")

    def test_safe_branch_component_removes_unsafe_characters(self) -> None:
        self.assertEqual(safe_branch_component("issue #12: monthly review"), "issue-12-monthly-review")

    def test_blocked_paths_blocks_data_and_secret_like_files(self) -> None:
        blocked = blocked_paths(["data/output/report.json", "docs/secret-token.md", "scripts/fix.py"])
        self.assertEqual(blocked, ["data/output/report.json", "docs/secret-token.md"])

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


if __name__ == "__main__":
    unittest.main()
