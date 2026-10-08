from __future__ import annotations

import builtins
import importlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quant_monitor_domain.briefing_consumer import (
    BriefingAction,
    BriefingConsumptionResult,
    BriefingFinding,
    consume_briefing_report,
)
from quant_monitor_domain.briefing_dispatch import (
    create_github_issue,
    dispatch_briefing_result,
    dispatch_runtime_digest,
    sender_prerequisites,
    send_telegram_alert,
)


class BriefingDispatchTests(unittest.TestCase):
    def test_runtime_digest_does_not_import_optional_strategy_watcher(self) -> None:
        from quant_monitor_domain import briefing_dispatch

        original_import = builtins.__import__

        def reject_watcher(name, *args, **kwargs):
            if name.startswith("quant_platform_kit.strategy_lifecycle.watch"):
                raise ModuleNotFoundError(name)
            return original_import(name, *args, **kwargs)

        projection = {
            "platform": "longbridge",
            "observed_at": "2026-09-28T08:40:00Z",
            "completeness": "complete",
            "read_errors": [],
            "unmatched_reports": [],
            "records": [{
                "platform": "longbridge",
                "target_key": "synthetic-service|rot|paper",
                "target": {"service": "synthetic-service", "strategy_profile": "rot", "account_scope": "paper"},
                "business_date": "2026-09-28",
                "timezone": "Asia/Hong_Kong",
                "status": "market_closed",
                "kind": "schedule",
                "completeness": "complete",
                "execution_lane": "paper",
                "runs": [],
                "conflicts": [],
                "fills": {"source": "not_connected", "records": [], "count": None},
            }],
        }

        with patch("builtins.__import__", side_effect=reject_watcher):
            importlib.reload(briefing_dispatch)
            summary = briefing_dispatch.dispatch_runtime_digest(projection, dry_run=True)

        self.assertEqual(summary["action"], "runtime_digest")
        self.assertIn("dry_run", summary["skipped"])

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
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "GLOBAL_TELEGRAM_CHAT_ID": "123",
                "QUANT_MONITOR_ROOT": tmp,
            },
        ):
            summary = dispatch_briefing_result(result, dry_run=True)
            self.assertIn("telegram_dry_run", summary)
            self.assertIn("demo", summary["telegram_dry_run"])
            self.assertFalse((Path(tmp) / "data" / "alert-state" / "health_cycle.json").exists())

    @patch("quant_monitor_domain.briefing_dispatch.dispatch_strategy_watch_findings")
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

    @patch("quant_monitor_domain.briefing_dispatch.dispatch_strategy_watch_findings")
    def test_strategy_record_failure_falls_back_to_operational_telegram(
        self,
        dispatch_findings,
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

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "GLOBAL_TELEGRAM_CHAT_ID": "123",
                "QUANT_MONITOR_ROOT": tmp,
            },
            clear=True,
        ), patch(
            "quant_monitor_domain.briefing_dispatch.telegram_target_outcome",
            return_value="sent",
        ) as send_target:
            summary = dispatch_briefing_result(result)
            self.assertIn("optimization_record_failed", summary["errors"])
            self.assertTrue(summary["operational_fallback_sent"])
            message = send_target.call_args.kwargs["text"]
            self.assertIn("optimization-record delivery failed", message)
            self.assertIn("operational recovery is required", message)
            self.assertNotIn("manual review required", message)
            state = (Path(tmp) / "data" / "alert-state" / "health_cycle.json").read_text(encoding="utf-8")
            self.assertNotIn("123", state)
            self.assertNotIn("token", state)

    @patch("quant_monitor_domain.briefing_dispatch.urllib.request.urlopen")
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

    def test_send_telegram_alert_still_attempts_later_targets_after_failure(self) -> None:
        calls: list[str] = []

        def outcome(*, text: str, token: str, chat_id: str) -> str:
            calls.append(chat_id)
            return "failed" if chat_id == "first" else "sent"

        with patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome", side_effect=outcome):
            ok = send_telegram_alert(text="hello", token="tok", chat_ids=("first", "second"))

        self.assertFalse(ok)
        self.assertEqual(calls, ["first", "second"])

    @patch("quant_monitor_domain.briefing_dispatch.subprocess.check_output", return_value="https://example.test/issues/1\n")
    @patch("quant_monitor_domain.briefing_dispatch.shutil_which", return_value="/usr/bin/gh")
    def test_create_github_issue_uses_actions_repository(self, _which, check_output) -> None:
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "QuantStrategyLab/CryptoStrategies"}, clear=True):
            issue = create_github_issue(title="review unavailable", body="details", labels=())

        self.assertEqual(issue, "https://example.test/issues/1")
        self.assertIn("QuantStrategyLab/CryptoStrategies", check_output.call_args.args[0])

    @patch(
        "quant_monitor_domain.briefing_dispatch.subprocess.check_output",
        side_effect=[
            subprocess.CalledProcessError(1, ["gh"], output="label not found"),
            "https://example.test/issues/2\n",
        ],
    )
    @patch("quant_monitor_domain.briefing_dispatch.shutil_which", return_value="/usr/bin/gh")
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

    @patch("quant_monitor_domain.briefing_dispatch.subprocess.check_output")
    @patch("quant_monitor_domain.briefing_dispatch.shutil_which", return_value="/usr/bin/gh")
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
            patch("quant_monitor_domain.briefing_dispatch.shutil_which", return_value="/usr/bin/gh"),
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
            patch("quant_monitor_domain.briefing_dispatch.urllib.request.urlopen") as urlopen,
            patch("quant_monitor_domain.briefing_dispatch.subprocess.check_output") as check_output,
            patch("quant_monitor_domain.briefing_dispatch.create_github_issue") as create_issue,
            patch("quant_monitor_domain.briefing_dispatch.send_telegram_alert") as send_tg,
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
            patch("quant_monitor_domain.briefing_dispatch.shutil_which", return_value="/usr/bin/gh"),
            patch("quant_monitor_domain.briefing_dispatch.urllib.request.urlopen") as urlopen,
            patch("quant_monitor_domain.briefing_dispatch.subprocess.check_output") as check_output,
            patch("quant_monitor_domain.briefing_dispatch.create_github_issue") as create_issue,
            patch("quant_monitor_domain.briefing_dispatch.send_telegram_alert") as send_tg,
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
            patch("quant_monitor_domain.briefing_dispatch.shutil_which", return_value="/usr/bin/gh"),
            patch("quant_monitor_domain.briefing_dispatch.urllib.request.urlopen") as urlopen,
            patch("quant_monitor_domain.briefing_dispatch.send_telegram_alert") as send_tg,
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
            patch("quant_monitor_domain.briefing_dispatch.shutil_which", return_value=None),
            patch("quant_monitor_domain.briefing_dispatch.subprocess.check_output") as check_output,
            patch("quant_monitor_domain.briefing_dispatch.create_github_issue") as create_issue,
            patch("quant_monitor_domain.briefing_dispatch.urllib.request.urlopen") as urlopen,
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
            patch("quant_monitor_domain.briefing_dispatch.shutil_which", return_value="/usr/bin/gh"),
            patch("quant_monitor_domain.briefing_dispatch.create_github_issue") as create_issue,
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

    def test_missing_monitor_root_does_not_send_or_count_delivered(self) -> None:
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
        with patch.dict(
            os.environ,
            {"TELEGRAM_TOKEN": "token", "GLOBAL_TELEGRAM_CHAT_ID": "123"},
            clear=True,
        ), patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome") as send_target:
            summary = dispatch_briefing_result(result)
        self.assertFalse(summary["telegram_sent"])
        self.assertIn("alert_state_root_unavailable", summary["errors"])
        send_target.assert_not_called()

    def test_reentry_sends_only_the_failed_target(self) -> None:
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
        calls: list[str] = []

        def send_target(*, text: str, token: str, chat_id: str) -> str:
            calls.append(chat_id)
            return "sent" if chat_id == "ok-chat" else "failed"

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "GLOBAL_TELEGRAM_CHAT_ID": "ok-chat,bad-chat",
                "QUANT_MONITOR_ROOT": tmp,
            },
            clear=True,
        ), patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome", side_effect=send_target):
            first = dispatch_briefing_result(result)
            second_calls: list[str] = []

            def retry(*, text: str, token: str, chat_id: str) -> str:
                second_calls.append(chat_id)
                return "sent"

            with patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome", side_effect=retry):
                second = dispatch_briefing_result(result)
        self.assertFalse(first["telegram_sent"])
        self.assertEqual(calls, ["ok-chat", "bad-chat"])
        self.assertIn("telegram_delivery_failed", first["errors"])
        self.assertEqual(second_calls, ["bad-chat"])
        self.assertTrue(second["telegram_sent"])
        self.assertNotIn("telegram_delivery_unknown", second["errors"])

    def test_runtime_digest_retries_only_failed_target_and_not_unknown(self) -> None:
        projection = {
            "platform": "longbridge",
            "observed_at": "2026-09-28T08:40:00+00:00",
            "completeness": "complete",
            "read_errors": [],
            "unmatched_reports": [],
            "records": [{
                "platform": "longbridge",
                "target_key": "lb-svc|rot|paper",
                "target": {"service": "lb-svc", "strategy_profile": "rot", "account_scope": "paper"},
                "business_date": "2026-09-28",
                "timezone": "Asia/Hong_Kong",
                "status": "no_submission",
                "completeness": "complete",
                "execution_lane": "paper",
                "runs": [],
                "conflicts": [],
                "fills": {"source": "not_connected", "records": [], "count": None},
            }],
        }
        calls: list[str] = []

        def send_target(*, text: str, token: str, chat_id: str) -> str:
            calls.append(chat_id)
            return "sent" if chat_id == "ok-chat" else "failed"

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "GLOBAL_TELEGRAM_CHAT_ID": "ok-chat,bad-chat",
                "QUANT_MONITOR_ROOT": tmp,
            },
            clear=True,
        ), patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome", side_effect=send_target):
            first = dispatch_runtime_digest(projection)
            calls.clear()

            def retry(*, text: str, token: str, chat_id: str) -> str:
                calls.append(chat_id)
                return "sent"

            with patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome", side_effect=retry):
                second = dispatch_runtime_digest(projection)
            later = dict(projection)
            later["observed_at"] = "2026-09-28T12:00:00+00:00"
            third = dispatch_runtime_digest(later)
        self.assertFalse(first["telegram_sent"])
        self.assertEqual(calls, ["bad-chat"])
        self.assertTrue(second["telegram_sent"])
        self.assertTrue(third["telegram_sent"] is False)
        self.assertIn("duplicate_delivered", third["skipped"])

        unknown_projection = dict(projection)
        unknown_projection["records"] = [dict(projection["records"][0], business_date="2026-09-29", target_key="lb-svc|rot|live", target={"service": "lb-svc", "strategy_profile": "rot", "account_scope": "live"})]
        unknown_calls: list[str] = []

        def unknown(*, text: str, token: str, chat_id: str) -> str:
            unknown_calls.append(chat_id)
            return "unknown"

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "GLOBAL_TELEGRAM_CHAT_ID": "chat",
                "QUANT_MONITOR_ROOT": tmp,
            },
            clear=True,
        ), patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome", side_effect=unknown):
            first_unknown = dispatch_runtime_digest(unknown_projection)
            unknown_calls.clear()
            second_unknown = dispatch_runtime_digest(unknown_projection)
        self.assertIn("telegram_delivery_unknown", first_unknown["errors"])
        self.assertEqual(unknown_calls, [])
        self.assertFalse(second_unknown["telegram_sent"])

    def test_runtime_digest_write_failure_and_overlong_text_send_nothing(self) -> None:
        projection = {
            "platform": "longbridge",
            "observed_at": "2026-09-28T08:40:00+00:00",
            "completeness": "incomplete",
            "read_errors": ["gs://bucket/private"],
            "unmatched_reports": [],
            "records": [],
        }
        for index in range(220):
            projection["records"].append({
                "platform": "longbridge",
                "target_key": f"lb-svc-{index}|rot|paper",
                "target": {"service": f"lb-svc-{index}", "strategy_profile": "rot", "account_scope": "paper"},
                "business_date": "2026-09-28",
                "timezone": "Asia/Hong_Kong",
                "status": "missing_report" if index == 0 else "market_closed",
                "completeness": "incomplete" if index == 0 else "complete",
                "execution_lane": "paper",
                "runs": [],
                "conflicts": [],
                "fills": {"source": "not_connected", "records": [], "count": None},
            })
        with patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome") as send_target:
            too_long = dispatch_runtime_digest(projection)
        self.assertIn("runtime_digest_too_long", too_long["errors"])
        self.assertIn("到期缺报告", too_long["telegram_preview"])
        send_target.assert_not_called()

        short = {
            "platform": "longbridge",
            "observed_at": "2026-09-28T08:40:00+00:00",
            "completeness": "complete",
            "read_errors": [],
            "unmatched_reports": [],
            "records": [projection["records"][1]],
        }
        module = __import__("quant_monitor_domain.briefing_dispatch", fromlist=["_health_cycle_module"])._health_cycle_module()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"TELEGRAM_TOKEN": "token", "GLOBAL_TELEGRAM_CHAT_ID": "chat", "QUANT_MONITOR_ROOT": tmp},
            clear=True,
        ), patch.object(module, "_write_alert_state", side_effect=OSError("disk")), patch(
            "quant_monitor_domain.briefing_dispatch.telegram_target_outcome",
        ) as send_target:
            failed = dispatch_runtime_digest(short)
        self.assertEqual(failed["errors"], ["alert_state_write_failed"])
        send_target.assert_not_called()


if __name__ == "__main__":
    unittest.main()
