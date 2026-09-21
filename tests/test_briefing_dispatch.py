from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from service.briefing_consumer import (
    BriefingAction,
    BriefingConsumptionResult,
    BriefingFinding,
    consume_briefing_report,
)
from service.briefing_dispatch import (
    create_github_issue,
    dispatch_briefing_result,
    sender_prerequisites,
    send_telegram_alert,
)


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

    @patch("service.briefing_dispatch.dispatch_strategy_watch_findings")
    def test_strategy_health_dispatches_to_issue_only_watcher(self, dispatch_findings) -> None:
        dispatch_findings.return_value = {
            "status": "ok",
            "findings": 1,
            "issues": [{"repo": "QuantStrategyLab/UsEquityStrategies", "created": True}],
            "errors": 0,
        }
        findings = consume_briefing_report(
            {
                "domain": "us_equity",
                "strategies": [
                    {
                        "strategy_profile": "global_etf_rotation",
                        "status": "critical",
                        "overall_score": 14.2,
                        "performance_score": 0.0,
                    }
                ],
            }
        )
        result = BriefingConsumptionResult(day="2026-07-30", report_dir="/tmp", findings=findings)

        summary = dispatch_briefing_result(result)

        self.assertEqual(summary["action"], "github_issue")
        self.assertFalse(summary["telegram_sent"])
        self.assertEqual(summary["optimization_watch"]["findings"], 1)
        dispatched = dispatch_findings.call_args.args[0]
        self.assertEqual(dispatched[0].snapshot.repo, "QuantStrategyLab/UsEquityStrategies")
        self.assertEqual(dispatched[0].finding_type, "monitoring_trigger")
        self.assertEqual(summary["errors"], [])

    @patch("service.briefing_dispatch.send_telegram_alert", return_value=True)
    @patch("service.briefing_dispatch.dispatch_strategy_watch_findings")
    def test_strategy_record_failure_falls_back_to_operational_telegram(
        self,
        dispatch_findings,
        send_telegram,
    ) -> None:
        dispatch_findings.return_value = {
            "status": "partial_error",
            "findings": 1,
            "issues": [{"error": "record failed"}],
            "errors": 1,
        }
        findings = consume_briefing_report(
            {
                "domain": "crypto",
                "strategies": [
                    {
                        "strategy_profile": "crypto_live_pool_rotation",
                        "status": "critical",
                        "overall_score": 27.7,
                    }
                ],
            }
        )
        result = BriefingConsumptionResult(day="2026-07-30", report_dir="/tmp", findings=findings)

        with patch.dict(
            os.environ,
            {"TELEGRAM_TOKEN": "token", "GLOBAL_TELEGRAM_CHAT_ID": "123"},
            clear=True,
        ):
            summary = dispatch_briefing_result(result)

        self.assertIn("optimization_record_failed", summary["errors"])
        self.assertTrue(summary["operational_fallback_sent"])
        self.assertIn("optimization-record delivery failure", send_telegram.call_args.kwargs["text"])

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

    @patch(
        "service.briefing_dispatch.subprocess.check_output",
        side_effect=[
            subprocess.CalledProcessError(1, ["gh"], output="label not found"),
            "https://example.test/issues/2\n",
        ],
    )
    @patch("service.briefing_dispatch.shutil_which", return_value="/usr/bin/gh")
    def test_create_github_issue_retries_without_missing_labels(self, _which, check_output) -> None:
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "QuantStrategyLab/CryptoStrategies"}, clear=True):
            issue = create_github_issue(
                title="review disagreement",
                body="details",
                labels=("dual-review", "needs-human"),
            )

        self.assertEqual(issue, "https://example.test/issues/2")
        self.assertIn("--label", check_output.call_args_list[0].args[0])
        self.assertNotIn("--label", check_output.call_args_list[1].args[0])

    @patch("service.briefing_dispatch.subprocess.check_output")
    @patch("service.briefing_dispatch.shutil_which", return_value="/usr/bin/gh")
    def test_create_github_issue_rejects_invalid_repository(self, _which, check_output) -> None:
        for repository in ("bad/repo --assignee admin", "QuantStrategyLab/..", ".hidden/repo"):
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": repository}, clear=True):
                issue = create_github_issue(title="review unavailable", body="details", labels=())
            self.assertIsNone(issue)

        check_output.assert_not_called()

    def test_sender_prerequisites_are_non_secret_booleans(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "TELEGRAM_TOKEN": "secret-token-value",
                    "GLOBAL_TELEGRAM_CHAT_ID": "12345",
                    "QSL_GITHUB_REPO": "QuantStrategyLab/AIAuditBridge",
                },
                clear=True,
            ),
            patch("service.briefing_dispatch.shutil_which", return_value="/usr/bin/gh"),
        ):
            prereqs = sender_prerequisites()
        self.assertEqual(
            prereqs,
            {
                "telegram_token_present": True,
                "telegram_chat_ids_present": True,
                "github_issue_target_valid": True,
                "gh_executable_present": True,
            },
        )
        blob = str(prereqs)
        self.assertNotIn("secret-token-value", blob)
        self.assertNotIn("12345", blob)

    def test_send_dry_run_quiet_preserves_success_without_side_effects(self) -> None:
        result = BriefingConsumptionResult(day="2026-07-08", report_dir="/tmp", findings=[])
        with (
            patch("service.briefing_dispatch.urllib.request.urlopen") as urlopen,
            patch("service.briefing_dispatch.subprocess.check_output") as check_output,
            patch("service.briefing_dispatch.create_github_issue") as create_issue,
            patch("service.briefing_dispatch.send_telegram_alert") as send_tg,
            patch("service.automation_run_ledger.get_automation_run_ledger") as get_ledger,
        ):
            summary = dispatch_briefing_result(result, send_dry_run=True)
        self.assertEqual(summary["action"], "quiet")
        self.assertTrue(summary["send_dry_run"])
        self.assertIn("sender_prerequisites", summary)
        self.assertFalse(summary["telegram_sent"])
        self.assertIsNone(summary["github_issue"])
        self.assertEqual(summary["errors"], [])
        urlopen.assert_not_called()
        check_output.assert_not_called()
        create_issue.assert_not_called()
        send_tg.assert_not_called()
        get_ledger.assert_not_called()

    def test_send_dry_run_telegram_configured_redacts_preview_and_skips_network(self) -> None:
        result = BriefingConsumptionResult(
            day="2026-07-08",
            report_dir="/tmp",
            findings=[
                BriefingFinding(
                    source="us.json",
                    level=BriefingAction.TELEGRAM,
                    reason="drift_score=0.9",
                    strategy_profile="demo-profile",
                )
            ],
        )
        with (
            patch.dict(
                os.environ,
                {
                    "TELEGRAM_TOKEN": "secret-token-value",
                    "GLOBAL_TELEGRAM_CHAT_ID": "999",
                    "QSL_GITHUB_REPO": "QuantStrategyLab/AIAuditBridge",
                },
                clear=True,
            ),
            patch("service.briefing_dispatch.shutil_which", return_value="/usr/bin/gh"),
            patch("service.briefing_dispatch.urllib.request.urlopen") as urlopen,
            patch("service.briefing_dispatch.subprocess.check_output") as check_output,
            patch("service.briefing_dispatch.create_github_issue") as create_issue,
            patch("service.briefing_dispatch.send_telegram_alert") as send_tg,
            patch("service.automation_run_ledger.get_automation_run_ledger") as get_ledger,
        ):
            summary = dispatch_briefing_result(result, send_dry_run=True)

        self.assertTrue(summary["send_dry_run"])
        self.assertEqual(summary["action"], "telegram")
        self.assertTrue(summary["sender_prerequisites"]["telegram_token_present"])
        self.assertTrue(summary["sender_prerequisites"]["telegram_chat_ids_present"])
        self.assertEqual(
            summary["telegram_dry_run"],
            {"present": True, "safe_summary": "telegram_preview_available"},
        )
        self.assertFalse(summary["telegram_sent"])
        self.assertEqual(summary["errors"], [])
        blob = str(summary)
        self.assertNotIn("secret-token-value", blob)
        self.assertNotIn("999", blob)
        self.assertNotIn("demo-profile", blob)
        self.assertNotIn("drift_score=0.9", blob)
        urlopen.assert_not_called()
        check_output.assert_not_called()
        create_issue.assert_not_called()
        send_tg.assert_not_called()
        get_ledger.assert_not_called()

    def test_send_dry_run_telegram_missing_env_fail_closed(self) -> None:
        result = BriefingConsumptionResult(
            day="2026-07-08",
            report_dir="/tmp",
            findings=[
                BriefingFinding(
                    source="us.json",
                    level=BriefingAction.TELEGRAM,
                    reason="circuit_open",
                )
            ],
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("service.briefing_dispatch.shutil_which", return_value="/usr/bin/gh"),
            patch("service.briefing_dispatch.urllib.request.urlopen") as urlopen,
            patch("service.briefing_dispatch.send_telegram_alert") as send_tg,
        ):
            summary = dispatch_briefing_result(result, send_dry_run=True)

        self.assertIn("telegram_missing_env", summary["errors"])
        self.assertFalse(summary["sender_prerequisites"]["telegram_token_present"])
        self.assertFalse(summary["sender_prerequisites"]["telegram_chat_ids_present"])
        self.assertEqual(
            summary["telegram_dry_run"],
            {"present": True, "safe_summary": "telegram_preview_available"},
        )
        urlopen.assert_not_called()
        send_tg.assert_not_called()

    def test_send_dry_run_github_missing_gh_fail_closed(self) -> None:
        result = BriefingConsumptionResult(
            day="2026-07-08",
            report_dir="/tmp",
            findings=[
                BriefingFinding(
                    source="review.json",
                    level=BriefingAction.GITHUB_ISSUE,
                    reason="needs_human_review",
                    kind="engineering_review",
                )
            ],
        )
        with (
            patch.dict(
                os.environ,
                {"QSL_GITHUB_REPO": "QuantStrategyLab/AIAuditBridge"},
                clear=True,
            ),
            patch("service.briefing_dispatch.shutil_which", return_value=None),
            patch("service.briefing_dispatch.subprocess.check_output") as check_output,
            patch("service.briefing_dispatch.create_github_issue") as create_issue,
            patch("service.briefing_dispatch.urllib.request.urlopen") as urlopen,
        ):
            summary = dispatch_briefing_result(result, send_dry_run=True)

        self.assertIn("gh_executable_missing", summary["errors"])
        self.assertFalse(summary["sender_prerequisites"]["gh_executable_present"])
        self.assertTrue(summary["sender_prerequisites"]["github_issue_target_valid"])
        self.assertEqual(
            summary["github_dry_run"],
            {"present": True, "safe_summary": "github_issue_preview_available"},
        )
        blob = str(summary)
        self.assertNotIn("needs_human_review", blob)
        self.assertNotIn("[briefing]", blob)
        check_output.assert_not_called()
        create_issue.assert_not_called()
        urlopen.assert_not_called()

    def test_send_dry_run_github_invalid_target_fail_closed(self) -> None:
        result = BriefingConsumptionResult(
            day="2026-07-08",
            report_dir="/tmp",
            findings=[
                BriefingFinding(
                    source="review.json",
                    level=BriefingAction.GITHUB_ISSUE,
                    reason="ordinary_source_review",
                    kind="engineering_review",
                )
            ],
        )
        with (
            patch.dict(
                os.environ,
                {"QSL_GITHUB_REPO": "bad/repo --assignee admin"},
                clear=True,
            ),
            patch("service.briefing_dispatch.shutil_which", return_value="/usr/bin/gh"),
            patch("service.briefing_dispatch.create_github_issue") as create_issue,
        ):
            summary = dispatch_briefing_result(result, send_dry_run=True)

        self.assertIn("github_issue_target_invalid", summary["errors"])
        self.assertFalse(summary["sender_prerequisites"]["github_issue_target_valid"])
        self.assertNotIn("gh_executable_missing", summary["errors"])
        create_issue.assert_not_called()

    def test_ordinary_dry_run_unchanged_keeps_preview_text(self) -> None:
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
        with patch.dict(os.environ, {}, clear=True):
            summary = dispatch_briefing_result(result, dry_run=True)
        self.assertNotIn("send_dry_run", summary)
        self.assertIn("telegram_dry_run", summary)
        self.assertIsInstance(summary["telegram_dry_run"], str)
        self.assertIn("demo", summary["telegram_dry_run"])
        self.assertEqual(summary["errors"], [])


if __name__ == "__main__":
    unittest.main()
