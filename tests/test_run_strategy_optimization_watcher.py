from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_strategy_optimization_watcher import list_open_issue_urls, parse_bool, resolve_input_path, run_watcher


class RunStrategyOptimizationWatcherTest(unittest.TestCase):
    def test_dry_run_does_not_create_issue(self) -> None:
        calls: list[tuple[str, str, str]] = []

        result = run_watcher(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "profile": "live",
                "current_metrics": {"sharpe": 0.5},
                "baseline_metrics": {"sharpe": 1.0},
            },
            dry_run=True,
            create_issue=lambda repo, title, body: calls.append((repo, title, body)) or "https://example.test/1",
            list_issues=lambda repo: {},
        )

        self.assertEqual(result["findings"], 1)
        self.assertTrue(result["issues"][0]["dry_run"])
        self.assertNotIn("metrics", result["issues"][0]["task"]["trigger"])
        self.assertEqual(calls, [])

    def test_non_dry_run_uses_source_repo_override(self) -> None:
        calls: list[tuple[str, str, str]] = []

        result = run_watcher(
            {
                "repo": "QuantStrategyLab/IssueRepo",
                "profile": "live",
                "current_metrics": {"max_dd": 0.2},
                "baseline_metrics": {"max_dd": 0.1},
            },
            source_repo="QuantStrategyLab/IssueRepo",
            dry_run=False,
            create_issue=lambda repo, title, body: calls.append((repo, title, body)) or "https://example.test/issue/1",
            list_issues=lambda repo: {},
        )

        self.assertTrue(result["issues"][0]["created"])
        self.assertEqual(result["issues"][0]["url"], "https://example.test/issue/1")
        self.assertEqual(calls[0][0], "QuantStrategyLab/IssueRepo")

    def test_non_dry_run_skips_existing_open_issue(self) -> None:
        calls: list[tuple[str, str, str]] = []

        result = run_watcher(
            {
                "repo": "QuantStrategyLab/TestStrategies",
                "profile": "live",
                "current_metrics": {"sharpe": 0.5},
                "baseline_metrics": {"sharpe": 1.0},
            },
            dry_run=False,
            create_issue=lambda repo, title, body: calls.append((repo, title, body)) or "https://example.test/new",
            list_issues=lambda repo: {"AI strategy optimization proposal: QuantStrategyLab/TestStrategies:live": "https://example.test/existing"},
        )

        self.assertFalse(result["issues"][0]["created"])
        self.assertEqual(result["issues"][0]["existing_url"], "https://example.test/existing")
        self.assertEqual(calls, [])

    def test_resolve_input_path_rejects_metrics_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                resolve_input_path(source_root=tmp, metrics_path="../outside.json")
            with self.assertRaises(ValueError):
                resolve_input_path(source_root=tmp, metrics_path="/tmp/outside.json")

    def test_resolve_input_path_accepts_source_relative_metrics_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = resolve_input_path(source_root=tmp, metrics_path="data/output/strategy_metrics.json")

        self.assertTrue(str(resolved).endswith("data/output/strategy_metrics.json"))

    def test_find_existing_open_issue_paginates_until_exact_match(self) -> None:
        calls: list[list[str]] = []
        first_page = [{"title": f"other-{i}", "html_url": f"https://example.test/{i}"} for i in range(100)]
        second_page = [{"title": "target", "html_url": "https://example.test/target"}]

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            page = "2" if "page=2" in cmd else "1"
            payload = second_page if page == "2" else first_page
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        with patch("scripts.run_strategy_optimization_watcher.subprocess.run", fake_run):
            issues = list_open_issue_urls("QuantStrategyLab/TestStrategies")

        self.assertEqual(issues["target"], "https://example.test/target")
        self.assertEqual(len(calls), 2)
        self.assertIn("--method", calls[0])
        self.assertIn("GET", calls[0])

    def test_run_watcher_rejects_mismatched_source_repo_payload(self) -> None:
        with self.assertRaises(ValueError):
            run_watcher(
                {
                    "repo": "QuantStrategyLab/Other",
                    "current_metrics": {"sharpe": 0.5},
                    "baseline_metrics": {"sharpe": 1.0},
                },
                source_repo="QuantStrategyLab/TestStrategies",
            )

    def test_run_watcher_uses_validated_source_repo_in_task_summary(self) -> None:
        result = run_watcher(
            {
                "profile": "live",
                "current_metrics": {"sharpe": 0.5},
                "baseline_metrics": {"sharpe": 1.0},
            },
            source_repo="QuantStrategyLab/TestStrategies",
            dry_run=True,
        )

        self.assertIn("QuantStrategyLab/TestStrategies:live", result["issues"][0]["title"])
        self.assertEqual(result["issues"][0]["task"]["proposed_action"]["target"], "QuantStrategyLab/TestStrategies")

    def test_run_watcher_updates_cache_after_create(self) -> None:
        create_calls: list[tuple[str, str, str]] = []
        payload = {
            "repo": "QuantStrategyLab/TestStrategies",
            "snapshots": [
                {"profile": "same", "current_metrics": {"sharpe": 0.5}, "baseline_metrics": {"sharpe": 1.0}},
                {"profile": "same", "current_metrics": {"sharpe": 0.4}, "baseline_metrics": {"sharpe": 1.0}},
            ],
        }

        result = run_watcher(
            payload,
            dry_run=False,
            create_issue=lambda repo, title, body: create_calls.append((repo, title, body)) or "https://example.test/new",
            list_issues=lambda repo: {},
        )

        self.assertEqual(result["findings"], 2)
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(result["issues"][1]["existing_url"], "https://example.test/new")

    def test_run_watcher_caches_open_issues_per_repo(self) -> None:
        list_calls: list[str] = []
        create_calls: list[tuple[str, str, str]] = []
        payload = {
            "repo": "QuantStrategyLab/TestStrategies",
            "snapshots": [
                {"profile": "a", "current_metrics": {"sharpe": 0.5}, "baseline_metrics": {"sharpe": 1.0}},
                {"profile": "b", "current_metrics": {"sharpe": 0.4}, "baseline_metrics": {"sharpe": 1.0}},
            ],
        }

        result = run_watcher(
            payload,
            dry_run=False,
            create_issue=lambda repo, title, body: create_calls.append((repo, title, body)) or "https://example.test/new",
            list_issues=lambda repo: list_calls.append(repo) or {},
        )

        self.assertEqual(result["findings"], 2)
        self.assertEqual(list_calls, ["QuantStrategyLab/TestStrategies"])
        self.assertEqual(len(create_calls), 2)

    def test_list_open_issue_urls_fails_closed_on_bad_json(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout="not-json", stderr="")

        with patch("scripts.run_strategy_optimization_watcher.subprocess.run", fake_run):
            with self.assertRaises(RuntimeError):
                list_open_issue_urls("QuantStrategyLab/TestStrategies")

    def test_parse_bool_defaults_safely(self) -> None:
        self.assertTrue(parse_bool("true"))
        self.assertFalse(parse_bool("false"))
        self.assertTrue(parse_bool(None, default=True))
        with self.assertRaises(ValueError):
            parse_bool("flase")


if __name__ == "__main__":
    unittest.main()
