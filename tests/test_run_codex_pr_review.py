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

    def test_repository_policy_disables_auto_convergence(self) -> None:
        policy = run_codex_pr_review.load_policy()
        self.assertEqual(run_codex_pr_review.auto_converge_after_from_policy(policy), 0)
        self.assertTrue(run_codex_pr_review.block_on_review_failure_from_policy(policy))

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

    def test_service_failure_does_not_fallback_to_direct_api_when_disabled(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_AUDIT_SERVICE_URL": "https://service.example",
                    "CODEX_PR_REVIEW_API_FALLBACK_ENABLED": "false",
                },
                clear=True,
            ),
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
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "CODEX_PR_REVIEW_API_FALLBACK_ENABLED": "true",
                },
                clear=True,
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

    def test_direct_api_runs_when_service_url_unset_even_if_service_fallback_disabled(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "CODEX_PR_REVIEW_API_FALLBACK_ENABLED": "false",
                    "CODEX_PR_REVIEW_DIRECT_API_PRIMARY_ENABLED": "true",
                },
                clear=True,
            ),
            patch("scripts.run_codex_pr_review.run_direct_api_review", return_value="api review") as direct_api,
        ):
            output = run_codex_review_with_fallback("Review this PR.", timeout_minutes=20)

        self.assertEqual(output, "api review")
        direct_api.assert_called_once()

    def test_direct_api_is_blocked_when_service_url_unset_and_primary_disabled(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "CODEX_PR_REVIEW_DIRECT_API_PRIMARY_ENABLED": "false",
                },
                clear=True,
            ),
            patch("scripts.run_codex_pr_review.run_direct_api_review") as direct_api,
        ):
            with self.assertRaises(ReviewError) as raised:
                run_codex_review_with_fallback("Review this PR.", timeout_minutes=20)

        self.assertEqual(str(raised.exception), run_codex_pr_review.NO_REVIEW_BACKEND_CONFIGURED)
        direct_api.assert_not_called()


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

    def test_main_allows_high_risk_on_review_infra_error_with_human_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = self._write_event(tmpdir, ["src/app.py"])
            policy = run_codex_pr_review._default_policy()
            policy["pr_review"]["block_on_review_failure"] = False
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
                patch("scripts.run_codex_pr_review.fetch_pr_labels", return_value=[]),
                patch("scripts.run_codex_pr_review.load_policy", return_value=policy),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError("Codex service job timed out")),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 0)

        comment.assert_called_once()

    def test_main_allows_low_risk_docs_on_review_infra_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = self._write_event(tmpdir, ["docs/guide.md", "tests/test_x.py"])
            policy = run_codex_pr_review._default_policy()
            policy["pr_review"]["block_on_review_failure"] = False
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": event_path,
                "GITHUB_EVENT_NAME": "pull_request",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch("scripts.run_codex_pr_review.fetch_pr_files", return_value=[{"filename": "docs/guide.md"}, {"filename": "tests/test_x.py"}]),
                patch("scripts.run_codex_pr_review.fetch_pr_labels", return_value=[]),
                patch("scripts.run_codex_pr_review.load_policy", return_value=policy),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError("Codex service job timed out")),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 0)

        comment.assert_called_once()


    def test_main_allows_unconfigured_backend_with_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = self._write_event(tmpdir, ["scripts/run_codex_pr_review.py"])
            policy = run_codex_pr_review._default_policy()
            policy["pr_review"]["block_on_review_failure"] = False
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
                patch("scripts.run_codex_pr_review.fetch_pr_labels", return_value=[]),
                patch("scripts.run_codex_pr_review.load_policy", return_value=policy),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError(run_codex_pr_review.NO_REVIEW_BACKEND_CONFIGURED)),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 0)

        comment.assert_called_once()
        self.assertIn("Human review required", comment.call_args.args[3])

    def test_ack_labels_default_to_review_ack(self) -> None:
        self.assertEqual(
            run_codex_pr_review.ack_labels_from_policy({"version": 1}),
            frozenset({"review-ack"}),
        )
        self.assertEqual(
            run_codex_pr_review.ack_labels_from_policy(
                {"version": 1, "pr_review": {"ack_labels": ["review-ack", "ship-it"]}}
            ),
            frozenset({"review-ack", "ship-it"}),
        )

    def test_auto_converge_threshold_defaults_and_parses(self) -> None:
        self.assertEqual(run_codex_pr_review.auto_converge_after_from_policy({"version": 1}), 3)
        self.assertEqual(
            run_codex_pr_review.auto_converge_after_from_policy(
                {"version": 1, "pr_review": {"auto_converge_after": 2}}
            ),
            2,
        )
        self.assertEqual(
            run_codex_pr_review.auto_converge_after_from_policy(
                {"version": 1, "pr_review": {"auto_converge_after": 0}}
            ),
            0,
        )

    def test_block_on_review_failure_defaults_and_parses(self) -> None:
        self.assertTrue(run_codex_pr_review.block_on_review_failure_from_policy({"version": 1}))
        self.assertTrue(
            run_codex_pr_review.block_on_review_failure_from_policy(
                {"version": 1, "pr_review": {"block_on_review_failure": True}}
            )
        )
        self.assertTrue(
            run_codex_pr_review.block_on_review_failure_from_policy(
                {"version": 1, "pr_review": {"block_on_review_failure": "true"}}
            )
        )
        self.assertFalse(
            run_codex_pr_review.block_on_review_failure_from_policy(
                {"version": 1, "pr_review": {"block_on_review_failure": "false"}}
            )
        )
        self.assertTrue(
            run_codex_pr_review.block_on_review_failure_from_policy(
                {"version": 1, "pr_review": {"block_on_review_failure": "maybe"}}
            )
        )

    def test_blocking_streak_advances_and_auto_converges(self) -> None:
        self.assertEqual(run_codex_pr_review.parse_blocking_streak(""), 0)
        self.assertEqual(
            run_codex_pr_review.parse_blocking_streak(
                "<!-- codex-pr-review -->\n<!-- codex-pr-review-streak:2 -->\n"
            ),
            2,
        )
        self.assertEqual(run_codex_pr_review.next_blocking_streak(2, blocked=True), 3)
        self.assertEqual(run_codex_pr_review.next_blocking_streak(2, blocked=False), 0)
        self.assertTrue(run_codex_pr_review.should_auto_converge(3, blocked=True, threshold=3))
        self.assertFalse(run_codex_pr_review.should_auto_converge(2, blocked=True, threshold=3))
        self.assertFalse(run_codex_pr_review.should_auto_converge(3, blocked=False, threshold=3))

    def test_main_auto_converges_after_threshold_without_ack_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event = {
                "pull_request": {
                    "number": 7,
                    "title": "feat: risky",
                    "body": "",
                    "html_url": "https://example.test/pr/7",
                    "labels": [],
                    "head": {"sha": "abc123"},
                    "base": {"sha": "base123", "repo": {"full_name": "org/repo"}},
                }
            }
            event_path = Path(tmpdir) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_EVENT_NAME": "pull_request",
            }
            review_json = json.dumps(
                {
                    "summary": "blocking issue",
                    "findings": [
                        {
                            "severity": "high",
                            "category": "security",
                            "file": "scripts/run_codex_pr_review.py",
                            "line": 1,
                            "description": "example blocking finding",
                            "suggestion": "fix it",
                        }
                    ],
                }
            )
            prior_comment = (
                "<!-- codex-pr-review -->\n"
                "<!-- codex-pr-review-streak:2 -->\n"
                "## prior\n"
            )
            with (
                patch.dict(os.environ, env, clear=True),
                patch(
                    "scripts.run_codex_pr_review.fetch_pr_files",
                    return_value=[{"filename": "scripts/run_codex_pr_review.py"}],
                ),
                patch(
                    "scripts.run_codex_pr_review.fetch_pr_diff",
                    return_value="diff --git a/scripts/run_codex_pr_review.py b/scripts/run_codex_pr_review.py",
                ),
                patch("scripts.run_codex_pr_review.fetch_pr_labels", return_value=[]),
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch(
                    "scripts.run_codex_pr_review.find_existing_review_comment",
                    return_value=(99, prior_comment),
                ),
                patch(
                    "scripts.run_codex_pr_review.run_codex_review_with_fallback",
                    return_value=review_json,
                ),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 0)

        comment.assert_called_once()
        body = comment.call_args.args[3]
        self.assertIn("codex-pr-review-streak:3", body)
        self.assertIn("Auto-converged", body)

    def test_pr_has_ack_label_matches_configured_label(self) -> None:
        policy = {"version": 1, "pr_review": {"ack_labels": ["review-ack"]}}
        matched, label = run_codex_pr_review.pr_has_ack_label(
            {"labels": [{"name": "review-ack"}]},
            policy,
        )
        self.assertTrue(matched)
        self.assertEqual(label, "review-ack")
        matched, label = run_codex_pr_review.pr_has_ack_label(
            {"labels": [{"name": "enhancement"}]},
            policy,
        )
        self.assertFalse(matched)
        self.assertEqual(label, "")

    def test_main_passes_when_ack_label_present_despite_blocking_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event = {
                "pull_request": {
                    "number": 7,
                    "title": "feat: risky",
                    "body": "",
                    "html_url": "https://example.test/pr/7",
                    "labels": [{"name": "review-ack"}],
                    "head": {"sha": "abc123"},
                    "base": {"sha": "base123", "repo": {"full_name": "org/repo"}},
                }
            }
            event_path = Path(tmpdir) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_EVENT_NAME": "pull_request",
            }
            review_json = json.dumps(
                {
                    "summary": "blocking issue",
                    "findings": [
                        {
                            "severity": "high",
                            "category": "security",
                            "file": "scripts/run_codex_pr_review.py",
                            "line": 1,
                            "description": "example blocking finding",
                            "suggestion": "fix it",
                        }
                    ],
                }
            )
            with (
                patch.dict(os.environ, env, clear=True),
                patch(
                    "scripts.run_codex_pr_review.fetch_pr_files",
                    return_value=[{"filename": "scripts/run_codex_pr_review.py"}],
                ),
                patch(
                    "scripts.run_codex_pr_review.fetch_pr_diff",
                    return_value="diff --git a/scripts/run_codex_pr_review.py b/scripts/run_codex_pr_review.py",
                ),
                patch(
                    "scripts.run_codex_pr_review.fetch_pr_labels",
                    return_value=[{"name": "review-ack"}],
                ),
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch(
                    "scripts.run_codex_pr_review.find_existing_review_comment",
                    return_value=(None, ""),
                ),
                patch(
                    "scripts.run_codex_pr_review.run_codex_review_with_fallback",
                    return_value=review_json,
                ),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 0)

        comment.assert_called_once()
        body = comment.call_args.args[3]
        self.assertIn("review-ack", body)
        self.assertIn("will not block merge", body)

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
                patch("scripts.run_codex_pr_review.fetch_pr_labels", return_value=[]),
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError(run_codex_pr_review.NO_REVIEW_BACKEND_CONFIGURED)),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 1)

        comment.assert_called_once()
        self.assertIn("Human review required", comment.call_args.args[3])

    def test_main_fails_closed_on_infrastructure_failure_when_policy_requires_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = self._write_event(tmpdir, ["scripts/run_codex_pr_review.py"])
            policy_path = Path(tmpdir) / ".github" / "codex_auto_merge_policy.json"
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pr_review": {
                            "block_on_review_failure": True,
                            "auto_converge_after": 3,
                        },
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": event_path,
                "GITHUB_EVENT_NAME": "pull_request",
                "CODEX_PR_REVIEW_REPO_ROOT": tmpdir,
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch("scripts.run_codex_pr_review.fetch_pr_files", return_value=[{"filename": "scripts/run_codex_pr_review.py"}]),
                patch("scripts.run_codex_pr_review.fetch_pr_diff", return_value="diff --git a/scripts/run_codex_pr_review.py b/scripts/run_codex_pr_review.py"),
                patch("scripts.run_codex_pr_review.fetch_pr_labels", return_value=[]),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError("Codex service job timed out")),
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

    def test_oidc_repo_not_allowed_counts_as_unconfigured_backend(self) -> None:
        exc = ReviewError('Codex service request failed: 401 {"status":"error","error":"OIDC repository is not allowed"}')
        self.assertTrue(run_codex_pr_review._review_backend_is_unconfigured(exc))

    def test_service_exec_failure_falls_back_to_direct_api(self) -> None:
        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://service.example"}, clear=True),
            patch(
                "scripts.run_codex_pr_review.run_codex_service_review",
                side_effect=ReviewError("Codex service job failed: codex exec failed (rc=1): boom"),
            ),
            patch(
                "scripts.run_codex_pr_review.run_direct_api_review",
                return_value='{"findings":[]}',
            ) as direct_api,
        ):
            output = run_codex_review_with_fallback(
                "Review this PR.",
                timeout_minutes=20,
                complexity="high",
                changed_file_count=3,
                changed_line_count=120,
            )

        direct_api.assert_called_once()
        self.assertEqual(output, '{"findings":[]}')

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

    def test_service_review_includes_router_model(self) -> None:
        responses = [
            {"job_id": "job-1"},
            {"status": "succeeded", "output": "{}"},
        ]
        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://service.example", "GITHUB_REPOSITORY": "org/repo"}, clear=True),
            patch("scripts.run_codex_pr_review.request_github_oidc_token", side_effect=["submit-token", "poll-token"]),
            patch("scripts.run_codex_pr_review._service_request", side_effect=responses) as request,
            patch("scripts.run_codex_pr_review.route_model", return_value={"model": "gpt-5.6-sol"}),
            patch("scripts.run_codex_pr_review.time.sleep"),
        ):
            output = run_codex_pr_review.run_codex_service_review("prompt", timeout_minutes=1)

        self.assertEqual(output, "{}")
        self.assertEqual(request.call_args_list[0].args[3]["model"], "gpt-5.6-sol")


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
        self.assertIn("direct_api_primary_enabled", workflow)
        self.assertIn("Allow direct API fallback", workflow)
        self.assertIn("Allow API-only PR review", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("type: boolean", workflow)
        self.assertIn("CODEX_PR_REVIEW_ALLOW_UNCONFIGURED_BACKEND", workflow)
        self.assertIn("CODEX_PR_REVIEW_API_FALLBACK_ENABLED", workflow)
        self.assertIn("CODEX_PR_REVIEW_DIRECT_API_PRIMARY_ENABLED", workflow)
        self.assertIn("CODEX_PR_REVIEW_REUSABLE_CALL", workflow)
        self.assertIn("CODEX_PR_REVIEW_API_FALLBACK_INPUT", workflow)
        self.assertIn("CODEX_PR_REVIEW_DIRECT_API_PRIMARY_INPUT", workflow)
        self.assertIn("resolve_boolean", workflow)
        self.assertIn("must be true or false", workflow)
        self.assertIn("tr '[:upper:]' '[:lower:]'", workflow)
        self.assertIn('if ! api_fallback_enabled="$(resolve_boolean', workflow)
        self.assertIn("inputs.caller_concurrency_key || github.event.pull_request.number || github.run_id", workflow)
        self.assertNotIn("Validate bridge checkout token", workflow)
        self.assertIn("required: false", workflow)
        self.assertIn("job.workflow_repository", workflow)
        self.assertNotIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("github.event.pull_request.head.sha", workflow)
        self.assertIn("Validate AIAuditBridge self-review ref", workflow)
        self.assertIn("AIAuditBridge self-review requires pull_request_target with a PR head SHA.", workflow)
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
        self.assertIn("if str(BRIDGE_ROOT) not in sys.path:", source)
        self.assertIn('PROMPT_TEMPLATE_PATH = BRIDGE_ROOT / "prompts" / "pr_review.md"', source)
