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

    def test_review_prompt_states_direct_oidc_contract(self) -> None:
        prompt = run_codex_pr_review.build_review_prompt("diff", "title", "", "org/repo")
        self.assertIn("`job_workflow_ref` is absent for explicit direct callers", prompt)
        self.assertIn("Do not emit a finding that concludes no code change is needed", prompt)

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

    def test_repeated_findings_are_fingerprinted_before_arbitration(self) -> None:
        findings = [
            {
                "severity": "high",
                "category": "logic",
                "file": "service/review.py",
                "line": 11,
                "description": "Leaves a failing review check green after retry.",
                "suggestion": "Return a non-zero result.",
            }
        ]
        reordered = [dict(findings[0], line=42)]
        reworded = [dict(findings[0], description="The retry path incorrectly returns success.")]
        other = [dict(findings[0], file="service/auth.py")]
        reclassified = [dict(findings[0], severity="critical")]

        fingerprint = run_codex_pr_review.blocking_finding_fingerprint(findings)
        fingerprints = run_codex_pr_review.blocking_finding_fingerprints(findings)
        self.assertEqual(fingerprint, run_codex_pr_review.blocking_finding_fingerprint(reordered))
        self.assertEqual(fingerprint, run_codex_pr_review.blocking_finding_fingerprint(reworded))
        self.assertNotEqual(fingerprint, run_codex_pr_review.blocking_finding_fingerprint(other))
        self.assertNotEqual(fingerprint, run_codex_pr_review.blocking_finding_fingerprint(reclassified))
        self.assertEqual(fingerprints, run_codex_pr_review.blocking_finding_fingerprints(reworded))
        self.assertEqual(
            run_codex_pr_review.next_blocking_streak(
                1,
                blocked=True,
                previous_fingerprint=fingerprint,
                current_fingerprint=fingerprint,
                previous_head_sha="deadbeef",
                current_head_sha="feedface",
            ),
            2,
        )
        self.assertEqual(
            run_codex_pr_review.next_blocking_streak(
                2,
                blocked=True,
                previous_fingerprint=fingerprint,
                current_fingerprint=run_codex_pr_review.blocking_finding_fingerprint(other),
                previous_head_sha="deadbeef",
                current_head_sha="feedface",
            ),
            1,
        )
        self.assertEqual(
            run_codex_pr_review.next_blocking_streak(
                1,
                blocked=True,
                previous_fingerprint=fingerprint,
                current_fingerprint=fingerprint,
                previous_head_sha="deadbeef",
                current_head_sha="deadbeef",
            ),
            1,
        )
        self.assertTrue(run_codex_pr_review.should_arbitrate(blocked=True, streak=2, repeated=True, new_head=True))
        self.assertFalse(run_codex_pr_review.should_arbitrate(blocked=True, streak=2, repeated=True, new_head=False))

    def test_parse_arbitration_output_requires_supported_verdict(self) -> None:
        self.assertEqual(
            run_codex_pr_review.parse_arbitration_output('{"verdict":"clear","reason":"covered by the regression test"}'),
            {"verdict": "clear", "reason": "covered by the regression test"},
        )
        with self.assertRaisesRegex(ReviewError, "verdict"):
            run_codex_pr_review.parse_arbitration_output('{"verdict":"maybe"}')
        self.assertEqual(
            run_codex_pr_review.parse_arbitration_output(
                "The pattern is `{7,64}`.\n```json\n{\"verdict\":\"clear\",\"reason\":\"fixed\"}\n```"
            ),
            {"verdict": "clear", "reason": "fixed"},
        )

    def test_parse_review_output_accepts_a_valid_json_prefix(self) -> None:
        self.assertEqual(
            run_codex_pr_review.parse_review_output('{"summary":"ok","findings":[]}\nReviewer metadata follows.'),
            {"summary": "ok", "findings": []},
        )
        with self.assertRaisesRegex(ReviewError, "findings"):
            run_codex_pr_review.parse_review_output('{"ok":true}\nReviewer metadata follows.')

    def test_existing_review_comment_ignores_forged_marker(self) -> None:
        forged = {"id": 1, "body": "<!-- codex-pr-review -->\nforged", "user": {"login": "attacker"}}
        trusted = {
            "id": 2,
            "body": "<!-- codex-pr-review -->\ntrusted",
            "user": {"login": "github-actions[bot]"},
        }
        with patch("scripts.run_codex_pr_review.github_request", return_value=[forged, trusted]):
            self.assertEqual(
                run_codex_pr_review.find_existing_review_comment("token", "org/repo", 7),
                (2, trusted["body"]),
            )

    def test_legacy_comment_fingerprints_are_recovered_per_finding(self) -> None:
        body = "#### 1. 🟠 [HIGH] Security in `service/auth.py`\n"
        expected = run_codex_pr_review.blocking_finding_fingerprints(
            [{"severity": "high", "category": "security", "file": "service/auth.py"}]
        )
        self.assertEqual(run_codex_pr_review.parse_blocking_fingerprints(body), expected)

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
        self.assertIn("Merge blocked", comment.call_args.args[3])

    def test_main_allows_required_ci_to_gate_when_review_quota_is_unavailable(self) -> None:
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
                self.assertEqual(run_codex_pr_review.main(), 0)

        comment.assert_called_once()
        self.assertIn("Review unavailable", comment.call_args.args[3])
        self.assertNotIn("Merge blocked", comment.call_args.args[3])

    def test_main_allows_required_ci_to_gate_when_codex_exec_failure_is_unclassified(self) -> None:
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
                self.assertEqual(run_codex_pr_review.main(), 0)

        self.assertIn("Review unavailable", comment.call_args.args[3])

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
        self.assertIn("Merge blocked", comment.call_args.args[3])

    def test_blocking_streak_requires_a_matching_fingerprint(self) -> None:
        self.assertEqual(run_codex_pr_review.parse_blocking_streak(""), 0)
        self.assertEqual(
            run_codex_pr_review.parse_blocking_streak(
                "<!-- codex-pr-review -->\n<!-- codex-pr-review-streak:2 -->\n"
            ),
            2,
        )
        self.assertEqual(
            run_codex_pr_review.next_blocking_streak(
                2,
                blocked=True,
                previous_fingerprint="same",
                current_fingerprint="same",
                previous_head_sha="deadbeef",
                current_head_sha="feedface",
            ),
            3,
        )
        self.assertEqual(
            run_codex_pr_review.next_blocking_streak(
                2,
                blocked=True,
                previous_fingerprint="old",
                current_fingerprint="new",
            ),
            1,
        )
        self.assertEqual(run_codex_pr_review.next_blocking_streak(2, blocked=False), 0)

    def test_main_clears_repeated_finding_only_after_arbitration(self) -> None:
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
            fingerprint = run_codex_pr_review.blocking_finding_fingerprint(json.loads(review_json)["findings"])
            fingerprints = run_codex_pr_review.blocking_finding_fingerprints(json.loads(review_json)["findings"])
            prior_comment = (
                "<!-- codex-pr-review -->\n"
                "<!-- codex-pr-review-streak:1 -->\n"
                f"<!-- codex-pr-review-fingerprint:{fingerprint} -->\n"
                f"<!-- codex-pr-review-fingerprints:{','.join(fingerprints)} -->\n"
                "<!-- codex-pr-review-head-sha:deadbeef -->\n"
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
                patch("scripts.run_codex_pr_review.load_policy", return_value=run_codex_pr_review._default_policy()),
                patch(
                    "scripts.run_codex_pr_review.find_existing_review_comment",
                    return_value=(99, prior_comment),
                ),
                patch(
                    "scripts.run_codex_pr_review.run_codex_review_with_fallback",
                    side_effect=[review_json, '{"verdict":"clear","reason":"The regression test covers the reported behavior."}'],
                ),
                patch("scripts.run_codex_pr_review.upsert_pr_comment") as comment,
            ):
                self.assertEqual(run_codex_pr_review.main(), 0)

        comment.assert_called_once()
        body = comment.call_args.args[3]
        self.assertIn("codex-pr-review-streak:0", body)
        self.assertIn("codex-pr-review-fingerprints:", body)
        self.assertIn("codex-pr-review-head-sha:abc123", body)
        self.assertIn("Codex Review Arbitration", body)
        self.assertIn("clear", body)

    def test_main_ignores_legacy_bypass_policy_fields(self) -> None:
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
        self.assertIn("Merge blocked", comment.call_args.args[3])

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
        self.assertIn("Merge blocked", comment.call_args.args[3])

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
