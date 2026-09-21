"""Focused tests for dependency/engineering review notification triage."""

from __future__ import annotations

import unittest

from service.briefing_consumer import BriefingAction
from service.briefing_dispatch import dispatch_briefing_result
from service.dependency_notification_triage import (
    DEPENDABOT_LOGINS,
    triage_dependency_notification,
    triage_to_briefing_result,
)


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _evidence(**overrides):
    payload = {
        "repository": "QuantStrategyLab/ExampleRepo",
        "author_login": "dependabot[bot]",
        "notification_kind": "dependabot",
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


class DependencyNotificationTriageTests(unittest.TestCase):
    def test_low_risk_python_and_schwab_samples_are_quiet(self) -> None:
        samples = (
            _evidence(dependency_names=["anyio"], changed_files=["pyproject.toml", "uv.lock"]),
            _evidence(dependency_names=["soupsieve"], changed_files=["requirements.txt"]),
            _evidence(
                repository="QuantStrategyLab/SchwabTokenAutoRefresher",
                dependency_names=["otpauth"],
                changed_files=["package.json", "package-lock.json"],
                update_class="patch",
            ),
            _evidence(
                repository="QuantStrategyLab/SchwabTokenAutoRefresher",
                dependency_names=["playwright"],
                changed_files=["package.json", "package-lock.json"],
                update_class="minor",
            ),
        )
        for sample in samples:
            with self.subTest(deps=sample["dependency_names"]):
                result = triage_dependency_notification(sample)
                self.assertEqual(result.disposition, "quiet")
                self.assertEqual(result.action, BriefingAction.QUIET)
                self.assertEqual(result.confidence, "known")
                self.assertFalse(result.review_required)
                briefing = triage_to_briefing_result(result)
                self.assertEqual(briefing.action, BriefingAction.QUIET)
                summary = dispatch_briefing_result(briefing, dry_run=True)
                self.assertEqual(summary["action"], "quiet")
                self.assertIn("quiet", summary["skipped"])

    def test_title_alone_never_grants_quiet(self) -> None:
        result = triage_dependency_notification(
            {
                "repository": "QuantStrategyLab/ExampleRepo",
                "title": "Bump anyio from 4.0.0 to 4.1.0",
                "author_login": "dependabot[bot]",
            }
        )
        self.assertNotEqual(result.disposition, "quiet")
        self.assertTrue(result.review_required)
        self.assertIn(result.confidence, {"unknown", "review_required"})

    def test_quant_runtime_settings_workflow_is_telegram(self) -> None:
        result = triage_dependency_notification(
            _evidence(
                repository="QuantStrategyLab/QuantRuntimeSettings",
                dependency_names=["actions/checkout"],
                changed_files=[".github/workflows/validate.yml"],
                update_class="minor",
            )
        )
        self.assertEqual(result.disposition, "telegram")
        self.assertEqual(result.action, BriefingAction.TELEGRAM)
        self.assertIn("workflow", " ".join(result.reasons).lower())

    def test_ib_gateway_docker_is_telegram(self) -> None:
        result = triage_dependency_notification(
            _evidence(
                repository="QuantStrategyLab/IBKRGatewayManager",
                dependency_names=["ib-gateway"],
                changed_files=["Dockerfile", "docker-compose.yml"],
                update_class="patch",
            )
        )
        self.assertEqual(result.disposition, "telegram")
        self.assertEqual(result.action, BriefingAction.TELEGRAM)
        self.assertTrue(any("docker" in reason.lower() or "broker" in reason.lower() for reason in result.reasons))

    def test_qpk_pin_is_telegram(self) -> None:
        by_flag = triage_dependency_notification(
            _evidence(
                repository="QuantStrategyLab/UsEquityStrategies",
                dependency_names=["QuantPlatformKit"],
                changed_files=["requirements.txt"],
                update_class="patch",
                qpk_pin_changed=True,
            )
        )
        by_path = triage_dependency_notification(
            _evidence(
                repository="QuantStrategyLab/UsEquityStrategies",
                dependency_names=["QuantPlatformKit"],
                changed_files=["requirements.txt", "qsl.toml"],
                update_class="patch",
            )
        )
        for result in (by_flag, by_path):
            self.assertEqual(result.disposition, "telegram")
            self.assertEqual(result.action, BriefingAction.TELEGRAM)

    def test_ci_failure_major_unknown_surface_and_non_dependabot_escalate(self) -> None:
        cases = (
            (_evidence(ci_status="failure"), "telegram", "ci"),
            (_evidence(update_class="major"), "telegram", "major"),
            (_evidence(update_class="security"), "telegram", "security"),
            (
                _evidence(changed_files=["src/helper.py"], dependency_only_manifest_change=False),
                "github_issue",
                "source",
            ),
            (
                _evidence(changed_files=["vendor/weird.bin"], dependency_only_manifest_change=False),
                "telegram",
                "unknown",
            ),
            (
                _evidence(author_login="human-dev", notification_kind="review_request"),
                "github_issue",
                "dependabot",
            ),
        )
        for sample, disposition, needle in cases:
            with self.subTest(disposition=disposition, needle=needle):
                result = triage_dependency_notification(sample)
                self.assertEqual(result.disposition, disposition)
                self.assertNotEqual(result.action, BriefingAction.QUIET)
                self.assertTrue(any(needle in reason.lower() for reason in result.reasons))

    def test_missing_or_conflicting_evidence_is_fail_closed(self) -> None:
        missing = triage_dependency_notification({"repository": "QuantStrategyLab/ExampleRepo"})
        self.assertNotEqual(missing.disposition, "quiet")
        self.assertEqual(missing.action, BriefingAction.TELEGRAM)
        self.assertTrue(missing.review_required)
        self.assertIn(missing.confidence, {"unknown", "review_required"})

        conflict = triage_dependency_notification(
            _evidence(
                update_class="patch",
                changed_files=["package.json", ".github/workflows/ci.yml"],
            )
        )
        self.assertEqual(conflict.disposition, "telegram")
        self.assertEqual(conflict.action, BriefingAction.TELEGRAM)
        self.assertNotEqual(conflict.disposition, "quiet")
        self.assertIn("workflow", " ".join(conflict.reasons).lower())

        bad_sha = triage_dependency_notification(_evidence(base_sha="short", head_sha=HEAD_SHA))
        self.assertNotEqual(bad_sha.disposition, "quiet")
        self.assertEqual(bad_sha.action, BriefingAction.TELEGRAM)
        self.assertTrue(bad_sha.review_required)
        self.assertIn(bad_sha.confidence, {"unknown", "review_required"})

    def test_gate_evidence_gaps_escalate_to_telegram_not_quiet(self) -> None:
        """Gate gaps that block low-risk quiet must surface on Telegram, not GitHub issue."""
        cases = (
            (
                "missing_evidence",
                {"repository": "QuantStrategyLab/ExampleRepo", "author_login": "dependabot[bot]"},
            ),
            ("invalid_sha", _evidence(base_sha="not-a-sha", head_sha=HEAD_SHA)),
            ("pending_ci", _evidence(ci_status="pending")),
            ("unknown_ci", _evidence(ci_status="unknown")),
            (
                "dependency_only_missing",
                _evidence(dependency_only_manifest_change=False, changed_files=["requirements.txt"]),
            ),
            (
                "conflicting_file_surface",
                _evidence(
                    changed_files=["package.json", ".github/workflows/ci.yml"],
                    dependency_only_manifest_change=True,
                ),
            ),
        )
        for label, sample in cases:
            with self.subTest(label=label):
                result = triage_dependency_notification(sample)
                self.assertNotEqual(result.disposition, "quiet")
                self.assertNotEqual(result.action, BriefingAction.QUIET)
                self.assertEqual(result.disposition, "telegram")
                self.assertEqual(result.action, BriefingAction.TELEGRAM)
                if label == "conflicting_file_surface":
                    # Known workflow/high-risk surface: Telegram without pretending quiet-gate passed.
                    self.assertIn("workflow", " ".join(result.reasons).lower())
                else:
                    self.assertTrue(result.review_required)
                    self.assertIn(result.confidence, {"unknown", "review_required"})

    def test_title_alone_and_none_evidence_go_telegram(self) -> None:
        title_only = triage_dependency_notification(
            {
                "repository": "QuantStrategyLab/ExampleRepo",
                "title": "Bump anyio from 4.0.0 to 4.1.0",
                "author_login": "dependabot[bot]",
            }
        )
        self.assertEqual(title_only.action, BriefingAction.TELEGRAM)
        self.assertTrue(title_only.review_required)
        self.assertIn(title_only.confidence, {"unknown", "review_required"})

        none_evidence = triage_dependency_notification(None)
        self.assertEqual(none_evidence.action, BriefingAction.TELEGRAM)
        self.assertTrue(none_evidence.review_required)
        self.assertIn(none_evidence.confidence, {"unknown", "review_required"})

    def test_explicit_source_and_non_dependabot_still_github_issue(self) -> None:
        source = triage_dependency_notification(
            _evidence(
                changed_files=["src/helper.py"],
                dependency_only_manifest_change=False,
            )
        )
        self.assertEqual(source.disposition, "github_issue")
        self.assertEqual(source.action, BriefingAction.GITHUB_ISSUE)
        self.assertTrue(source.review_required)

        human = triage_dependency_notification(
            _evidence(author_login="human-dev", notification_kind="review_request")
        )
        self.assertEqual(human.disposition, "github_issue")
        self.assertEqual(human.action, BriefingAction.GITHUB_ISSUE)
        self.assertTrue(human.review_required)

    def test_schwab_audit_decision_maps_without_side_effects(self) -> None:
        from service.dependency_notification_triage import triage_schwab_dependency_audit_decision

        quiet = triage_schwab_dependency_audit_decision(
            {
                "pr": 62,
                "decision": "approve",
                "summary": "compatible",
                "reason": "otpauth patch only",
                "repository": "QuantStrategyLab/SchwabTokenAutoRefresher",
            }
        )
        self.assertEqual(quiet.action, BriefingAction.QUIET)
        review = triage_schwab_dependency_audit_decision(
            {
                "pr": 63,
                "decision": "human_required",
                "summary": "needs operator",
                "reason": "uncertain compatibility",
            }
        )
        self.assertEqual(review.action, BriefingAction.GITHUB_ISSUE)
        briefing = triage_to_briefing_result(review)
        summary = dispatch_briefing_result(briefing, dry_run=True)
        self.assertIn("github_dry_run", summary)
        self.assertFalse(summary.get("telegram_sent"))

    def test_dependabot_login_allowlist_is_narrow(self) -> None:
        self.assertIn("dependabot[bot]", DEPENDABOT_LOGINS)
        self.assertIn("app/dependabot", DEPENDABOT_LOGINS)


if __name__ == "__main__":
    unittest.main()
