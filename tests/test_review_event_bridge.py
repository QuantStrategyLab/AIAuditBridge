from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from scripts import emit_pr_review_event
from service.review_event_notification import (
    ReviewEventError,
    assert_review_event_provenance,
    dispatch_review_event_notification,
    expected_review_event_run_id,
    parse_review_event_metadata,
)
from service.review_event_store import ReviewEventStore, ReviewEventStoreError


class ReviewEventEmitterTests(unittest.TestCase):
    def test_build_payload_binds_current_pr_head_and_sanitized_decision(self) -> None:
        with TemporaryDirectory() as tmp:
            decision_path = Path(tmp) / "decision.json"
            decision_path.write_text(
                json.dumps({"blocked": True, "contract_conflict": False, "findings": ["secret body"]}),
                encoding="utf-8",
            )
            env = {
                "GITHUB_REPOSITORY": "QuantStrategyLab/ExampleRepo",
                "GITHUB_RUN_ID": "123456789",
                "CODEX_REVIEW_STEP_OUTCOME": "failure",
                "CODEX_REVIEW_DECISION_PATH": str(decision_path),
                "CODEX_REVIEW_PR_NUMBER": "17",
                "CODEX_REVIEW_HEAD_SHA": "a" * 40,
            }

            payload = emit_pr_review_event.build_payload(env)

        self.assertEqual(
            payload["run_id"],
            f"review:QuantStrategyLab/ExampleRepo:17:{'a' * 40}:123456789",
        )
        self.assertEqual(payload["task"], "pr_review_completed")
        self.assertEqual(payload["task_state"], "completed")
        self.assertEqual(payload["mode"], "review_only")
        self.assertEqual(
            payload["metadata"],
            {
                "schema": "qsl.pr_review_event.v1",
                "repository": "QuantStrategyLab/ExampleRepo",
                "pr_number": 17,
                "head_sha": "a" * 40,
                "workflow_run_id": "123456789",
                "review_outcome": "failure",
                "blocked": True,
                "contract_conflict": False,
            },
        )
        self.assertNotIn("findings", payload["metadata"])

    def test_build_payload_uses_unknown_decision_fields_when_output_is_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            payload = emit_pr_review_event.build_payload(
                {
                    "GITHUB_REPOSITORY": "QuantStrategyLab/ExampleRepo",
                    "GITHUB_RUN_ID": "9",
                    "CODEX_REVIEW_STEP_OUTCOME": "failure",
                    "CODEX_REVIEW_DECISION_PATH": str(Path(tmp) / "missing.json"),
                    "CODEX_REVIEW_PR_NUMBER": "2",
                    "CODEX_REVIEW_HEAD_SHA": "b" * 40,
                }
            )

        self.assertIsNone(payload["metadata"]["blocked"])
        self.assertIsNone(payload["metadata"]["contract_conflict"])

    def test_normalize_service_url_requires_https_outside_loopback(self) -> None:
        self.assertEqual(
            emit_pr_review_event.normalize_service_url("https://audit.example/path/"),
            "https://audit.example/path",
        )
        self.assertEqual(
            emit_pr_review_event.normalize_service_url("http://127.0.0.1:8080"),
            "http://127.0.0.1:8080",
        )
        for value in (
            "http://audit.example",
            "https://user:pass@audit.example",
            "https://audit.example/path?token=x",
            "relative/path",
        ):
            with self.subTest(value=value), self.assertRaises(emit_pr_review_event.ReviewEventEmissionError):
                emit_pr_review_event.normalize_service_url(value)

    def test_main_skips_cleanly_when_service_is_not_configured(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(emit_pr_review_event.main(), 0)

    def test_post_event_requires_delivered_or_deduplicated_notification(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"status": "ok", "notification": {"status": "failed"}}
        ).encode("utf-8")
        with patch("scripts.emit_pr_review_event.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(
                emit_pr_review_event.ReviewEventEmissionError,
                "notification was not delivered",
            ):
                emit_pr_review_event.post_event("https://audit.example", "oidc", {})


class ReviewEventStoreTests(unittest.TestCase):
    def test_store_persists_status_without_automation_ledger_semantics(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "review_events.json"
            store = ReviewEventStore(storage_path=path, max_events=2)
            store.set_status("review:repo:1:a:1", "pending")
            store.set_status("review:repo:1:a:1", "sent")

            reloaded = ReviewEventStore(storage_path=path, max_events=2)

        self.assertEqual(reloaded.get_status("review:repo:1:a:1"), "sent")

    def test_store_is_bounded_and_rejects_corrupt_state(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "review_events.json"
            store = ReviewEventStore(storage_path=path, max_events=2)
            with patch("service.review_event_store.time.time", side_effect=[1.0, 2.0, 3.0]):
                store.set_status("review:repo:1:a:1", "sent")
                store.set_status("review:repo:2:b:2", "sent")
                store.set_status("review:repo:3:c:3", "sent")
            self.assertIsNone(store.get_status("review:repo:1:a:1"))
            self.assertEqual(store.get_status("review:repo:3:c:3"), "sent")

            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(ReviewEventStoreError):
                ReviewEventStore(storage_path=path)


class ReviewEventNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = {
            "schema": "qsl.pr_review_event.v1",
            "repository": "QuantStrategyLab/ExampleRepo",
            "pr_number": 17,
            "head_sha": "a" * 40,
            "workflow_run_id": "123456789",
            "review_outcome": "failure",
            "blocked": True,
            "contract_conflict": False,
        }

    def test_parser_derives_public_url_and_run_id(self) -> None:
        event = parse_review_event_metadata("QuantStrategyLab/ExampleRepo", self.metadata)

        self.assertEqual(event.workflow_run_url, "https://github.com/QuantStrategyLab/ExampleRepo/actions/runs/123456789")
        self.assertEqual(
            expected_review_event_run_id(event),
            f"review:QuantStrategyLab/ExampleRepo:17:{'a' * 40}:123456789",
        )

    def test_parser_rejects_unknown_keys_and_type_confusion(self) -> None:
        cases = [
            {**self.metadata, "review_body": "do not retain"},
            {**self.metadata, "pr_number": True},
            {**self.metadata, "blocked": 1},
            {**self.metadata, "head_sha": "A" * 40},
            {**self.metadata, "repository": "OtherOrg/ExampleRepo"},
            {**self.metadata, "workflow_run_id": "01"},
        ]
        for metadata in cases:
            with self.subTest(metadata=metadata), self.assertRaises(ReviewEventError):
                parse_review_event_metadata("QuantStrategyLab/ExampleRepo", metadata)

    def test_provenance_binds_run_and_trusted_review_workflow(self) -> None:
        event = parse_review_event_metadata("QuantStrategyLab/ExampleRepo", self.metadata)
        direct_claims = {
            "auth_method": "github_oidc",
            "repository": event.repository,
            "run_id": event.workflow_run_id,
            "workflow_ref": "QuantStrategyLab/AIAuditBridge/.github/workflows/codex_pr_review.yml@refs/heads/main",
        }
        reusable_claims = {
            **direct_claims,
            "workflow_ref": "QuantStrategyLab/ExampleRepo/.github/workflows/codex_pr_review.yml@refs/heads/main",
            "job_workflow_ref": "QuantStrategyLab/AIAuditBridge/.github/workflows/codex_pr_review.yml@refs/heads/main",
        }

        assert_review_event_provenance(direct_claims, event)
        assert_review_event_provenance(reusable_claims, event)

        invalid_claims = [
            {**direct_claims, "run_id": "987654321"},
            {**direct_claims, "repository": "QuantStrategyLab/OtherRepo"},
            {**direct_claims, "workflow_ref": "QuantStrategyLab/AIAuditBridge/.github/workflows/codex_audit.yml@refs/heads/main"},
            {**direct_claims, "auth_method": "static_token"},
        ]
        for claims in invalid_claims:
            with self.subTest(claims=claims), self.assertRaises(PermissionError):
                assert_review_event_provenance(claims, event)

    @patch("service.review_event_notification.send_telegram_alert", return_value=True)
    def test_dispatch_uses_sanitized_summary_only(self, send) -> None:
        event = parse_review_event_metadata("QuantStrategyLab/ExampleRepo", self.metadata)
        with patch.dict(
            os.environ,
            {"TELEGRAM_TOKEN": "token", "GLOBAL_TELEGRAM_CHAT_ID": "123"},
            clear=True,
        ):
            result = dispatch_review_event_notification(event)

        self.assertEqual(result["status"], "sent")
        text = send.call_args.kwargs["text"]
        self.assertIn("ExampleRepo PR #17", text)
        self.assertIn("review=failure", text)
        self.assertIn(event.workflow_run_url, text)
        self.assertNotIn("token", text)


if __name__ == "__main__":
    unittest.main()
