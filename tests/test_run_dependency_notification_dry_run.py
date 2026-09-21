"""Focused tests for trusted structured dependency-notification dry-run intake."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_dependency_notification_dry_run as cli
from service import dependency_notification_triage as triage


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
HEAD_SHA_2 = "c" * 40


def _event(**overrides):
    payload = {
        "repository": "QuantStrategyLab/ExampleRepo",
        "pr_number": 101,
        "author_login": "dependabot[bot]",
        "update_class": "minor",
        "dependency_names": ["anyio"],
        "changed_files": ["requirements.txt", "requirements-lock.txt"],
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "ci_status": "success",
        "dependency_only_manifest_change": True,
    }
    payload.update(overrides)
    return payload


def _payload(events, *, schema_version=1):
    return {"schema_version": schema_version, "events": list(events)}


class TrustedIntakeDryRunTests(unittest.TestCase):
    def test_low_risk_samples_are_quiet(self) -> None:
        samples = (
            _event(dependency_names=["anyio"], changed_files=["pyproject.toml", "uv.lock"]),
            _event(
                pr_number=102,
                dependency_names=["soupsieve"],
                changed_files=["requirements.txt"],
                head_sha=HEAD_SHA_2,
            ),
            _event(
                repository="QuantStrategyLab/SchwabTokenAutoRefresher",
                pr_number=201,
                dependency_names=["otpauth"],
                changed_files=["package.json", "package-lock.json"],
                update_class="patch",
                head_sha="d" * 40,
            ),
            _event(
                repository="QuantStrategyLab/SchwabTokenAutoRefresher",
                pr_number=202,
                dependency_names=["playwright"],
                changed_files=["package.json", "package-lock.json"],
                update_class="minor",
                head_sha="e" * 40,
            ),
        )
        result = triage.run_trusted_intake_dry_run(_payload(samples))
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "quiet")
        self.assertEqual(result["counts"]["quiet"], 4)
        self.assertEqual(result["counts"]["events"], 4)
        for item in result["events"]:
            self.assertEqual(item["decision"], "quiet")
            self.assertFalse(item["review_required"])
            self.assertNotIn("title", item)
            self.assertNotIn("body", item)

    def test_workflow_docker_qpk_preview_telegram(self) -> None:
        events = (
            _event(
                repository="QuantStrategyLab/QuantRuntimeSettings",
                pr_number=10,
                dependency_names=["actions/checkout"],
                changed_files=[".github/workflows/validate.yml"],
                head_sha="f" * 40,
            ),
            _event(
                repository="QuantStrategyLab/IBKRGatewayManager",
                pr_number=11,
                dependency_names=["ib-gateway"],
                changed_files=["Dockerfile", "docker-compose.yml"],
                update_class="patch",
                head_sha="1" * 40,
            ),
            _event(
                repository="QuantStrategyLab/UsEquityStrategies",
                pr_number=12,
                dependency_names=["QuantPlatformKit"],
                changed_files=["requirements.txt"],
                update_class="patch",
                qpk_pin_changed=True,
                head_sha="2" * 40,
            ),
        )
        result = triage.run_trusted_intake_dry_run(_payload(events))
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "telegram")
        self.assertEqual(result["counts"]["telegram"], 3)
        self.assertTrue(result["dispatch"]["telegram_dry_run"]["present"])
        for item in result["events"]:
            self.assertEqual(item["decision"], "telegram")

    def test_source_and_non_dependabot_preview_github_issue(self) -> None:
        events = (
            _event(
                pr_number=20,
                changed_files=["src/helper.py"],
                dependency_only_manifest_change=False,
                head_sha="3" * 40,
            ),
            _event(
                pr_number=21,
                author_login="human-dev",
                head_sha="4" * 40,
            ),
        )
        result = triage.run_trusted_intake_dry_run(_payload(events))
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "github_issue")
        self.assertEqual(result["counts"]["github_issue"], 2)
        self.assertTrue(result["dispatch"]["github_dry_run"]["present"])
        for item in result["events"]:
            self.assertEqual(item["decision"], "github_issue")
            self.assertTrue(item["review_required"])

    def test_duplicate_events_deduped_by_repo_pr_head(self) -> None:
        first = _event(pr_number=55, head_sha=HEAD_SHA)
        duplicate = _event(pr_number=55, head_sha=HEAD_SHA, dependency_names=["anyio", "extra"])
        different_head = _event(pr_number=55, head_sha=HEAD_SHA_2)
        result = triage.run_trusted_intake_dry_run(_payload([first, duplicate, different_head]))
        self.assertTrue(result["ok"])
        self.assertEqual(result["counts"]["events"], 2)
        self.assertEqual(result["counts"]["deduped"], 1)
        self.assertEqual(len(result["events"]), 2)

    def test_invalid_json_schema_missing_fields_and_limit_fail_closed(self) -> None:
        cases = (
            ("not-json", "invalid_json"),
            ({"schema_version": 2, "events": []}, "schema_version"),
            ({"schema_version": 1, "events": "nope"}, "events"),
            ({"schema_version": 1, "events": [{"repository": "x"}]}, "required"),
            (
                {
                    "schema_version": 1,
                    "events": [
                        _event(pr_number=i, head_sha=f"{i:040d}")
                        for i in range(triage.MAX_TRUSTED_INTAKE_EVENTS + 1)
                    ],
                },
                "limit",
            ),
        )
        for payload, needle in cases:
            with self.subTest(needle=needle):
                if payload == "not-json":
                    result = triage.run_trusted_intake_dry_run(None, parse_error="invalid_json")
                else:
                    result = triage.run_trusted_intake_dry_run(payload)
                self.assertFalse(result["ok"])
                self.assertEqual(result["action"], "telegram")
                self.assertTrue(result["review_required"])
                self.assertIn(result["confidence"], {"unknown", "review_required"})
                self.assertTrue(any(needle in reason.lower() for reason in result["reasons"]))

    def test_dry_run_never_calls_send_or_create(self) -> None:
        payload = _payload(
            [
                _event(
                    repository="QuantStrategyLab/QuantRuntimeSettings",
                    changed_files=[".github/workflows/validate.yml"],
                ),
                _event(
                    pr_number=99,
                    author_login="human-dev",
                    head_sha=HEAD_SHA_2,
                ),
            ]
        )
        with (
            patch("service.briefing_dispatch.send_telegram_alert") as send_tg,
            patch("service.briefing_dispatch.create_github_issue") as create_issue,
        ):
            result = triage.run_trusted_intake_dry_run(payload)
        self.assertTrue(result["ok"])
        send_tg.assert_not_called()
        create_issue.assert_not_called()
        self.assertFalse(result["dispatch"].get("telegram_sent", False))
        self.assertIsNone(result["dispatch"].get("github_issue"))

    def test_cli_reads_file_and_stdin(self) -> None:
        quiet_payload = _payload(
            [_event(dependency_names=["anyio"], changed_files=["pyproject.toml", "uv.lock"])]
        )
        with self.subTest("file"):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "intake.json"
                path.write_text(json.dumps(quiet_payload), encoding="utf-8")
                code = cli.main(["--input", str(path)])
            self.assertEqual(code, 0)
        with self.subTest("stdin"):
            with patch("sys.stdin", io.StringIO(json.dumps(quiet_payload))):
                code = cli.main(["--input", "-"])
            self.assertEqual(code, 0)
        with self.subTest("bad schema exit"):
            with patch("sys.stdin", io.StringIO("{bad")):
                code = cli.main(["--input", "-"])
            self.assertNotEqual(code, 0)

    def test_output_redacts_title_body_and_raw_dispatch_text(self) -> None:
        payload = _payload(
            [
                _event(
                    repository="QuantStrategyLab/QuantRuntimeSettings",
                    changed_files=[".github/workflows/validate.yml"],
                    title="Bump checkout",
                    body="secret token abc",
                )
            ]
        )
        result = triage.run_trusted_intake_dry_run(payload)
        blob = json.dumps(result)
        self.assertNotIn("Bump checkout", blob)
        self.assertNotIn("secret token", blob)
        self.assertNotIn("telegram_dry_run\": \"", blob.replace(" ", ""))
        self.assertTrue(result["dispatch"]["telegram_dry_run"]["present"])
        self.assertIn("safe_summary", result["dispatch"]["telegram_dry_run"])


if __name__ == "__main__":
    unittest.main()
