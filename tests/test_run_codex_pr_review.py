from __future__ import annotations

import base64
import json
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
            json.dumps(event),
            encoding="utf-8",
        )
        return str(path)

    def test_changed_files_are_low_risk_only_for_docs_and_tests(self) -> None:
        policy = run_codex_pr_review.load_policy()
        self.assertTrue(run_codex_pr_review.changed_files_are_low_risk(["docs/guide.md", "tests/test_x.py"], policy))
        self.assertFalse(run_codex_pr_review.changed_files_are_low_risk(["src/app.py"], policy))

    def test_load_policy_uses_trusted_base_ref(self) -> None:
        trusted_policy = {
            "version": 1,
            "blocked_path_patterns": [],
            "risk_policy": {
                "low": {"prefixes": ["trusted/"], "exact": ["SAFE.md"], "reason": "trusted"},
                "high": {"reason": "trusted high"},
            },
        }
        encoded = base64.b64encode(json.dumps(trusted_policy).encode("utf-8")).decode("ascii")
        with patch("scripts.run_codex_pr_review.github_request", return_value={"content": encoded}) as request:
            policy = run_codex_pr_review.load_policy("token", "org/repo", "base-sha")

        self.assertEqual(policy["risk_policy"]["low"]["exact"], ["SAFE.md"])
        request.assert_called_once()

    def test_service_failure_falls_back_to_direct_api(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_AUDIT_SERVICE_URL": "https://service.example",
                    "CODEX_PR_REVIEW_API_FALLBACK_ENABLED": "true",
                },
                clear=True,
            ),
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

    def test_service_failure_does_not_fallback_to_direct_api_by_default(self) -> None:
        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://service.example"}, clear=True),
            patch(
                "scripts.run_codex_pr_review.run_codex_service_review",
                side_effect=ReviewError("HTTP 429 Too Many Requests"),
            ),
            patch("scripts.run_codex_pr_review.run_direct_api_review") as direct_api,
        ):
            with self.assertRaises(ReviewError) as raised:
                run_codex_review_with_fallback(
                    "Review this PR.",
                    timeout_minutes=20,
                    complexity="high",
                    changed_file_count=3,
                    changed_line_count=120,
                )

        self.assertIn("direct API fallback is disabled", str(raised.exception))
        direct_api.assert_not_called()

    def test_direct_api_runs_when_service_url_is_unset(self) -> None:
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True),
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


    def test_service_fallback_without_api_keys_preserves_service_failure(self) -> None:
        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://service.example"}, clear=True),
            patch(
                "scripts.run_codex_pr_review.run_codex_service_review",
                side_effect=ReviewError("HTTP 429 Too Many Requests"),
            ),
        ):
            with self.assertRaises(ReviewError) as raised:
                run_codex_review_with_fallback(
                    "Review this PR.",
                    timeout_minutes=20,
                    complexity="high",
                    changed_file_count=3,
                    changed_line_count=120,
                )

        self.assertIn("Codex service review failed", str(raised.exception))
        self.assertFalse(run_codex_pr_review._review_backend_is_unconfigured(raised.exception))

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


    def test_main_allows_unconfigured_backend_with_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = self._write_event(tmpdir, ["scripts/run_codex_pr_review.py"])
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": event_path,
                "GITHUB_EVENT_NAME": "pull_request",
                "CODEX_PR_REVIEW_ALLOW_UNCONFIGURED_BACKEND": "true",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch("scripts.run_codex_pr_review.fetch_pr_files", return_value=[{"filename": "scripts/run_codex_pr_review.py"}]),
                patch("scripts.run_codex_pr_review.fetch_pr_diff", return_value="diff --git a/scripts/run_codex_pr_review.py b/scripts/run_codex_pr_review.py"),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError(run_codex_pr_review.NO_REVIEW_BACKEND_CONFIGURED)),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 0)

        comment.assert_called_once()
        self.assertIn("Human review required", comment.call_args.args[3])


    def test_main_fails_closed_on_unconfigured_backend_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = self._write_event(tmpdir, ["scripts/run_codex_pr_review.py"])
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": event_path,
                "GITHUB_EVENT_NAME": "pull_request",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch("scripts.run_codex_pr_review.fetch_pr_files", return_value=[{"filename": "scripts/run_codex_pr_review.py"}]),
                patch("scripts.run_codex_pr_review.fetch_pr_diff", return_value="diff --git a/scripts/run_codex_pr_review.py b/scripts/run_codex_pr_review.py"),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError(run_codex_pr_review.NO_REVIEW_BACKEND_CONFIGURED)),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 1)

        comment.assert_called_once()
        self.assertIn("Human review required", comment.call_args.args[3])

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

    def test_service_review_refreshes_oidc_token_while_polling(self) -> None:
        responses = [
            {"job_id": "job-1"},
            {"status": "succeeded", "output": "{}"},
        ]
        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://service.example", "GITHUB_REPOSITORY": "org/repo"}, clear=True),
            patch("scripts.run_codex_pr_review.request_github_oidc_token", side_effect=["submit-token", "poll-token"]) as oidc,
            patch("scripts.run_codex_pr_review._service_request", side_effect=responses) as request,
            patch("scripts.run_codex_pr_review.time.sleep"),
        ):
            output = run_codex_pr_review.run_codex_service_review("prompt", timeout_minutes=1)

        self.assertEqual(output, "{}")
        self.assertEqual(oidc.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[2], "submit-token")
        self.assertEqual(request.call_args_list[1].args[2], "poll-token")


class CodexPrReviewWorkflowTest(unittest.TestCase):
    def test_reusable_workflow_runs_bridge_script_against_source_checkout(self) -> None:
        workflow = Path(".github/workflows/codex_pr_review.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("  pull_request:\n    types: [opened, synchronize, reopened]", workflow)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", workflow)
        self.assertIn("path: source", workflow)
        self.assertIn("path: bridge", workflow)
        self.assertIn("CODEX_AUDIT_REUSABLE_WORKFLOW_TOKEN", workflow)
        self.assertIn("caller_concurrency_key", workflow)
        self.assertIn("allow_unconfigured_backend", workflow)
        self.assertIn("api_fallback_enabled", workflow)
        self.assertIn("Defaults to true for backward-compatible reusable callers", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("default: true", workflow)
        self.assertIn("CODEX_PR_REVIEW_ALLOW_UNCONFIGURED_BACKEND", workflow)
        self.assertIn("CODEX_PR_REVIEW_API_FALLBACK_ENABLED", workflow)
        self.assertIn("inputs.api_fallback_enabled || vars.CODEX_PR_REVIEW_API_FALLBACK_ENABLED || 'false'", workflow)
        self.assertIn("inputs.caller_concurrency_key || github.event.pull_request.number || github.run_id", workflow)
        self.assertNotIn("Validate bridge checkout token", workflow)
        self.assertIn("required: false", workflow)
        self.assertIn("job.workflow_repository", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("github.event.pull_request.head.sha", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("job.workflow_sha", workflow)
        self.assertIn("token: ${{ secrets.CODEX_AUDIT_REUSABLE_WORKFLOW_TOKEN || github.token }}", workflow)
        self.assertNotIn("CODEX_AUDIT_DISPATCH_TOKEN", workflow)
        self.assertNotIn("bridge_ref", workflow)
        self.assertIn("CODEX_PR_REVIEW_REPO_ROOT: ${{ github.workspace }}/source", workflow)
        self.assertIn("working-directory: source", workflow)
        self.assertIn("bridge/scripts/run_codex_pr_review.py", workflow)
        self.assertIn("Trusted Codex review script not found", workflow)
        self.assertNotIn("source/scripts/run_codex_pr_review.py", workflow)
        self.assertIn("source/data/output/codex_pr_review/", workflow)

    def test_repo_root_can_be_overridden_for_reusable_workflow(self) -> None:
        source = Path("scripts/run_codex_pr_review.py").read_text(encoding="utf-8")
        self.assertIn("CODEX_PR_REVIEW_REPO_ROOT", source)
        self.assertIn("BRIDGE_ROOT = Path(__file__).resolve().parents[1]", source)
        self.assertIn('PROMPT_TEMPLATE_PATH = BRIDGE_ROOT / "prompts" / "pr_review.md"', source)
