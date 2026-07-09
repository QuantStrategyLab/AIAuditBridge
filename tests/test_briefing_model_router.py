from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from service.briefing_consumer import (
    BriefingAction,
    consume_briefing_dir,
    consume_briefing_report,
)
from service.model_resolver import reset_catalog_cache
from service.model_router import list_task_routes, route_model


class ModelRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo_catalog = Path(__file__).resolve().parents[1] / "generated" / "model_catalog.json"
        if not repo_catalog.is_file():
            raise unittest.SkipTest("generated/model_catalog.json missing")
        cls._catalog_path = str(repo_catalog)

    def setUp(self) -> None:
        reset_catalog_cache()
        os.environ["MODEL_CATALOG_PATH"] = self._catalog_path

    def tearDown(self) -> None:
        reset_catalog_cache()
        os.environ.pop("MODEL_CATALOG_PATH", None)

    def test_daily_briefing_uses_nano_tier(self) -> None:
        route = route_model("daily_briefing")
        self.assertEqual(route["tier"], "nano")
        self.assertEqual(route["effort"], "medium")

    def test_dual_review_uses_flagship_tier(self) -> None:
        route = route_model("dual_review")
        self.assertEqual(route["tier"], "flagship")
        self.assertEqual(route["effort"], "xhigh")

    def test_low_quota_overrides_to_recommend_model(self) -> None:
        with patch("service.model_resolver.tier_for_budget", return_value="nano"):
            route = route_model("dual_review", budget_remaining=0.0, quota_status="low")
        self.assertEqual(route["tier"], "nano")
        self.assertEqual(route["effort"], "low")
        self.assertEqual(route["quota_override"], "low")

    def test_list_task_routes_is_copy(self) -> None:
        routes = list_task_routes()
        routes["daily_briefing"]["model"] = "mutated"
        self.assertNotEqual(route_model("daily_briefing")["model"], "mutated")


class BriefingConsumerTests(unittest.TestCase):
    def test_quiet_when_healthy(self) -> None:
        findings = consume_briefing_report(
            {
                "ok": True,
                "domain": "us_equity",
                "strategies": [
                    {"strategy_profile": "demo", "status": "healthy", "overall_score": 80},
                ],
                "summary": {"healthy": 1, "critical": 0, "review": 0},
            }
        )
        self.assertEqual(findings, [])

    def test_github_issue_for_review_status(self) -> None:
        findings = consume_briefing_report(
            {
                "strategies": [{"strategy_profile": "demo", "status": "review", "overall_score": 50}],
            },
            source="us_equity.json",
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].level, BriefingAction.GITHUB_ISSUE)

    def test_telegram_for_critical_drift(self) -> None:
        findings = consume_briefing_report(
            {
                "strategies": [
                    {"strategy_profile": "demo", "status": "watch", "drift_score": 0.9},
                ],
            }
        )
        self.assertEqual(findings[0].level, BriefingAction.TELEGRAM)

    def test_consume_briefing_dir_reads_files(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "2026-07-08"
            report_dir.mkdir()
            (report_dir / "us_equity.json").write_text(
                json.dumps(
                    {
                        "domain": "us_equity",
                        "summary": {"critical": 1, "review": 0},
                    }
                ),
                encoding="utf-8",
            )
            result = consume_briefing_dir(report_dir)
            self.assertEqual(result.action, BriefingAction.TELEGRAM)
            self.assertEqual(len(result.findings), 1)


if __name__ == "__main__":
    unittest.main()
