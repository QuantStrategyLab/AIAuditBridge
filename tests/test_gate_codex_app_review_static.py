from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC_MODULE_PATH = ROOT / "scripts" / "gate_codex_app_review_static.py"
STATIC_SPEC = importlib.util.spec_from_file_location("gate_codex_app_review_static", STATIC_MODULE_PATH)
gate_codex_app_review_static = importlib.util.module_from_spec(STATIC_SPEC)
assert STATIC_SPEC.loader is not None
sys.modules[STATIC_SPEC.name] = gate_codex_app_review_static
STATIC_SPEC.loader.exec_module(gate_codex_app_review_static)


class GateCodexAppReviewStaticTest(unittest.TestCase):

    def test_scan_diff_does_not_echo_secret_values(self) -> None:
        diff = "\n".join(
            [
                "diff --git a/example.py b/example.py",
                "+++ b/example.py",
                '+api_key = "sk-' 'live-12345678901234567890"',
            ]
        )

        violations = gate_codex_app_review_static.scan_diff(diff, [])

        self.assertEqual(len(violations), 1)
        self.assertIn("api_key=<redacted>", violations[0])
        self.assertNotIn("sk-live-12345678901234567890", violations[0])

    def test_collect_static_gate_issues_aggregates_metadata_and_diff(self) -> None:
        files = [{"filename": "src/main.py", "status": "modified", "additions": 2, "deletions": 0}]
        policy = gate_codex_app_review_static.load_policy()
        policy["max_changed_lines"] = 1
        diff = "\n".join(
            [
                "diff --git a/src/main.py b/src/main.py",
                "+++ b/src/main.py",
                '+api_key = "sk-' 'live-12345678901234567890"',
            ]
        )

        issues = gate_codex_app_review_static.collect_static_gate_issues(files, diff, policy)

        self.assertTrue(any("Hardcoded secret" in issue for issue in issues))
        self.assertTrue(any("Too many lines" in issue for issue in issues))

    def test_check_metadata_allows_only_an_exact_base_approved_change_bundle(self) -> None:
        files = [
            {
                "filename": "scripts/retired.py",
                "status": "removed",
                "additions": 0,
                "deletions": 150,
            },
            {
                "filename": "service/caller.py",
                "status": "modified",
                "additions": 2_400,
                "deletions": 0,
            },
        ]
        policy = {
            "max_changed_files": 10,
            "max_changed_lines": 2_000,
            "approved_change_bundles": [
                {
                    "exact_changed_paths": [
                        "scripts/retired.py",
                        "service/caller.py",
                    ],
                    "exact_deleted_paths": ["scripts/retired.py"],
                    "max_changed_lines": 3_000,
                }
            ],
        }

        issues = gate_codex_app_review_static.check_metadata(files, policy)

        self.assertEqual(issues, [])

    def test_check_metadata_does_not_expand_line_budget_for_an_unrelated_change(self) -> None:
        files = [
            {
                "filename": "service/unrelated.py",
                "status": "modified",
                "additions": 2_500,
                "deletions": 0,
            },
        ]
        policy = {
            "max_changed_files": 10,
            "max_changed_lines": 2_000,
            "approved_change_bundles": [
                {
                    "exact_changed_paths": [
                        "scripts/retired.py",
                        "service/caller.py",
                    ],
                    "exact_deleted_paths": ["scripts/retired.py"],
                    "max_changed_lines": 3_000,
                }
            ],
        }

        issues = gate_codex_app_review_static.check_metadata(files, policy)

        self.assertTrue(any("Too many lines" in issue for issue in issues))

    def test_check_metadata_rejects_partial_or_unsafe_change_bundles(self) -> None:
        files = [
            {
                "filename": "scripts/retired.py",
                "status": "removed",
                "additions": 0,
                "deletions": 10,
            },
        ]
        for exact_changed_paths in (
            ["scripts/retired.py", "service/caller.py"],
            ["scripts/*.py"],
            ["../scripts/retired.py"],
            ["/scripts/retired.py"],
            ["scripts\\retired.py"],
        ):
            with self.subTest(exact_changed_paths=exact_changed_paths):
                policy = {
                    "max_changed_files": 10,
                    "max_changed_lines": 100,
                    "approved_change_bundles": [
                        {
                            "exact_changed_paths": exact_changed_paths,
                            "exact_deleted_paths": ["scripts/retired.py"],
                            "max_changed_lines": 200,
                        }
                    ],
                }
                issues = gate_codex_app_review_static.check_metadata(files, policy)
                self.assertTrue(any("scripts/retired.py" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
