from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from service.briefing_consumer import (
    BriefingAction,
    consume_briefing_dir,
    consume_briefing_report,
)
from service.model_router import list_task_routes, route_model


class ModelRouterTests(unittest.TestCase):
    def test_daily_briefing_uses_nano(self) -> None:
        route = route_model("daily_briefing")
        self.assertEqual(route["model"], "gpt-4.1-nano")
        self.assertEqual(route["effort"], "medium")

    def test_dual_review_uses_xhigh(self) -> None:
        route = route_model("dual_review")
        self.assertEqual(route["model"], "gpt-5.5")
        self.assertEqual(route["effort"], "xhigh")

    def test_low_quota_overrides_to_recommend_model(self) -> None:
        with patch("service.model_router.recommend_model", return_value="gpt-5.4-mini"):
            route = route_model("dual_review", budget_remaining=0.0, quota_status="low")
        self.assertEqual(route["model"], "gpt-5.4-mini")
        self.assertEqual(route["effort"], "low")
        self.assertEqual(route["quota_override"], "low")

    def test_list_task_routes_is_copy(self) -> None:
        routes = list_task_routes()
        routes["daily_briefing"]["model"] = "mutated"
        self.assertEqual(route_model("daily_briefing")["model"], "gpt-4.1-nano")


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
