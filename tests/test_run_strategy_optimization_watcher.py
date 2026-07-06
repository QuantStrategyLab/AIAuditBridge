from __future__ import annotations

import tempfile
import unittest

from scripts.run_strategy_optimization_watcher import parse_bool, resolve_input_path, run_watcher


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
            find_issue=lambda repo, title: "",
        )

        self.assertEqual(result["findings"], 1)
        self.assertTrue(result["issues"][0]["dry_run"])
        self.assertEqual(calls, [])

    def test_non_dry_run_uses_source_repo_override(self) -> None:
        calls: list[tuple[str, str, str]] = []

        result = run_watcher(
            {
                "repo": "QuantStrategyLab/MetricSource",
                "profile": "live",
                "current_metrics": {"max_dd": 0.2},
                "baseline_metrics": {"max_dd": 0.1},
            },
            source_repo="QuantStrategyLab/IssueRepo",
            dry_run=False,
            create_issue=lambda repo, title, body: calls.append((repo, title, body)) or "https://example.test/issue/1",
            find_issue=lambda repo, title: "",
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
            find_issue=lambda repo, title: "https://example.test/existing",
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

    def test_parse_bool_defaults_safely(self) -> None:
        self.assertTrue(parse_bool("true"))
        self.assertFalse(parse_bool("false"))
        self.assertTrue(parse_bool(None, default=True))


if __name__ == "__main__":
    unittest.main()
