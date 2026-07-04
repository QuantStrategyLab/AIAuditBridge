from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.run_codex_pr_review as run_codex_pr_review
from scripts.run_codex_pr_review import ReviewError, run_codex_review_with_fallback


class RunCodexPrReviewTests(unittest.TestCase):
    def _write_event(self, tmpdir: str, files: list[str]) -> str:
        event = {
            "pull_request": {"number": 7, "head": {"sha": "abc123"}},
        }
        path = Path(tmpdir) / "event.json"
        path.write_text(
            __import__("json").dumps(event),
            encoding="utf-8",
        )
        return str(path)

    def test_changed_files_are_low_risk_only_for_docs_and_tests(self) -> None:
        policy = run_codex_pr_review.load_policy()
        self.assertTrue(run_codex_pr_review.changed_files_are_low_risk(["docs/guide.md", "tests/test_x.py"], policy))
        self.assertFalse(run_codex_pr_review.changed_files_are_low_risk(["src/app.py"], policy))

    def test_service_failure_falls_back_to_direct_api(self) -> None:
        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://service.example"}, clear=True),
            patch(
                "scripts.run_codex_pr_review.run_codex_service_review",
                side_effect=ReviewError("HTTP 429 Too Many Requests"),
            ),
            patch("scripts.run_codex_pr_review.run_direct_api_review", return_value="api review") as direct_api,
        ):
            output = run_codex_review_with_fallback(
                "Review this PR.",
                timeout_minutes=20,
                complexity="high",
                changed_file_count=3,
                changed_line_count=120,
            )

        self.assertEqual(output, "api review")
        direct_api.assert_called_once_with("Review this PR.", complexity="high")

    def test_service_auth_failure_does_not_fall_back_to_direct_api(self) -> None:
        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://service.example"}, clear=True),
            patch(
                "scripts.run_codex_pr_review.run_codex_service_review",
                side_effect=ReviewError("Codex service request failed: 401 Unauthorized"),
            ),
            patch("scripts.run_codex_pr_review.run_direct_api_review") as direct_api,
        ):
            with self.assertRaises(ReviewError):
                run_codex_review_with_fallback(
                    "Review this PR.",
                    timeout_minutes=20,
                    complexity="high",
                    changed_file_count=3,
                    changed_line_count=120,
                )

        direct_api.assert_not_called()

    def test_main_fails_closed_on_review_infra_error_for_high_risk_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = self._write_event(tmpdir, ["src/app.py"])
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": event_path,
                "GITHUB_EVENT_NAME": "pull_request",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch("scripts.run_codex_pr_review.fetch_pr_files", return_value=[{"filename": "src/app.py"}]),
                patch("scripts.run_codex_pr_review.fetch_pr_diff", return_value="diff --git a/src/app.py b/src/app.py"),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError("Codex service job timed out")),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 1)

        comment.assert_called_once()

    def test_main_allows_low_risk_docs_on_review_infra_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = self._write_event(tmpdir, ["docs/guide.md", "tests/test_x.py"])
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": event_path,
                "GITHUB_EVENT_NAME": "pull_request",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch("scripts.run_codex_pr_review.fetch_pr_files", return_value=[{"filename": "docs/guide.md"}, {"filename": "tests/test_x.py"}]),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError("Codex service job timed out")),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 0)

        comment.assert_called_once()

    def test_service_timeout_does_not_fall_back_to_direct_api(self) -> None:
        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://service.example"}, clear=True),
            patch(
                "scripts.run_codex_pr_review.run_codex_service_review",
                side_effect=ReviewError("Codex service job timed out"),
            ),
            patch("scripts.run_codex_pr_review.run_direct_api_review") as direct_api,
        ):
            with self.assertRaises(ReviewError):
                run_codex_review_with_fallback(
                    "Review this PR.",
                    timeout_minutes=20,
                    complexity="high",
                    changed_file_count=3,
                    changed_line_count=120,
                )

        direct_api.assert_not_called()
