from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "gate_codex_app_review.py"
SPEC = importlib.util.spec_from_file_location("gate_codex_app_review", MODULE_PATH)
gate_codex_app_review = importlib.util.module_from_spec(SPEC)
STATIC_MODULE_PATH = ROOT / "scripts" / "gate_codex_app_review_static.py"
STATIC_SPEC = importlib.util.spec_from_file_location("gate_codex_app_review_static", STATIC_MODULE_PATH)
gate_codex_app_review_static = importlib.util.module_from_spec(STATIC_SPEC)
assert SPEC.loader is not None
assert STATIC_SPEC.loader is not None
sys.modules[SPEC.name] = gate_codex_app_review
sys.modules[STATIC_SPEC.name] = gate_codex_app_review_static
SPEC.loader.exec_module(gate_codex_app_review)
STATIC_SPEC.loader.exec_module(gate_codex_app_review_static)


class GateCodexAppReviewTest(unittest.TestCase):
    def test_wrapper_exports_shared_static_functions(self) -> None:
        sample = "diff --git a/example.py b/example.py\n+++ b/example.py\n+api_key= \"x\""
        files = [{"filename": "example.py", "status": "modified", "additions": 0, "deletions": 0}]
        policy = gate_codex_app_review_static.load_policy()
        policy["max_changed_files"] = 1

        self.assertEqual(
            gate_codex_app_review.scan_diff(sample, []),
            gate_codex_app_review_static.scan_diff(sample, []),
        )
        self.assertEqual(
            gate_codex_app_review.compile_patterns(policy),
            gate_codex_app_review_static.compile_patterns(policy),
        )
        self.assertEqual(
            gate_codex_app_review.check_metadata(files, policy),
            gate_codex_app_review_static.check_metadata(files, policy),
        )
        self.assertEqual(
            gate_codex_app_review.load_policy(Path(".github/codex_auto_merge_policy.json")),
            gate_codex_app_review_static.load_policy(Path(".github/codex_auto_merge_policy.json")),
        )

    def test_script_entrypoint_can_run_without_package_context(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH)],
            check=False,
            cwd=ROOT,
            env={},
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("GH_TOKEN + GITHUB_REPOSITORY required", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_wrapper_scan_diff_does_not_echo_secret_values(self) -> None:
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

    def test_scan_diff_does_not_echo_secret_values(self) -> None:
        diff = "\n".join(
            [
                "diff --git a/example.py b/example.py",
                "+++ b/example.py",
                '+api_key = "sk-' 'live-12345678901234567890"',
            ]
        )

        violations = gate_codex_app_review.scan_diff(diff, [])

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


if __name__ == "__main__":
    unittest.main()
