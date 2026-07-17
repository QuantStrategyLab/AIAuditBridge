from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.run_codex_pr_review as run_codex_pr_review
from scripts.run_codex_pr_review import ReviewError, run_codex_review_with_fallback


class RunCodexPrReviewTests(unittest.TestCase):
    def _write_event(self, tmpdir: str, files: list[str]) -> str:
        event = {
            "pull_request": {"number": 7, "head": {"sha": "abc1234"}},
        }
        path = Path(tmpdir) / "event.json"
        path.write_text(
            json.dumps(event),
            encoding="utf-8",
        )
        return str(path)

    def _run_main_with_review(
        self, tmpdir: str, output: str | Exception, previous_comment: str = ""
    ) -> tuple[int, object, object, dict[str, object]]:
        env = {
            "GH_TOKEN": "token",
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_EVENT_PATH": self._write_event(
                tmpdir, ["scripts/run_codex_pr_review.py"]
            ),
        }
        backend_patch = (
            patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=output)
            if isinstance(output, Exception)
            else patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", return_value=output)
        )
        previous_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("scripts.run_codex_pr_review.fetch_pr_files", return_value=[{"filename": "scripts/run_codex_pr_review.py"}]),
                patch("scripts.run_codex_pr_review.fetch_pr_diff", return_value="current diff"),
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(99, previous_comment)),
                backend_patch as backend,
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                result = run_codex_pr_review.main()
        finally:
            os.chdir(previous_cwd)
        decision = json.loads(
            (Path(tmpdir) / "data/output/codex_pr_review/decision.json").read_text(encoding="utf-8")
        )
        return result, backend, comment, decision

    def test_changed_files_are_low_risk_only_for_docs_and_tests(self) -> None:
        policy = run_codex_pr_review.load_policy()
        self.assertTrue(run_codex_pr_review.changed_files_are_low_risk(["docs/guide.md", "tests/test_x.py"], policy))
        self.assertFalse(run_codex_pr_review.changed_files_are_low_risk(["src/app.py"], policy))

    def test_review_prompt_states_direct_oidc_contract(self) -> None:
        prompt = run_codex_pr_review.build_review_prompt("diff", "title", "", "org/repo")
        self.assertIn("`job_workflow_ref` is absent for explicit direct callers", prompt)
        self.assertIn("Do not emit a finding that concludes no code change is needed", prompt)

    def test_review_prompt_requires_holistic_contract_review(self) -> None:
        prompt = run_codex_pr_review.build_review_prompt(
            "diff",
            "clean-slate contract",
            "Legacy compatibility is explicitly out of scope.",
            "org/repo",
        )
        self.assertIn("report all independent actionable findings in one response", prompt)
        self.assertIn("Do not stop after the first blocking issue", prompt)
        self.assertIn("current exact-head diff", prompt)
        self.assertIn("clean-slate", prompt)
        self.assertIn("optional-key presence versus explicit null", prompt)
        self.assertIn("every identity-bearing integer", prompt)
        self.assertIn("one canonical timestamp representation", prompt)

    def test_review_prompt_requires_reachability_evidence_for_blockers(self) -> None:
        prompt = run_codex_pr_review.build_review_prompt(
            "diff",
            "bounded implementation",
            "Future consumers are out of scope.",
            "org/repo",
        )

        self.assertIn("current caller or entry point proven by the supplied PR context", prompt)
        self.assertIn("introduced by this PR", prompt)
        self.assertIn("explicitly declared public untrusted boundary", prompt)
        self.assertIn("current configuration and inputs", prompt)
        self.assertIn("downgrade it to medium or low", prompt)
        self.assertIn("hypothetical future consumer", prompt)
        self.assertIn("Do not request a new parser, store, registry, or event-persistence layer", prompt)

    def test_repository_review_template_uses_the_same_reachability_gate(self) -> None:
        template = run_codex_pr_review.PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")

        self.assertIn("current caller or entry point proven by the supplied PR context", template)
        self.assertIn("introduced by this PR", template)
        self.assertIn("explicitly declared public untrusted boundary", template)
        self.assertIn("downgrade it to medium or low", template)
        self.assertIn("hypothetical future consumer", template)

    def test_review_script_never_imports_from_the_pr_checkout(self) -> None:
        source = Path(run_codex_pr_review.__file__).read_text(encoding="utf-8")
        self.assertNotIn("SOURCE_ROOT = BRIDGE_ROOT.parent / \"source\"", source)

    def test_isolated_review_runtime_imports_from_the_trusted_bridge(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(run_codex_pr_review.__file__))],
            env={"GITHUB_EVENT_PATH": "does-not-exist"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("GH_TOKEN or GITHUB_TOKEN is required", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_parse_review_output_accepts_a_valid_json_prefix(self) -> None:
        self.assertEqual(
            run_codex_pr_review.parse_review_output('{"summary":"ok","findings":[]}\nReviewer metadata follows.'),
            {"summary": "ok", "findings": []},
        )
        with self.assertRaisesRegex(ReviewError, "findings"):
            run_codex_pr_review.parse_review_output('{"ok":true}\nReviewer metadata follows.')

    def test_existing_review_comment_ignores_forged_marker(self) -> None:
        forged = {
            "id": 1,
            "body": "<!-- codex-pr-review -->\nforged state",
            "user": {"login": "attacker"},
        }
        trusted = {
            "id": 2,
            "body": "<!-- codex-pr-review -->\ntrusted",
            "user": {"id": 418, "login": "github-actions[bot]", "type": "Bot"},
            "created_at": "2026-07-12T00:00:00Z",
        }
        with patch("scripts.run_codex_pr_review.github_request", return_value=[forged, trusted]):
            comment = run_codex_pr_review.find_existing_review_comment("token", "org/repo", 7)

        self.assertEqual(comment, (2, trusted["body"]))

    def test_review_comment_records_implementation_identity(self) -> None:
        body = run_codex_pr_review.build_pr_comment(
            {"summary": "ok", "blocking_findings": [], "non_blocking_findings": []},
            "https://example.test/pr/7",
        )
        self.assertEqual(
            run_codex_pr_review.parse_review_implementation_digest(body),
            run_codex_pr_review.review_implementation_digest(),
        )

    def test_repository_policy_has_no_bypass_fields(self) -> None:
        policy = run_codex_pr_review.load_policy()
        self.assertTrue(
            {"ack_labels", "auto_converge_after", "block_on_review_failure"}.isdisjoint(policy["pr_review"])
        )
        self.assertEqual(run_codex_pr_review._default_policy()["pr_review"], {})

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

    def test_main_blocks_high_risk_on_review_infra_error(self) -> None:
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
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch(
                    "scripts.run_codex_pr_review.run_codex_review_with_fallback",
                    side_effect=ReviewError("Codex service job timed out"),
                ) as backend,
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 1)

        comment.assert_called_once()
        backend.assert_called_once()
        self.assertIn("Review incomplete", comment.call_args.args[3])

    def test_main_does_not_carry_blockers_across_heads(self) -> None:
        prior = """<!-- codex-pr-review -->
<!-- codex-pr-review-streak:3 -->
<!-- codex-pr-review-fingerprint:deadbeef -->
<!-- codex-pr-review-head-sha:deadbeef -->
<!-- codex-pr-review-history:v1:prior -->"""
        with tempfile.TemporaryDirectory() as tmpdir:
            result, backend, comment, _decision = self._run_main_with_review(
                tmpdir, '{"summary":"clear","findings":[]}', prior
            )

        self.assertEqual(result, 0)
        backend.assert_called_once()
        body = comment.call_args.args[3]
        self.assertIn("codex-pr-review-head-sha:abc1234", body)
        for historical_marker in ("streak", "fingerprint", "history"):
            self.assertNotIn(f"codex-pr-review-{historical_marker}", body)

    def test_backend_failure_is_retryable_and_not_a_successful_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _backend, comment, decision = self._run_main_with_review(
                tmpdir, ReviewError("quota_or_capacity_failure")
            )

        self.assertEqual(result, 1)
        self.assertFalse(decision["review_completed"])
        body = comment.call_args.args[3]
        self.assertIn("codex-pr-review-completed:false", body)
        self.assertIn("external retryable failure", body)
        self.assertNotIn("Merge allowed", body)

    def test_parse_failure_records_external_retry_without_review_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _backend, comment, decision = self._run_main_with_review(
                tmpdir, "not json"
            )

        self.assertEqual(result, 1)
        self.assertFalse(decision["review_completed"])
        self.assertEqual(decision["reviewed_head_sha"], "")
        self.assertEqual(decision["current_head_sha"], "abc1234")
        self.assertEqual(decision["failure_kind"], "external_retryable")
        self.assertIn("codex-pr-review-completed:false", comment.call_args.args[3])

    def test_main_retries_when_review_quota_is_unavailable(self) -> None:
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
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch(
                    "scripts.run_codex_pr_review.run_codex_review_with_fallback",
                    side_effect=ReviewError("Codex service job failed [quota_or_capacity_failure]: usage limits reached"),
                ),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 1)

        comment.assert_called_once()
        self.assertIn("Review incomplete", comment.call_args.args[3])
        self.assertNotIn("Merge blocked", comment.call_args.args[3])

    def test_main_retries_when_daily_budget_is_exhausted(self) -> None:
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
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch(
                    "scripts.run_codex_pr_review.run_codex_review_with_fallback",
                    side_effect=ReviewError(
                        'Codex service request failed: 429 {"error": "Daily budget exceeded: '
                        '$0.0000 remaining, $0.0500 needed"}'
                    ),
                ),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 1)

        self.assertIn("Review incomplete", comment.call_args.args[3])
        self.assertNotIn("Merge blocked", comment.call_args.args[3])

    def test_main_retries_when_codex_exec_failure_is_unclassified(self) -> None:
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
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch(
                    "scripts.run_codex_pr_review.run_codex_review_with_fallback",
                    side_effect=ReviewError("Codex service job failed [unknown_failure]: codex exec failed (rc=1)"),
                ),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 1)

        self.assertIn("Review incomplete", comment.call_args.args[3])

    def test_main_skips_low_risk_docs_before_calling_review_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = self._write_event(tmpdir, ["docs/guide.md", "tests/test_x.py"])
            policy = run_codex_pr_review._default_policy()
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": event_path,
                "GITHUB_EVENT_NAME": "pull_request",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch("scripts.run_codex_pr_review.fetch_pr_files", return_value=[{"filename": "docs/guide.md"}, {"filename": "tests/test_x.py"}]),
                patch("scripts.run_codex_pr_review.load_policy", return_value=policy),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch(
                    "scripts.run_codex_pr_review.run_codex_review_with_fallback",
                    side_effect=ReviewError("Codex service job timed out"),
                ) as backend,
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 0)

        comment.assert_called_once()
        backend.assert_not_called()

    def test_main_blocks_unconfigured_backend_even_with_legacy_opt_in(self) -> None:
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
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError(run_codex_pr_review.NO_REVIEW_BACKEND_CONFIGURED)),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 1)

        comment.assert_called_once()
        self.assertIn("Review incomplete", comment.call_args.args[3])

    def test_main_ignores_legacy_bypass_policy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event = {
                "pull_request": {
                    "number": 7,
                    "title": "feat: risky",
                    "body": "",
                    "html_url": "https://example.test/pr/7",
                    "labels": [{"name": "review-ack"}],
                    "head": {"sha": "abc1234"},
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
                    "scripts.run_codex_pr_review.load_policy",
                    return_value={
                        "version": 1,
                        "pr_review": {
                            "ack_labels": ["review-ack"],
                            "auto_converge_enabled": True,
                            "auto_converge_after": 1,
                            "block_on_review_failure": True,
                        },
                    },
                ),
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
                self.assertEqual(run_codex_pr_review.main(), 1)

        comment.assert_called_once()
        body = comment.call_args.args[3]
        self.assertNotIn("will not block merge", body)

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
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError(run_codex_pr_review.NO_REVIEW_BACKEND_CONFIGURED)),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 1)

        comment.assert_called_once()
        self.assertIn("Review incomplete", comment.call_args.args[3])

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
                patch("scripts.run_codex_pr_review.find_existing_review_comment", return_value=(None, "")),
                patch("scripts.run_codex_pr_review.run_codex_review_with_fallback", side_effect=ReviewError("Codex service job timed out")),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 1)

        comment.assert_called_once()
        self.assertIn("Review incomplete", comment.call_args.args[3])

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

    def test_service_review_uses_deployed_cli_model(self) -> None:
        responses = [
            {"job_id": "job-1"},
            {"status": "succeeded", "output": "{}"},
        ]
        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://service.example", "GITHUB_REPOSITORY": "org/repo"}, clear=True),
            patch("scripts.run_codex_pr_review.request_github_oidc_token", side_effect=["submit-token", "poll-token"]),
            patch("scripts.run_codex_pr_review._service_request", side_effect=responses) as request,
            patch("scripts.run_codex_pr_review.time.sleep"),
        ):
            output = run_codex_pr_review.run_codex_service_review("prompt", timeout_minutes=1)

        self.assertEqual(output, "{}")
        self.assertNotIn("model", request.call_args_list[0].args[3])


class CodexPrReviewWorkflowTest(unittest.TestCase):
    def test_reusable_workflow_runs_bridge_script_against_source_checkout(self) -> None:
        workflow = Path(".github/workflows/codex_pr_review.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("  pull_request:\n    types: [opened, synchronize, reopened]", workflow)
        self.assertNotIn("  review:\n    if:", workflow)
        self.assertIn("Reject unsupported fork pull requests", workflow)
        self.assertIn("Codex review is not configured for fork pull requests", workflow)
        self.assertIn("path: source", workflow)
        self.assertIn("path: bridge", workflow)
        self.assertIn("CODEX_AUDIT_REUSABLE_WORKFLOW_TOKEN", workflow)
        self.assertIn("caller_concurrency_key", workflow)
        self.assertIn("allow_unconfigured_backend", workflow)
        self.assertIn("api_fallback_enabled", workflow)
        self.assertIn("direct_api_primary_enabled", workflow)
        self.assertIn("Optional true/false override for direct API fallback", workflow)
        self.assertIn("Optional true/false override for API-only PR review", workflow)
        self.assertIn('default: "false"', workflow)
        self.assertIn("type: string", workflow)
        self.assertNotIn("CODEX_PR_REVIEW_ALLOW_UNCONFIGURED_BACKEND", workflow)
        self.assertIn("CODEX_PR_REVIEW_API_FALLBACK_ENABLED", workflow)
        self.assertIn("CODEX_PR_REVIEW_DIRECT_API_PRIMARY_ENABLED", workflow)
        self.assertIn("CODEX_PR_REVIEW_REUSABLE_CALL", workflow)
        self.assertIn("CODEX_PR_REVIEW_API_FALLBACK_INPUT", workflow)
        self.assertIn("CODEX_PR_REVIEW_DIRECT_API_PRIMARY_INPUT", workflow)
        self.assertIn("resolve_boolean", workflow)
        self.assertIn("must be true or false", workflow)
        self.assertIn("tr '[:upper:]' '[:lower:]'", workflow)
        self.assertIn('if ! api_fallback_enabled="$(resolve_boolean', workflow)
        self.assertIn('timeout --signal=TERM --kill-after=60s 25m python -I "${script_path}"', workflow)
        self.assertIn("inputs.caller_concurrency_key || github.event.pull_request.number || github.run_id", workflow)
        self.assertNotIn("Validate bridge checkout token", workflow)
        self.assertIn("required: false", workflow)
        self.assertIn("job.workflow_repository", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
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
