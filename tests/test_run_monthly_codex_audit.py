from __future__ import annotations

import unittest

from scripts.run_monthly_codex_audit import blocked_paths, parse_bool, safe_branch_component, validate_repo


class RunMonthlyCodexAuditTests(unittest.TestCase):
    def test_parse_bool_accepts_common_true_values(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on", True):
            self.assertTrue(parse_bool(value))
        for value in ("", "false", "0", "no", False, None):
            self.assertFalse(parse_bool(value))

    def test_validate_repo_accepts_owner_repo(self) -> None:
        self.assertEqual(validate_repo("QuantStrategyLab/CryptoSnapshotPipelines"), "QuantStrategyLab/CryptoSnapshotPipelines")

    def test_validate_repo_rejects_invalid_values(self) -> None:
        with self.assertRaises(Exception):
            validate_repo("QuantStrategyLab/CryptoSnapshotPipelines/extra")

    def test_safe_branch_component_removes_unsafe_characters(self) -> None:
        self.assertEqual(safe_branch_component("issue #12: monthly review"), "issue-12-monthly-review")

    def test_blocked_paths_blocks_data_and_secret_like_files(self) -> None:
        blocked = blocked_paths(["data/output/report.json", "docs/secret-token.md", "scripts/fix.py"])
        self.assertEqual(blocked, ["data/output/report.json", "docs/secret-token.md"])


if __name__ == "__main__":
    unittest.main()

