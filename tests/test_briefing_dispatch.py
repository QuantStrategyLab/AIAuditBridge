from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from service.briefing_consumer import BriefingAction, BriefingConsumptionResult, BriefingFinding
from service.briefing_dispatch import create_github_issue, dispatch_briefing_result, send_telegram_alert


class BriefingDispatchTests(unittest.TestCase):
    def test_dispatch_quiet_skips(self) -> None:
        result = BriefingConsumptionResult(day="2026-07-08", report_dir="/tmp", findings=[])
        summary = dispatch_briefing_result(result)
        self.assertEqual(summary["action"], "quiet")
        self.assertIn("quiet", summary["skipped"])

    def test_dispatch_telegram_dry_run(self) -> None:
        result = BriefingConsumptionResult(
            day="2026-07-08",
            report_dir="/tmp",
            findings=[
                BriefingFinding(
                    source="us.json",
                    level=BriefingAction.TELEGRAM,
                    reason="drift_score=0.9",
                    strategy_profile="demo",
                )
            ],
        )
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "token", "GLOBAL_TELEGRAM_CHAT_ID": "123"}):
            summary = dispatch_briefing_result(result, dry_run=True)
        self.assertIn("telegram_dry_run", summary)
        self.assertIn("demo", summary["telegram_dry_run"])

    @patch("service.briefing_dispatch.urllib.request.urlopen")
    def test_send_telegram_alert_success(self, mock_urlopen) -> None:
        class _Resp:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        mock_urlopen.return_value = _Resp()
        ok = send_telegram_alert(text="hello", token="tok", chat_ids=("123",))
        self.assertTrue(ok)

    @patch("service.briefing_dispatch.subprocess.check_output", return_value="https://example.test/issues/1\n")
    @patch("service.briefing_dispatch.shutil_which", return_value="/usr/bin/gh")
    def test_create_github_issue_uses_actions_repository(self, _which, check_output) -> None:
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "QuantStrategyLab/CryptoStrategies"}, clear=True):
            issue = create_github_issue(title="review unavailable", body="details", labels=())

        self.assertEqual(issue, "https://example.test/issues/1")
        self.assertIn("QuantStrategyLab/CryptoStrategies", check_output.call_args.args[0])

    @patch("service.briefing_dispatch.subprocess.check_output")
    @patch("service.briefing_dispatch.shutil_which", return_value="/usr/bin/gh")
    def test_create_github_issue_rejects_invalid_repository(self, _which, check_output) -> None:
        for repository in ("bad/repo --assignee admin", "QuantStrategyLab/..", ".hidden/repo"):
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": repository}, clear=True):
                issue = create_github_issue(title="review unavailable", body="details", labels=())
            self.assertIsNone(issue)

        check_output.assert_not_called()


if __name__ == "__main__":
    unittest.main()
