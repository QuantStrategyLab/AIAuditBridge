from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import call, patch

from scripts.run_monthly_codex_audit import (
    BridgeError,
    DEFAULT_GUARDED_AUTO_MERGE_POLICY,
    GUARDED_AUTO_MERGE_LABEL,
    HUMAN_REVIEW_LABEL,
    GitHubRequestError,
    SOURCE_REPO_TASKS,
    api_fallback_allowed_source_repos,
    api_fallback_allow_fix,
    apply_service_changes,
    blocked_paths,
    build_api_review_prompt,
    build_service_prompt,
    classify_service_failure,
    classify_guarded_auto_merge_risk,
    codex_service_api_url,
    codex_service_job_url,
    codex_service_jobs_url,
    convert_local_markdown_links,
    create_pull_request,
    extract_anthropic_text,
    extract_openai_text,
    auto_fallback_missing_api_key_message,
    fetch_issue_comments,
    format_api_review_comment,
    git_diff_stats,
    format_guarded_risk_details,
    guarded_auto_merge_label,
    guarded_auto_merge_label_for_mutation,
    issue_has_label,
    load_guarded_auto_merge_policy,
    latest_feedback_pr_number,
    main as run_audit_main,
    normalize_codex_service_url,
    parse_service_patch_response,
    parse_bool,
    pr_closing_line,
    remove_issue_label_if_present,
    request_github_oidc_token,
    request_codex_service,
    request_guarded_auto_merge,
    request_human_review,
    RemediationWorkspace,
    resolve_feedback_retry_pr,
    resolve_source_repo_token,
    run_configured_api_reviews,
    run_api_patch_provider,
    run_auto_provider_fallback,
    service_failure_category,
    is_service_infrastructure_failure,
    safe_branch_component,
    strip_audit_heading,
    validate_codex_backend,
    validate_api_fallback_source_repo,
    validate_provider,
    validate_repo,
    validate_task,
    default_provider_for_task,
    write_codex_context,
)
from scripts.codex_audit_service import CodexAuditServiceRequestHandler, _codex_env
from scripts import codex_audit_service


def _normalized_policy(policy: dict[str, object]) -> dict[str, object]:
    normalized = dict(policy)
    normalized["blocked_path_patterns"] = sorted(policy.get("blocked_path_patterns", []))
    risk_policy = dict(policy["risk_policy"])
    low = dict(risk_policy["low"])
    medium = dict(risk_policy["medium"])
    low["prefixes"] = sorted(low.get("prefixes", []))
    low["exact"] = sorted(low.get("exact", []))
    medium["exact"] = sorted(medium.get("exact", []))
    risk_policy["low"] = low
    risk_policy["medium"] = medium
    normalized["risk_policy"] = risk_policy
    return normalized


class RunMonthlyCodexAuditTests(unittest.TestCase):
    def test_parse_bool_accepts_common_true_values(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on", True):
            self.assertTrue(parse_bool(value))
        for value in ("", "false", "0", "no", False, None):
            self.assertFalse(parse_bool(value))

    def test_validate_repo_accepts_owner_repo(self) -> None:
        for source_repo in SOURCE_REPO_TASKS:
            with self.subTest(source_repo=source_repo):
                self.assertEqual(validate_repo(source_repo), source_repo)

    def test_codex_service_api_url_maps_legacy_endpoint_to_ai_route(self) -> None:
        self.assertEqual(
            codex_service_api_url("https://codex.quant.example/v1/codex-audit", "/v1/ai/feedback/register"),
            "https://codex.quant.example/v1/ai/feedback/register",
        )

    def test_validate_repo_rejects_invalid_values(self) -> None:
        with self.assertRaises(Exception):
            validate_repo("QuantStrategyLab/CryptoLivePoolPipelines/extra")
        with self.assertRaises(Exception):
            validate_repo("OtherOrg/CryptoLivePoolPipelines")

    def test_source_repo_task_mapping_matches_known_dispatchers(self) -> None:
        expected = {
            "QuantStrategyLab/CryptoLivePoolPipelines": "monthly_snapshot_audit",
            "QuantStrategyLab/HkEquitySnapshotPipelines": "monthly_snapshot_audit",
            "QuantStrategyLab/ResearchSignalContextPipelines": "long_horizon_signal_shadow",
            "QuantStrategyLab/UsEquitySnapshotPipelines": "monthly_snapshot_audit",
        }

        self.assertEqual(set(SOURCE_REPO_TASKS), set(expected))
        for source_repo, task in expected.items():
            with self.subTest(source_repo=source_repo):
                self.assertEqual(validate_task(task, source_repo), task)

    def test_validate_task_rejects_repo_task_mismatch(self) -> None:
        self.assertEqual(
            validate_task("long-horizon-signal-shadow", "QuantStrategyLab/ResearchSignalContextPipelines"),
            "long_horizon_signal_shadow",
        )
        self.assertEqual(
            validate_task("", "QuantStrategyLab/CryptoLivePoolPipelines"),
            "monthly_snapshot_audit",
        )
        self.assertEqual(
            validate_task("monthly_snapshot_audit", "QuantStrategyLab/HkEquitySnapshotPipelines"),
            "monthly_snapshot_audit",
        )
        with self.assertRaises(Exception):
            validate_task("monthly_snapshot_audit", "QuantStrategyLab/ResearchSignalContextPipelines")

    def test_validate_provider_accepts_supported_values(self) -> None:
        self.assertEqual(validate_provider(""), "auto")
        self.assertEqual(validate_provider("", task="long_horizon_signal_shadow"), "codex")
        self.assertEqual(validate_provider("task_default", task="long_horizon_signal_shadow"), "codex")
        self.assertEqual(validate_provider("codex"), "codex")
        self.assertEqual(validate_provider("OPENAI"), "openai")
        self.assertEqual(validate_provider("anthropic"), "anthropic")
        self.assertEqual(validate_provider("api"), "api")
        self.assertEqual(validate_provider("auto"), "auto")
        with self.assertRaises(Exception):
            validate_provider("claude")

    def test_default_provider_for_task_is_task_specific(self) -> None:
        self.assertEqual(default_provider_for_task("monthly_snapshot_audit"), "auto")
        self.assertEqual(default_provider_for_task("long_horizon_signal_shadow"), "codex")
        self.assertEqual(default_provider_for_task("unknown_task"), "auto")

    def test_api_fallback_allowlist_requires_explicit_configuration(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(BridgeError, "must explicitly list"):
                api_fallback_allowed_source_repos()
            with self.assertRaisesRegex(BridgeError, "must explicitly list"):
                validate_api_fallback_source_repo("QuantStrategyLab/CryptoLivePoolPipelines")

    def test_api_fallback_allowlist_can_restrict_source_repositories(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES": (
                    "QuantStrategyLab/CryptoLivePoolPipelines"
                )
            },
            clear=True,
        ):
            self.assertEqual(
                api_fallback_allowed_source_repos(),
                frozenset({"QuantStrategyLab/CryptoLivePoolPipelines"}),
            )
            with self.assertRaisesRegex(BridgeError, "API fallback is not allowed"):
                validate_api_fallback_source_repo("QuantStrategyLab/HkEquitySnapshotPipelines")

    def test_api_fallback_allowlist_rejects_unknown_repositories(self) -> None:
        with patch.dict(
            os.environ,
            {"CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES": "OtherOrg/Repo"},
            clear=True,
        ):
            with self.assertRaisesRegex(BridgeError, "unsupported repositories"):
                api_fallback_allowed_source_repos()

    def test_validate_codex_backend_accepts_only_service(self) -> None:
        self.assertEqual(validate_codex_backend(""), "service")
        self.assertEqual(validate_codex_backend("service"), "service")
        with self.assertRaises(Exception):
            validate_codex_backend("local")
        with self.assertRaises(Exception):
            validate_codex_backend("ssh")

    def test_safe_branch_component_removes_unsafe_characters(self) -> None:
        self.assertEqual(safe_branch_component("issue #12: monthly review"), "issue-12-monthly-review")

    def test_blocked_paths_blocks_data_and_secret_like_files(self) -> None:
        blocked = blocked_paths(["data/output/report.json", "docs/secret-token.md", "scripts/fix.py"])
        self.assertEqual(blocked, ["data/output/report.json", "docs/secret-token.md"])

    def test_blocked_paths_allows_long_horizon_shadow_outputs(self) -> None:
        blocked = blocked_paths(
            [
                "data/output/latest_signal.json",
                "data/output/latest_signal.manifest.json",
                "data/output/signal_history/2026-05-28.json",
                "data/raw/market_history.csv",
            ],
            task="long_horizon_signal_shadow",
        )
        self.assertEqual(blocked, ["data/raw/market_history.csv"])

    def test_git_diff_stats_reads_staged_additions_deletions_and_file_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            with patch("scripts.run_monthly_codex_audit.run_checked") as run:
                run.side_effect = [
                    "3\t1\tdocs/review.md\n2\t0\ttests/test_review.py\n-\t-\tdocs/chart.png\n",
                    "M\tdocs/review.md\nD\tdocs/old.md\nR100\tdocs/old_name.md\tdocs/new_name.md\nC100\tdocs/template.md\tdocs/copy.md\n",
                ]

                stats = git_diff_stats(repo_dir, cached=True)

        self.assertEqual(
            stats,
            {
                "additions": 5,
                "deletions": 1,
                "binary_files": 1,
                "deleted_files": 1,
                "renamed_files": 1,
                "copied_files": 1,
            },
        )
        run.assert_any_call(["git", "diff", "--numstat", "--cached"], cwd=repo_dir)
        run.assert_any_call(["git", "diff", "--name-status", "--cached"], cwd=repo_dir)

    def test_codex_audit_service_env_removes_service_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_AUDIT_SERVICE_STATIC_TOKEN": "service-token",
                "CODEX_AUDIT_SERVICE_OPENAI_API_KEY": "service-openai-key",
                "OPENAI_API_KEY": "openai-key",
                "PATH": "/usr/bin",
            },
            clear=True,
        ):
            env = _codex_env()

        self.assertNotIn("CODEX_AUDIT_SERVICE_STATIC_TOKEN", env)
        self.assertNotIn("CODEX_AUDIT_SERVICE_OPENAI_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_request_github_oidc_token_does_not_accept_static_token_fallback(self) -> None:
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_TOKEN": "legacy-token"}, clear=True):
            with self.assertRaisesRegex(BridgeError, "GitHub Actions OIDC token request environment is unavailable"):
                request_github_oidc_token("quant-codex-audit")

    def test_codex_audit_service_oidc_requires_repository_workflow_and_ref_allowlists(self) -> None:
        payload = {
            "aud": "quant-codex-audit",
            "iss": codex_audit_service.GITHUB_OIDC_ISSUER,
            "exp": int(time.time()) + 300,
            "repository": "QuantStrategyLab/AIAuditBridge",
            "workflow_ref": "QuantStrategyLab/AIAuditBridge/.github/workflows/codex_audit.yml@refs/heads/main",
            "ref": "refs/heads/main",
            "repository_visibility": "public",
        }
        env = {
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS": (
                "QuantStrategyLab/AIAuditBridge/.github/workflows/codex_audit.yml@refs/heads/main"
            ),
            "CODEX_AUDIT_SERVICE_ALLOWED_REFS": "refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES": "public",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                codex_audit_service,
                "_jwt_parts",
                return_value=({"alg": "RS256", "kid": "1"}, payload, b"x", b"y"),
            ),
            patch.object(codex_audit_service, "_load_jwks", return_value={"keys": [{"kid": "1", "kty": "RSA"}]}),
            patch.object(codex_audit_service, "_verify_rs256", return_value=None),
        ):
            claims = codex_audit_service._verify_github_oidc("header.payload.signature")

        self.assertEqual(claims["repository"], "QuantStrategyLab/AIAuditBridge")

    def test_codex_audit_service_oidc_rejects_missing_workflow_allowlist(self) -> None:
        payload = {
            "aud": "quant-codex-audit",
            "iss": codex_audit_service.GITHUB_OIDC_ISSUER,
            "exp": int(time.time()) + 300,
            "repository": "QuantStrategyLab/AIAuditBridge",
            "workflow_ref": "QuantStrategyLab/AIAuditBridge/.github/workflows/codex_audit.yml@refs/heads/main",
            "ref": "refs/heads/main",
        }
        env = {
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_REFS": "refs/heads/main",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                codex_audit_service,
                "_jwt_parts",
                return_value=({"alg": "RS256", "kid": "1"}, payload, b"x", b"y"),
            ),
            patch.object(codex_audit_service, "_load_jwks", return_value={"keys": [{"kid": "1", "kty": "RSA"}]}),
            patch.object(codex_audit_service, "_verify_rs256", return_value=None),
            self.assertRaisesRegex(PermissionError, "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS is required"),
        ):
            codex_audit_service._verify_github_oidc("header.payload.signature")

    def test_strip_audit_heading_removes_only_leading_heading(self) -> None:
        for heading in ("## Crypto Codex Audit", "## Codex Audit"):
            body = f"{heading}\n\n### Verdict\n\nOK"
            self.assertEqual(strip_audit_heading(body), "### Verdict\n\nOK")

    def test_convert_local_markdown_links_rewrites_repo_paths(self) -> None:
        repo_dir = Path("/tmp/codex-audit-bridge-abc/source")
        body = "See [script](/tmp/codex-audit-bridge-abc/source/scripts/run.py:42)."

        converted = convert_local_markdown_links(
            body,
            repo_dir,
            "QuantStrategyLab/CryptoLivePoolPipelines",
            "codex/monthly-audit-47",
        )

        self.assertEqual(
            converted,
            "See [script](https://github.com/QuantStrategyLab/CryptoLivePoolPipelines/blob/codex/monthly-audit-47/scripts/run.py#L42).",
        )

    def test_convert_local_markdown_links_leaves_external_paths(self) -> None:
        repo_dir = Path("/tmp/codex-audit-bridge-abc/source")
        body = "See [outside](/tmp/other/source/scripts/run.py:42)."

        self.assertEqual(
            convert_local_markdown_links(body, repo_dir, "QuantStrategyLab/CryptoLivePoolPipelines", "main"),
            body,
        )

    def test_resolve_source_repo_token_prefers_source_scoped_tokens(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_AUDIT_GH_TOKEN": "source-token",
                "GITHUB_TOKEN": "workflow-token",
                "GITHUB_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
            },
            clear=True,
        ):
            self.assertEqual(
                resolve_source_repo_token("QuantStrategyLab/UsEquitySnapshotPipelines"),
                "source-token",
            )

    def test_resolve_source_repo_token_allows_same_repo_github_token(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "workflow-token",
                "GITHUB_REPOSITORY": "QuantStrategyLab/UsEquitySnapshotPipelines",
            },
            clear=True,
        ):
            self.assertEqual(
                resolve_source_repo_token("QuantStrategyLab/UsEquitySnapshotPipelines"),
                "workflow-token",
            )

    def test_resolve_source_repo_token_rejects_cross_repo_github_token(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "workflow-token",
                "GITHUB_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
            },
            clear=True,
        ):
            with self.assertRaises(BridgeError):
                resolve_source_repo_token("QuantStrategyLab/UsEquitySnapshotPipelines")

    def test_extract_openai_text_reads_chat_completion_content(self) -> None:
        response = {"choices": [{"message": {"content": "review"}}]}
        self.assertEqual(extract_openai_text(response), "review")

    def test_extract_anthropic_text_reads_content_blocks(self) -> None:
        response = {"content": [{"type": "text", "text": "review"}, {"type": "text", "text": "second"}]}
        self.assertEqual(extract_anthropic_text(response), "review\n\nsecond")

    def test_build_api_review_prompt_includes_source_context(self) -> None:
        prompt = build_api_review_prompt(
            "QuantStrategyLab/CryptoLivePoolPipelines",
            "main",
            {"title": "Monthly Report", "body": "Body", "html_url": "https://example.test/issue"},
            [],
        )
        self.assertIn("QuantStrategyLab/CryptoLivePoolPipelines", prompt)
        self.assertIn("Monthly Report", prompt)
        self.assertIn("API Monthly Review", prompt)

    def test_build_api_review_prompt_includes_latest_issue_comments(self) -> None:
        comments = [
            {"user": {"login": f"user-{index}"}, "body": f"comment-{index}"}
            for index in range(25)
        ]

        prompt = build_api_review_prompt(
            "QuantStrategyLab/UsEquitySnapshotPipelines",
            "main",
            {"title": "Monthly Report", "body": "Body", "html_url": "https://example.test/issue"},
            comments,
        )

        self.assertNotIn("comment-0", prompt)
        self.assertNotIn("comment-4", prompt)
        self.assertIn("comment-5", prompt)
        self.assertIn("comment-24", prompt)

    def test_build_api_review_prompt_includes_hk_snapshot_gates(self) -> None:
        prompt = build_api_review_prompt(
            "QuantStrategyLab/HkEquitySnapshotPipelines",
            "main",
            {"title": "HK Monthly Snapshot Report", "body": "Body", "html_url": "https://example.test/issue"},
            [],
        )

        self.assertIn("QuantStrategyLab/HkEquitySnapshotPipelines", prompt)
        self.assertIn("hk_low_vol_dividend_quality", prompt)
        self.assertIn("hk_shareholder_yield_quality", prompt)
        self.assertIn("hk_free_cash_flow_quality", prompt)
        self.assertIn("max drawdown <= 30%", prompt)
        self.assertIn("bilingual notification evidence", prompt)

    def test_build_api_review_prompt_supports_long_horizon_task(self) -> None:
        prompt = build_api_review_prompt(
            "QuantStrategyLab/ResearchSignalContextPipelines",
            "main",
            {"title": "Shadow Signal", "body": "Body", "html_url": "https://example.test/issue"},
            [],
            task="long_horizon_signal_shadow",
        )

        self.assertIn("QuantStrategyLab/ResearchSignalContextPipelines", prompt)
        self.assertIn("API Long-Horizon Shadow Signal Review", prompt)
        self.assertIn("Draft Shadow Signal JSON", prompt)

    def test_build_service_prompt_adds_filtered_context_and_patch_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / ".codex-audit").mkdir()
            (repo_dir / ".codex-audit" / "monthly_issue.md").write_text("Issue body", encoding="utf-8")
            (repo_dir / "scripts").mkdir()
            (repo_dir / "scripts" / "check.py").write_text("print('ok')\n", encoding="utf-8")
            (repo_dir / "docs").mkdir()
            (repo_dir / "docs" / "api-token.md").write_text("should not be included\n", encoding="utf-8")

            prompt = build_service_prompt(repo_dir, "Base prompt", task="monthly_snapshot_audit", mode="review_and_fix")

        self.assertIn("Base prompt", prompt)
        self.assertIn('context path=".codex-audit/monthly_issue.md"', prompt)
        self.assertIn('context path="scripts/check.py"', prompt)
        self.assertIn("Service patch contract", prompt)
        self.assertNotIn("api-token.md", prompt)
        self.assertNotIn("should not be included", prompt)

    def test_parse_service_patch_response_accepts_fenced_json(self) -> None:
        final_message, changes = parse_service_patch_response(
            """```json
{"final_message": "Reviewed.", "changes": [{"path": "README.md", "content": "# Title\\n"}]}
```"""
        )

        self.assertEqual(final_message, "Reviewed.")
        self.assertEqual(changes, [{"path": "README.md", "content": "# Title\n"}])

    def test_apply_service_changes_writes_allowed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            changed = apply_service_changes(
                repo_dir,
                [{"path": "docs/review.md", "content": "review\n"}],
                task="monthly_snapshot_audit",
            )

            self.assertEqual(changed, ["docs/review.md"])
            self.assertEqual((repo_dir / "docs" / "review.md").read_text(encoding="utf-8"), "review\n")

    def test_apply_service_changes_rejects_blocked_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BridgeError):
                apply_service_changes(
                    Path(tmp),
                    [{"path": "docs/api-token.md", "content": "secret\n"}],
                    task="monthly_snapshot_audit",
                )

    def test_normalize_codex_service_url_requires_https_except_local_testing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                normalize_codex_service_url("https://codex.quant.example"),
                "https://codex.quant.example/v1/codex-audit",
            )
            self.assertEqual(
                normalize_codex_service_url("https://codex.quant.example/api"),
                "https://codex.quant.example/api/v1/codex-audit",
            )
            with self.assertRaises(BridgeError):
                normalize_codex_service_url("http://codex.quant.example")
        with patch.dict(os.environ, {"CODEX_AUDIT_ALLOW_INSECURE_SERVICE_URL": "true"}):
            self.assertEqual(
                normalize_codex_service_url("http://127.0.0.1:8797"),
                "http://127.0.0.1:8797/v1/codex-audit",
            )

    def test_codex_service_async_job_urls_are_derived_from_audit_endpoint(self) -> None:
        service_url = "https://codex.quant.example/v1/codex-audit"
        job_id = "abcdefghijklmnopqrstuvwxyzABCD12"

        self.assertEqual(codex_service_jobs_url(service_url), "https://codex.quant.example/v1/codex-audit/jobs")
        self.assertEqual(
            codex_service_job_url(service_url, job_id),
            f"https://codex.quant.example/v1/codex-audit/jobs/{job_id}",
        )
        with self.assertRaises(BridgeError):
            codex_service_job_url(service_url, "short")

    def test_request_codex_service_includes_router_model(self) -> None:
        responses = [
            {"status": "queued", "job_id": "job-1abcdefghijklmno123456"},
            {"status": "succeeded", "output": "done"},
        ]
        with (
            patch.dict(
                os.environ,
                {"CODEX_AUDIT_SERVICE_URL": "https://service.example", "CODEX_AUDIT_SERVICE_AUDIENCE": "aud"},
                clear=True,
            ),
            patch("scripts.run_monthly_codex_audit.request_github_oidc_token", return_value="oidc-token"),
            patch("scripts.run_monthly_codex_audit.request_codex_service_json", side_effect=responses) as request,
            patch("scripts.run_monthly_codex_audit.route_model", return_value={"model": "gpt-5.6-sol"}),
            patch("scripts.run_monthly_codex_audit.time.sleep"),
        ):
            output = request_codex_service(
                source_repo="QuantStrategyLab/CryptoLivePoolPipelines",
                source_ref="main",
                task="monthly_snapshot_audit",
                mode="review_only",
                prompt="prompt",
                timeout_minutes=1,
            )

        self.assertEqual(output, "done")
        self.assertEqual(request.call_args_list[0].kwargs["payload"]["model"], "gpt-5.6-sol")

    def test_service_failure_classification_identifies_infra_failures(self) -> None:
        self.assertEqual(classify_service_failure("OIDC token is expired"), "auth_or_config_failure")
        self.assertEqual(service_failure_category("Codex audit service job failed [quota_or_capacity_failure]: budget"), "quota_or_capacity_failure")
        self.assertFalse(is_service_infrastructure_failure("Codex audit service job failed [quota_or_capacity_failure]: budget"))
        self.assertTrue(is_service_infrastructure_failure("Codex audit service job failed [transient_service_failure]: timed out"))
        self.assertFalse(is_service_infrastructure_failure("Codex audit service job failed [patch_contract_failure]: invalid JSON"))

    def test_service_failure_classification_ignores_source_code_secret_words(self) -> None:
        message = "codex exec failed: BLOCKED_PATH_RE = r'.*token.*|.*secret.*'"
        self.assertEqual(classify_service_failure(message), "unknown_failure")
        allowlist_message = "codex exec failed: raise ValueError('not allowed by source allowlist')"
        self.assertEqual(classify_service_failure(allowlist_message), "unknown_failure")
        self.assertEqual(codex_audit_service._classify_failure(allowlist_message), "unknown_failure")
        forbidden_message = "codex exec failed: fixture text includes 403 forbidden"
        self.assertEqual(classify_service_failure(forbidden_message), "unknown_failure")
        self.assertEqual(codex_audit_service._classify_failure(forbidden_message), "unknown_failure")
        self.assertEqual(classify_service_failure("codex exec failed: too many active requests"), "quota_or_capacity_failure")
        self.assertEqual(
            classify_service_failure("source_repository foo is not allowed by service allowlist"),
            "auth_or_config_failure",
        )

    def test_codex_audit_service_async_job_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES": "QuantStrategyLab/CryptoLivePoolPipelines",
                "CODEX_AUDIT_SERVICE_FAKE_OUTPUT": "async review output",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), CodexAuditServiceRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "source_repository": "QuantStrategyLab/CryptoLivePoolPipelines",
                        "source_ref": "main",
                        "task": "monthly_snapshot_audit",
                        "mode": "review_only",
                        "prompt": "Review this snapshot.",
                        "timeout_seconds": 60,
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/codex-audit/jobs",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 202)
                        submitted = json.loads(response.read().decode("utf-8"))
                    job_id = submitted["job_id"]

                    polled: dict[str, object] = {}
                    for _ in range(50):
                        try:
                            with urllib.request.urlopen(f"{base_url}/v1/codex-audit/jobs/{job_id}", timeout=5) as response:
                                polled = json.loads(response.read().decode("utf-8"))
                        except urllib.error.HTTPError as exc:
                            if exc.code != 404:
                                raise
                            time.sleep(0.05)
                            continue
                        if polled["status"] == "succeeded":
                            break
                        time.sleep(0.05)
                    self.assertEqual(polled["status"], "succeeded")
                    self.assertEqual(polled["output"], "async review output")
                    self.assertNotIn("prompt", polled)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_codex_audit_service_background_job_marks_clone_failure_failed_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token = "test-token-value"
            captured: dict[str, object] = {}
            payload = {
                "source_repository": "QuantStrategyLab/CryptoLivePoolPipelines",
                "source_ref": "main",
                "task": "monthly_snapshot_audit",
                "mode": "review_only",
                "prompt": "Review this snapshot.",
                "timeout_seconds": 60,
            }

            def fail_clone(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                captured["command"] = command
                captured["env"] = kwargs.get("env")
                raise subprocess.CalledProcessError(
                    128,
                    command,
                    stderr=f"fatal: authentication failed for {token}",
                )

            env = {
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CROSS_REPO_GIT_TOKEN": token,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(codex_audit_service.subprocess, "run", side_effect=fail_clone),
            ):
                job_id = "a" * 24
                codex_audit_service._write_job(job_id, payload, "dedupe-key")
                codex_audit_service._run_job_background(job_id, payload)
                job = codex_audit_service._read_job(job_id)

            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "failed")
            self.assertIn("git clone failed", job["error"])
            self.assertNotIn(token, job["error"])
            self.assertIn("[REDACTED]", job["error"])
            command = captured.get("command")
            self.assertIsInstance(command, list)
            command_text = " ".join(command)
            self.assertNotIn(token, command_text)
            self.assertIn("https://github.com/QuantStrategyLab/CryptoLivePoolPipelines.git", command_text)
            env_value = captured["env"]
            self.assertIsInstance(env_value, dict)
            self.assertEqual(env_value.get("GIT_CONFIG_KEY_0"), "http.https://github.com/.extraheader")

    def test_codex_audit_service_deduplicates_active_audit_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES": "QuantStrategyLab/CryptoLivePoolPipelines",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(codex_audit_service, "_run_job_background", side_effect=lambda *_args: time.sleep(0.2)),
            ):
                server = ThreadingHTTPServer(("127.0.0.1", 0), CodexAuditServiceRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "source_repository": "QuantStrategyLab/CryptoLivePoolPipelines",
                        "source_ref": "main",
                        "issue_number": 42,
                        "task": "monthly_snapshot_audit",
                        "mode": "review_only",
                        "prompt": "Review this snapshot.",
                        "timeout_seconds": 60,
                    }

                    def submit() -> dict[str, object]:
                        request = urllib.request.Request(
                            f"{base_url}/v1/codex-audit/jobs",
                            data=json.dumps(payload).encode("utf-8"),
                            method="POST",
                            headers={"Content-Type": "application/json"},
                        )
                        with urllib.request.urlopen(request, timeout=5) as response:
                            self.assertEqual(response.status, 202)
                            return json.loads(response.read().decode("utf-8"))

                    first = submit()
                    second = submit()

                    self.assertEqual(second["job_id"], first["job_id"])
                    self.assertTrue(second["deduped"])
                finally:
                    server.shutdown()
                    server.server_close()

    def test_auto_fallback_missing_api_key_message_mentions_reason(self) -> None:
        message = auto_fallback_missing_api_key_message("Codex setup failed.")
        self.assertIn("Codex setup failed.", message)
        self.assertIn("OPENAI_API_KEY", message)
        self.assertIn("ANTHROPIC_API_KEY", message)
        self.assertIn("No files were pushed", message)
        self.assertNotIn("provider `auto`", message)

    def test_api_fallback_allow_fix_defaults_true(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(api_fallback_allow_fix())
        with patch.dict(os.environ, {"CODEX_AUDIT_API_FALLBACK_ALLOW_FIX": "false"}, clear=True):
            self.assertFalse(api_fallback_allow_fix())

    def test_run_api_patch_provider_applies_allowed_changes(self) -> None:
        patch_json = json.dumps(
            {
                "final_message": "Updated shadow signal.",
                "changes": [
                    {
                        "path": "data/output/latest_signal.json",
                        "content": '{"mode":"shadow"}\n',
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / ".codex-audit").mkdir()
            (repo_dir / ".codex-audit" / "monthly_issue.md").write_text("Issue body", encoding="utf-8")
            with patch(
                "scripts.run_monthly_codex_audit.request_openai_completion",
                return_value=patch_json,
            ):
                return_code, _, final_message = run_api_patch_provider(
                    repo_dir,
                    "Base prompt",
                    task="long_horizon_signal_shadow",
                    mode="review_and_fix",
                    provider="openai",
                )
            self.assertEqual(return_code, 0)
            self.assertEqual(final_message, "Updated shadow signal.")
            self.assertTrue((repo_dir / "data/output/latest_signal.json").exists())

    def test_run_auto_provider_fallback_review_and_fix_delegates_to_patch_remediation(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES": (
                    "QuantStrategyLab/ResearchSignalContextPipelines"
                ),
                "CODEX_AUDIT_API_FALLBACK_ALLOW_FIX": "true",
            },
            clear=True,
        ):
            with patch("scripts.run_monthly_codex_audit.run_api_patch_remediation", return_value=0) as patch_remediation:
                exit_code = run_auto_provider_fallback(
                    token="token",
                    source_repo="QuantStrategyLab/ResearchSignalContextPipelines",
                    source_ref="main",
                    issue={"number": 19, "title": "Shadow", "body": "Body"},
                    comments=[],
                    issue_number=19,
                    reason="Codex service failed.",
                    mode="review_and_fix",
                    task="long_horizon_signal_shadow",
                )
        self.assertEqual(exit_code, 0)
        patch_remediation.assert_called_once()

    def test_main_codex_failure_uses_api_patch_remediation_for_auto_provider(self) -> None:
        issue = {
            "number": 19,
            "title": "Shadow",
            "html_url": "https://example.test/issues/19",
            "body": "Body",
            "labels": [],
        }
        env = {
            "SOURCE_REPO": "QuantStrategyLab/ResearchSignalContextPipelines",
            "SOURCE_REF": "main",
            "ISSUE_NUMBER": "19",
            "CODEX_AUDIT_GH_TOKEN": "token",
            "CODEX_AUDIT_TASK": "long_horizon_signal_shadow",
            "CODEX_AUDIT_MODE": "review_and_fix",
            "CODEX_AUDIT_PROVIDER": "auto",
            "CODEX_AUDIT_CODEX_BACKEND": "service",
            "CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES": (
                "QuantStrategyLab/ResearchSignalContextPipelines"
            ),
            "CODEX_AUDIT_API_FALLBACK_ALLOW_FIX": "true",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("scripts.run_monthly_codex_audit.github_request", return_value=issue),
            patch("scripts.run_monthly_codex_audit.fetch_issue_comments", return_value=[]),
            patch("scripts.run_monthly_codex_audit.prepare_remediation_workspace") as prepare,
            patch("scripts.run_monthly_codex_audit.run_codex_backend", return_value=(1, "service down", "")),
            patch("scripts.run_monthly_codex_audit.run_api_patch_remediation", return_value=0) as patch_remediation,
        ):
            workspace = RemediationWorkspace(
                repo_dir=Path("/tmp/source"),
                branch_name="codex/long-horizon-signal-issue-19-test",
                baseline_auto_merge_policy=dict(DEFAULT_GUARDED_AUTO_MERGE_POLICY),
                feedback_retry_pr=None,
                stale_auto_merge_label=GUARDED_AUTO_MERGE_LABEL,
                stale_auto_merge_label_skip_reason="",
                stale_auto_merge_label_removed=False,
                prompt="prompt",
            )
            prepare.return_value = workspace
            exit_code = run_audit_main()

        self.assertEqual(exit_code, 0)
        patch_remediation.assert_called_once()
        self.assertIs(patch_remediation.call_args.kwargs["workspace"], workspace)

    def test_main_defaults_long_horizon_task_to_codex_provider(self) -> None:
        issue = {
            "number": 19,
            "title": "Shadow",
            "html_url": "https://example.test/issues/19",
            "body": "Body",
            "labels": [],
        }
        env = {
            "SOURCE_REPO": "QuantStrategyLab/ResearchSignalContextPipelines",
            "SOURCE_REF": "main",
            "ISSUE_NUMBER": "19",
            "CODEX_AUDIT_GH_TOKEN": "token",
            "CODEX_AUDIT_TASK": "long_horizon_signal_shadow",
            "CODEX_AUDIT_MODE": "review_and_fix",
            "CODEX_AUDIT_CODEX_BACKEND": "service",
            "CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES": (
                "QuantStrategyLab/ResearchSignalContextPipelines"
            ),
            "CODEX_AUDIT_API_FALLBACK_ALLOW_FIX": "true",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("scripts.run_monthly_codex_audit.github_request", return_value=issue),
            patch("scripts.run_monthly_codex_audit.fetch_issue_comments", return_value=[]),
            patch("scripts.run_monthly_codex_audit.prepare_remediation_workspace") as prepare,
            patch(
                "scripts.run_monthly_codex_audit.run_codex_backend",
                return_value=(1, "Codex audit service job failed [quota_or_capacity_failure]: budget", ""),
            ),
            patch("scripts.run_monthly_codex_audit.run_auto_provider_fallback") as patch_fallback,
            patch("scripts.run_monthly_codex_audit.post_issue_comment") as post_comment,
        ):
            workspace = RemediationWorkspace(
                repo_dir=Path("/tmp/source"),
                branch_name="codex/long-horizon-signal-issue-19-test",
                baseline_auto_merge_policy=dict(DEFAULT_GUARDED_AUTO_MERGE_POLICY),
                feedback_retry_pr=None,
                stale_auto_merge_label=GUARDED_AUTO_MERGE_LABEL,
                stale_auto_merge_label_skip_reason="",
                stale_auto_merge_label_removed=False,
                prompt="prompt",
            )
            prepare.return_value = workspace
            exit_code = run_audit_main()

        self.assertEqual(exit_code, 1)
        patch_fallback.assert_not_called()
        post_comment.assert_called_once()

    def test_main_codex_quota_failure_uses_api_fallback_for_auto_provider(self) -> None:
        issue = {
            "number": 19,
            "title": "Shadow",
            "html_url": "https://example.test/issues/19",
            "body": "Body",
            "labels": [],
        }
        env = {
            "SOURCE_REPO": "QuantStrategyLab/ResearchSignalContextPipelines",
            "SOURCE_REF": "main",
            "ISSUE_NUMBER": "19",
            "CODEX_AUDIT_GH_TOKEN": "token",
            "CODEX_AUDIT_TASK": "long_horizon_signal_shadow",
            "CODEX_AUDIT_MODE": "review_and_fix",
            "CODEX_AUDIT_PROVIDER": "auto",
            "CODEX_AUDIT_CODEX_BACKEND": "service",
            "CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES": (
                "QuantStrategyLab/ResearchSignalContextPipelines"
            ),
            "CODEX_AUDIT_API_FALLBACK_ALLOW_FIX": "true",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("scripts.run_monthly_codex_audit.github_request", return_value=issue),
            patch("scripts.run_monthly_codex_audit.fetch_issue_comments", return_value=[]),
            patch("scripts.run_monthly_codex_audit.prepare_remediation_workspace") as prepare,
            patch(
                "scripts.run_monthly_codex_audit.run_codex_backend",
                return_value=(1, "Codex audit service job failed [quota_or_capacity_failure]: budget", ""),
            ),
            patch("scripts.run_monthly_codex_audit.run_auto_provider_fallback", return_value=0) as patch_fallback,
        ):
            workspace = RemediationWorkspace(
                repo_dir=Path("/tmp/source"),
                branch_name="codex/long-horizon-signal-issue-19-test",
                baseline_auto_merge_policy=dict(DEFAULT_GUARDED_AUTO_MERGE_POLICY),
                feedback_retry_pr=None,
                stale_auto_merge_label=GUARDED_AUTO_MERGE_LABEL,
                stale_auto_merge_label_skip_reason="",
                stale_auto_merge_label_removed=False,
                prompt="prompt",
            )
            prepare.return_value = workspace
            exit_code = run_audit_main()

        self.assertEqual(exit_code, 0)
        patch_fallback.assert_called_once()
        self.assertIs(patch_fallback.call_args.kwargs["workspace"], workspace)
        self.assertIn("quota_or_capacity_failure", patch_fallback.call_args.kwargs["reason"])

    def test_main_codex_infra_failure_stops_without_api_fallback_for_auto_provider(self) -> None:
        issue = {
            "number": 19,
            "title": "Shadow",
            "html_url": "https://example.test/issues/19",
            "body": "Body",
            "labels": [],
        }
        env = {
            "SOURCE_REPO": "QuantStrategyLab/ResearchSignalContextPipelines",
            "SOURCE_REF": "main",
            "ISSUE_NUMBER": "19",
            "CODEX_AUDIT_GH_TOKEN": "token",
            "CODEX_AUDIT_TASK": "long_horizon_signal_shadow",
            "CODEX_AUDIT_MODE": "review_and_fix",
            "CODEX_AUDIT_PROVIDER": "auto",
            "CODEX_AUDIT_CODEX_BACKEND": "service",
            "CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES": (
                "QuantStrategyLab/ResearchSignalContextPipelines"
            ),
            "CODEX_AUDIT_API_FALLBACK_ALLOW_FIX": "true",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("scripts.run_monthly_codex_audit.github_request", return_value=issue),
            patch("scripts.run_monthly_codex_audit.fetch_issue_comments", return_value=[]),
            patch("scripts.run_monthly_codex_audit.prepare_remediation_workspace") as prepare,
            patch(
                "scripts.run_monthly_codex_audit.run_codex_backend",
                return_value=(75, "Codex audit service job failed [transient_service_failure]: timed out", ""),
            ),
            patch("scripts.run_monthly_codex_audit.post_issue_comment") as post_comment,
            patch("scripts.run_monthly_codex_audit.run_auto_provider_fallback") as patch_fallback,
        ):
            workspace = RemediationWorkspace(
                repo_dir=Path("/tmp/source"),
                branch_name="codex/long-horizon-signal-issue-19-test",
                baseline_auto_merge_policy=dict(DEFAULT_GUARDED_AUTO_MERGE_POLICY),
                feedback_retry_pr=None,
                stale_auto_merge_label=GUARDED_AUTO_MERGE_LABEL,
                stale_auto_merge_label_skip_reason="",
                stale_auto_merge_label_removed=False,
                prompt="prompt",
            )
            prepare.return_value = workspace
            exit_code = run_audit_main()

        self.assertEqual(exit_code, 0)
        post_comment.assert_called_once()
        self.assertIn("Failure category", post_comment.call_args.args[3])
        patch_fallback.assert_not_called()

    def test_run_configured_api_reviews_uses_both_configured_reviewers(self) -> None:
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "openai-key", "ANTHROPIC_API_KEY": "anthropic-key"}, clear=True),
            patch("scripts.run_monthly_codex_audit.run_openai_review", return_value="openai review"),
            patch("scripts.run_monthly_codex_audit.run_anthropic_review", return_value="anthropic review"),
        ):
            reviews, warnings = run_configured_api_reviews(
                "QuantStrategyLab/CryptoLivePoolPipelines",
                "main",
                {"title": "Monthly Report", "body": "Body"},
                [],
            )

        self.assertEqual(reviews, [("OpenAI", "openai review"), ("Anthropic Claude", "anthropic review")])
        self.assertEqual(warnings, [])

    def test_run_configured_api_reviews_reports_missing_optional_reviewer(self) -> None:
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "openai-key"}, clear=True),
            patch("scripts.run_monthly_codex_audit.run_openai_review", return_value="openai review"),
        ):
            reviews, warnings = run_configured_api_reviews(
                "QuantStrategyLab/CryptoLivePoolPipelines",
                "main",
                {"title": "Monthly Report", "body": "Body"},
                [],
            )

        self.assertEqual(reviews, [("OpenAI", "openai review")])
        self.assertEqual(warnings, ["Anthropic Claude fallback skipped because `ANTHROPIC_API_KEY` is not configured."])

    def test_run_configured_api_reviews_sanitizes_failed_reviewer_errors(self) -> None:
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "openai-key", "ANTHROPIC_API_KEY": "anthropic-key"}, clear=True),
            patch(
                "scripts.run_monthly_codex_audit.run_openai_review",
                side_effect=BridgeError("Incorrect API key provided: sk-proj-secretTail"),
            ),
            patch(
                "scripts.run_monthly_codex_audit.run_anthropic_review",
                side_effect=BridgeError("Anthropic API request failed: 401 invalid x-api-key"),
            ),
        ):
            reviews, warnings = run_configured_api_reviews(
                "QuantStrategyLab/CryptoLivePoolPipelines",
                "main",
                {"title": "Monthly Report", "body": "Body"},
                [],
            )

        warning_text = "\n".join(warnings)
        self.assertEqual(reviews, [])
        self.assertIn("OpenAI fallback failed", warning_text)
        self.assertIn("Anthropic Claude fallback failed", warning_text)
        self.assertNotIn("sk-proj", warning_text)
        self.assertNotIn("secretTail", warning_text)

    def test_format_api_review_comment_combines_reviews(self) -> None:
        message = format_api_review_comment(
            "Codex failed.",
            [("OpenAI", "openai review"), ("Anthropic Claude", "anthropic review")],
            ["Anthropic warning"],
        )
        self.assertIn("## API Monthly Review", message)
        self.assertIn("### OpenAI Review", message)
        self.assertIn("### Anthropic Claude Review", message)
        self.assertIn("Anthropic warning", message)

    def test_pr_closing_line_only_closes_long_horizon_signal_issues(self) -> None:
        self.assertEqual(pr_closing_line("long_horizon_signal_shadow", 4), "Closes #4")
        self.assertEqual(pr_closing_line("monthly_snapshot_audit", 4), "")

    def test_write_codex_context_includes_latest_issue_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / ".git" / "info").mkdir(parents=True)
            comments = [
                {"user": {"login": f"user-{index}"}, "body": f"comment-{index}"}
                for index in range(25)
            ]

            issue_path, _context_path = write_codex_context(
                repo_dir,
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                "main",
                {"number": 7, "title": "Monthly Review", "html_url": "https://example.test/issue/7"},
                comments,
            )

            issue_markdown = issue_path.read_text(encoding="utf-8")
            self.assertNotIn("comment-0", issue_markdown)
            self.assertNotIn("comment-4", issue_markdown)
            self.assertIn("comment-5", issue_markdown)
            self.assertIn("comment-24", issue_markdown)

    def test_latest_feedback_pr_number_uses_newest_ci_or_review_marker(self) -> None:
        comments = [
            {"body": "<!-- codex-pr-feedback:ci:12 -->\nold"},
            {"body": "operator note"},
            {"body": "<!-- codex-pr-feedback:review:15 -->\nnew"},
        ]

        self.assertEqual(latest_feedback_pr_number(comments), 15)

    def test_fetch_issue_comments_reads_next_page_for_latest_feedback_marker(self) -> None:
        first_page = [{"body": f"comment-{index}"} for index in range(100)]
        second_page = [
            {"body": "operator note"},
            {"body": "<!-- codex-pr-feedback:ci:42 -->\nretry latest failed CI"},
        ]

        with patch(
            "scripts.run_monthly_codex_audit.github_request",
            side_effect=[first_page, second_page],
        ) as request:
            comments = fetch_issue_comments(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                7,
            )

        self.assertEqual(len(comments), 102)
        self.assertEqual(latest_feedback_pr_number(comments), 42)
        request.assert_has_calls(
            [
                call(
                    "token",
                    "GET",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/7/comments?per_page=100&page=1",
                ),
                call(
                    "token",
                    "GET",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/7/comments?per_page=100&page=2",
                ),
            ]
        )

    def test_fetch_issue_comments_stops_on_non_list_response(self) -> None:
        with patch("scripts.run_monthly_codex_audit.github_request", return_value={"message": "bad"}) as request:
            comments = fetch_issue_comments(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                7,
            )

        self.assertEqual(comments, [])
        request.assert_called_once_with(
            "token",
            "GET",
            "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/7/comments?per_page=100&page=1",
        )

    def test_resolve_feedback_retry_pr_accepts_same_repo_open_codex_pr(self) -> None:
        comments = [{"body": "<!-- codex-pr-feedback:ci:12 -->\nretry"}]
        pr_payload = {
            "number": 12,
            "state": "open",
            "html_url": "https://github.com/QuantStrategyLab/UsEquitySnapshotPipelines/pull/12",
            "body": "<!-- codex-monthly-remediation:issue-7 -->\nfix",
            "head": {
                "ref": "codex/monthly-review-issue-7-20260620",
                "repo": {"full_name": "QuantStrategyLab/UsEquitySnapshotPipelines"},
            },
            "base": {"ref": "main"},
        }
        with patch("scripts.run_monthly_codex_audit.github_request", return_value=pr_payload) as request:
            retry_pr = resolve_feedback_retry_pr(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                7,
                "main",
                comments,
            )

        request.assert_called_once_with("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/pulls/12")
        self.assertEqual(
            retry_pr,
            {
                "number": 12,
                "html_url": "https://github.com/QuantStrategyLab/UsEquitySnapshotPipelines/pull/12",
                "head_ref": "codex/monthly-review-issue-7-20260620",
                "base_ref": "main",
            },
        )

    def test_resolve_feedback_retry_pr_rejects_cross_repo_or_wrong_issue_pr(self) -> None:
        comments = [{"body": "<!-- codex-pr-feedback:review:12 -->\nretry"}]
        cross_repo_payload = {
            "number": 12,
            "state": "open",
            "head": {
                "ref": "codex/monthly-review-issue-7-20260620",
                "repo": {"full_name": "someone/fork"},
            },
            "base": {"ref": "main"},
        }
        wrong_issue_payload = {
            "number": 12,
            "state": "open",
            "body": "<!-- codex-monthly-remediation:issue-7 -->\nfix",
            "head": {
                "ref": "codex/monthly-review-issue-8-20260620",
                "repo": {"full_name": "QuantStrategyLab/UsEquitySnapshotPipelines"},
            },
            "base": {"ref": "main"},
        }

        with patch("scripts.run_monthly_codex_audit.github_request", return_value=cross_repo_payload):
            self.assertIsNone(
                resolve_feedback_retry_pr(
                    "token",
                    "QuantStrategyLab/UsEquitySnapshotPipelines",
                    7,
                    "main",
                    comments,
                )
            )
        with patch("scripts.run_monthly_codex_audit.github_request", return_value=wrong_issue_payload):
            self.assertIsNone(
                resolve_feedback_retry_pr(
                    "token",
                    "QuantStrategyLab/UsEquitySnapshotPipelines",
                    7,
                    "main",
                    comments,
                )
            )

    def test_resolve_feedback_retry_pr_rejects_numeric_prefix_collision(self) -> None:
        comments = [{"body": "<!-- codex-pr-feedback:ci:12 -->\nretry"}]
        pr_payload = {
            "number": 12,
            "state": "open",
            "body": "<!-- codex-monthly-remediation:issue-7 -->\nfix",
            "head": {
                "ref": "codex/monthly-review-issue-77-20260620",
                "repo": {"full_name": "QuantStrategyLab/UsEquitySnapshotPipelines"},
            },
            "base": {"ref": "main"},
        }

        with patch("scripts.run_monthly_codex_audit.github_request", return_value=pr_payload):
            self.assertIsNone(
                resolve_feedback_retry_pr(
                    "token",
                    "QuantStrategyLab/UsEquitySnapshotPipelines",
                    7,
                    "main",
                    comments,
                )
            )

    def test_resolve_feedback_retry_pr_rejects_missing_pr_body_marker(self) -> None:
        comments = [{"body": "<!-- codex-pr-feedback:ci:12 -->\nretry"}]
        pr_payload = {
            "number": 12,
            "state": "open",
            "body": "marker removed",
            "head": {
                "ref": "codex/monthly-review-issue-7-20260620",
                "repo": {"full_name": "QuantStrategyLab/UsEquitySnapshotPipelines"},
            },
            "base": {"ref": "main"},
        }

        with patch("scripts.run_monthly_codex_audit.github_request", return_value=pr_payload):
            self.assertIsNone(
                resolve_feedback_retry_pr(
                    "token",
                    "QuantStrategyLab/UsEquitySnapshotPipelines",
                    7,
                    "main",
                    comments,
                )
            )

    def test_classify_guarded_auto_merge_risk_allows_low_and_medium_surfaces(self) -> None:
        low = classify_guarded_auto_merge_risk(["./README.zh-CN.md", "docs/runbook.md", "tests/test_report.py"])
        medium = classify_guarded_auto_merge_risk(
            [
                "scripts/build_monthly_live_strategy_health_reports.py",
                "scripts/run_monthly_report_bundle.py",
                "scripts/plan_codex_auto_merge_enablement.py",
            ]
        )
        control_plane = classify_guarded_auto_merge_risk(
            [
                ".github/workflows/auto_merge_codex_pr.yml",
                "scripts/evaluate_codex_pr_merge.py",
                "scripts/check_codex_auto_merge_readiness.py",
                "scripts/post_codex_auto_merge_decision_comment.py",
                "scripts/sync_codex_auto_merge_labels.py",
            ]
        )

        self.assertTrue(low["label_allowed"])
        self.assertEqual(low["risk_level"], "low")
        self.assertEqual(low["high_risk_files"], [])
        self.assertTrue(medium["label_allowed"])
        self.assertEqual(medium["risk_level"], "medium")
        self.assertEqual(
            medium["medium_risk_files"],
            [
                "scripts/build_monthly_live_strategy_health_reports.py",
                "scripts/run_monthly_report_bundle.py",
                "scripts/plan_codex_auto_merge_enablement.py",
            ],
        )
        self.assertFalse(control_plane["label_allowed"])
        self.assertEqual(control_plane["risk_level"], "high")
        self.assertEqual(
            control_plane["high_risk_files"],
            [
                ".github/workflows/auto_merge_codex_pr.yml",
                "scripts/evaluate_codex_pr_merge.py",
                "scripts/check_codex_auto_merge_readiness.py",
                "scripts/post_codex_auto_merge_decision_comment.py",
                "scripts/sync_codex_auto_merge_labels.py",
            ],
        )

    def test_guarded_auto_merge_default_high_reason_matches_source_policy(self) -> None:
        self.assertEqual(
            DEFAULT_GUARDED_AUTO_MERGE_POLICY["risk_policy"]["high"]["reason"],
            "blocked/high-risk files require human review",
        )

    def test_guarded_auto_merge_default_policy_matches_local_source_policy_when_available(self) -> None:
        source_policy_path = (
            Path(__file__).resolve().parents[2]
            / "UsEquitySnapshotPipelines"
            / ".github"
            / "codex_auto_merge_policy.json"
        )
        if not source_policy_path.exists():
            self.skipTest("local UsEquitySnapshotPipelines checkout is not available")

        source_policy = json.loads(source_policy_path.read_text(encoding="utf-8"))

        # Verify both policies are structurally valid (not identical — source repos
        # may add medium-risk files independently of the bridge's default fallback)
        for policy, label in ((DEFAULT_GUARDED_AUTO_MERGE_POLICY, "default"),
                               (source_policy, "source")):
            self.assertIn("version", policy, f"{label} policy missing version")
            self.assertIn("risk_policy", policy, f"{label} policy missing risk_policy")
            self.assertIn("medium", policy["risk_policy"], f"{label} policy missing medium risk tier")
            self.assertIn("exact", policy["risk_policy"]["medium"],
                          f"{label} policy medium tier missing exact list")

    def test_classify_guarded_auto_merge_risk_can_use_source_policy_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / ".github" / "codex_auto_merge_policy.json"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "auto_merge_label": "custom-auto-ok",
                        "human_review_label": "custom-review-required",
                        "monthly_marker_prefix": "<!-- custom-remediation:issue-",
                        "max_changed_files": 20,
                        "max_changed_lines": 1200,
                        "blocked_path_patterns": [
                            "(^|/)(\\.env|.*secret.*|.*credential.*|.*token.*|.*private.*|.*\\.pem|.*\\.key)$"
                        ],
                        "risk_policy": {
                            "low": {
                                "prefixes": ["docs/"],
                                "exact": ["README.md"],
                                "reason": "custom low",
                            },
                            "medium": {
                                "exact": ["scripts/custom_monthly_fix.py"],
                                "reason": "custom medium",
                            },
                            "high": {"reason": "custom high"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            policy = load_guarded_auto_merge_policy(policy_path)

        risk = classify_guarded_auto_merge_risk(["scripts/custom_monthly_fix.py"], policy=policy)
        blocked = classify_guarded_auto_merge_risk(["tests/test_report.py"], policy=policy)

        self.assertTrue(risk["label_allowed"])
        self.assertEqual(risk["risk_level"], "medium")
        self.assertEqual(risk["risk_reasons"], ["custom medium"])
        self.assertEqual(risk["human_review_label"], "custom-review-required")
        self.assertFalse(blocked["label_allowed"])
        self.assertEqual(blocked["risk_reasons"], ["custom high"])
        self.assertEqual(blocked["human_review_label"], "custom-review-required")

    def test_guarded_auto_merge_policy_fails_closed_when_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / ".github" / "codex_auto_merge_policy.json"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text("{not-json", encoding="utf-8")
            policy = load_guarded_auto_merge_policy(policy_path)

        risk = classify_guarded_auto_merge_risk(["README.md"], policy=policy)

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["high_risk_files"], ["README.md"])
        self.assertEqual(risk["policy_errors"], ["invalid auto-merge policy requires human review"])
        self.assertEqual(risk["risk_reasons"], ["invalid auto-merge policy requires human review"])

    def test_guarded_auto_merge_policy_fails_closed_when_existing_policy_schema_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / ".github" / "codex_auto_merge_policy.json"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text("{}", encoding="utf-8")
            policy = load_guarded_auto_merge_policy(policy_path)

        risk = classify_guarded_auto_merge_risk(["README.md"], policy=policy)

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["high_risk_files"], ["README.md"])
        self.assertEqual(risk["risk_reasons"], ["invalid auto-merge policy schema requires human review"])

    def test_guarded_auto_merge_policy_fails_closed_when_policy_labels_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / ".github" / "codex_auto_merge_policy.json"
            policy_path.parent.mkdir(parents=True)
            payload = json.loads(json.dumps(DEFAULT_GUARDED_AUTO_MERGE_POLICY))
            payload["human_review_label"] = payload["auto_merge_label"]
            policy_path.write_text(json.dumps(payload), encoding="utf-8")
            policy = load_guarded_auto_merge_policy(policy_path)

        risk = classify_guarded_auto_merge_risk(["README.md"], policy=policy)

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["high_risk_files"], ["README.md"])
        self.assertEqual(
            risk["risk_reasons"],
            ["auto-merge and human-review labels must be distinct requires human review"],
        )

    def test_guarded_auto_merge_policy_fails_closed_when_policy_allows_control_plane_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / ".github" / "codex_auto_merge_policy.json"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "auto_merge_label": "auto-merge-ok",
                        "human_review_label": "human-review-required",
                        "monthly_marker_prefix": "<!-- codex-monthly-remediation:issue-",
                        "max_changed_files": 20,
                        "max_changed_lines": 1200,
                        "blocked_path_patterns": [".*secret.*"],
                        "risk_policy": {
                            "low": {"prefixes": ["docs/"], "exact": ["README.md"], "reason": "low"},
                            "medium": {"exact": ["scripts/evaluate_codex_pr_merge.py"], "reason": "medium"},
                            "high": {"reason": "high"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            policy = load_guarded_auto_merge_policy(policy_path)

        risk = classify_guarded_auto_merge_risk(["README.md"], policy=policy)

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["high_risk_files"], ["README.md"])
        self.assertEqual(
            risk["risk_reasons"],
            ["auto-merge policy must keep control-plane paths high-risk: scripts/evaluate_codex_pr_merge.py"],
        )

    def test_guarded_auto_merge_policy_fails_closed_when_policy_allows_control_plane_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / ".github" / "codex_auto_merge_policy.json"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "auto_merge_label": "auto-merge-ok",
                        "human_review_label": "human-review-required",
                        "monthly_marker_prefix": "<!-- codex-monthly-remediation:issue-",
                        "max_changed_files": 20,
                        "max_changed_lines": 1200,
                        "blocked_path_patterns": [".*secret.*"],
                        "risk_policy": {
                            "low": {"prefixes": [".github/"], "exact": ["README.md"], "reason": "low"},
                            "medium": {"exact": [], "reason": "medium"},
                            "high": {"reason": "high"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            policy = load_guarded_auto_merge_policy(policy_path)

        risk = classify_guarded_auto_merge_risk(["README.md"], policy=policy)

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["high_risk_files"], ["README.md"])
        self.assertEqual(
            risk["risk_reasons"],
            [
                "auto-merge policy must keep control-plane paths high-risk: "
                ".github/codex_auto_merge_policy.json, .github/workflows/*"
            ],
        )

    def test_guarded_auto_merge_policy_fails_closed_on_unsupported_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / ".github" / "codex_auto_merge_policy.json"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "auto_merge_label": "auto-merge-ok",
                        "human_review_label": "human-review-required",
                        "monthly_marker_prefix": "<!-- codex-monthly-remediation:issue-",
                        "blocked_path_patterns": [".*secret.*"],
                        "risk_policy": {
                            "low": {"prefixes": ["docs/"], "exact": [], "reason": "low"},
                            "medium": {"exact": [], "reason": "medium"},
                            "high": {"reason": "high"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            policy = load_guarded_auto_merge_policy(policy_path)

        risk = classify_guarded_auto_merge_risk(["docs/runbook.md"], policy=policy)

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["high_risk_files"], ["docs/runbook.md"])
        self.assertEqual(risk["risk_reasons"], ["unsupported auto-merge policy version requires human review"])

    def test_guarded_auto_merge_policy_fails_closed_on_invalid_blocked_regex(self) -> None:
        risk = classify_guarded_auto_merge_risk(
            ["docs/runbook.md"],
            policy={
                "blocked_path_patterns": ["["],
                "risk_policy": {
                    "low": {"prefixes": ["docs/"], "exact": [], "reason": "low"},
                    "medium": {"exact": [], "reason": "medium"},
                    "high": {"reason": "high"},
                },
            },
        )

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["high_risk_files"], ["docs/runbook.md"])
        self.assertEqual(risk["risk_reasons"], ["invalid blocked_path_patterns regex requires human review"])

    def test_guarded_auto_merge_policy_fails_closed_on_malformed_lists(self) -> None:
        risk = classify_guarded_auto_merge_risk(
            ["data/output/report.json"],
            policy={
                "risk_policy": {
                    "low": {"prefixes": "docs/", "exact": [], "reason": "low"},
                    "medium": {"exact": [], "reason": "medium"},
                    "high": {"reason": "high"},
                },
            },
        )

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["high_risk_files"], ["data/output/report.json"])
        self.assertEqual(risk["risk_reasons"], ["invalid risk_policy.low.prefixes list requires human review"])

    def test_baseline_auto_merge_policy_blocks_policy_self_escalation(self) -> None:
        baseline_policy = {
            "risk_policy": {
                "low": {"prefixes": ["docs/"], "exact": ["README.md"], "reason": "baseline low"},
                "medium": {"exact": ["scripts/monthly.py"], "reason": "baseline medium"},
                "high": {"reason": "baseline high"},
            }
        }
        modified_policy = {
            "risk_policy": {
                "low": {
                    "prefixes": ["docs/"],
                    "exact": [".github/codex_auto_merge_policy.json"],
                    "reason": "modified low",
                },
                "medium": {"exact": [], "reason": "modified medium"},
                "high": {"reason": "modified high"},
            }
        }

        baseline_risk = classify_guarded_auto_merge_risk(
            [".github/codex_auto_merge_policy.json"],
            policy=baseline_policy,
        )
        modified_risk = classify_guarded_auto_merge_risk(
            [".github/codex_auto_merge_policy.json"],
            policy=modified_policy,
        )

        self.assertFalse(baseline_risk["label_allowed"])
        self.assertEqual(baseline_risk["risk_reasons"], ["baseline high"])
        self.assertTrue(modified_risk["label_allowed"])
        self.assertEqual(modified_risk["risk_reasons"], ["modified low"])

    def test_classify_guarded_auto_merge_risk_blocks_unknown_or_strategy_surfaces(self) -> None:
        risk = classify_guarded_auto_merge_risk(
            ["src/us_equity_snapshot_pipelines/contracts.py", "pyproject.toml", "data/output/report.json"]
        )

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["risk_level"], "high")
        self.assertEqual(
            risk["high_risk_files"],
            ["src/us_equity_snapshot_pipelines/contracts.py", "pyproject.toml", "data/output/report.json"],
        )

    def test_classify_guarded_auto_merge_risk_blocks_secret_like_paths_before_low_risk_prefixes(self) -> None:
        risk = classify_guarded_auto_merge_risk(["docs/operator-token.md", "tests/private.key", "README.md"])

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["risk_level"], "high")
        self.assertEqual(risk["high_risk_files"], ["docs/operator-token.md", "tests/private.key"])

    def test_classify_guarded_auto_merge_risk_blocks_oversized_low_risk_surface(self) -> None:
        paths = [f"docs/review-{index}.md" for index in range(21)]

        risk = classify_guarded_auto_merge_risk(paths)

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["risk_level"], "high")
        self.assertEqual(risk["high_risk_files"], paths)
        self.assertEqual(
            risk["risk_reasons"],
            ["changed file count exceeds auto-merge limit requires human review: 21 > 20"],
        )

    def test_classify_guarded_auto_merge_risk_blocks_large_low_risk_diff(self) -> None:
        risk = classify_guarded_auto_merge_risk(
            ["docs/review.md"],
            diff_stats={"additions": 1000, "deletions": 201, "binary_files": 0},
        )

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["risk_level"], "high")
        self.assertEqual(risk["high_risk_files"], ["docs/review.md"])
        self.assertEqual(risk["changed_lines"], 1201)
        self.assertEqual(
            risk["risk_reasons"],
            ["changed line count exceeds auto-merge limit requires human review: 1201 > 1200"],
        )

    def test_classify_guarded_auto_merge_risk_blocks_binary_diff(self) -> None:
        risk = classify_guarded_auto_merge_risk(
            ["docs/chart.png"],
            diff_stats={"additions": 0, "deletions": 0, "binary_files": 1},
        )

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["risk_level"], "high")
        self.assertEqual(risk["risk_reasons"], ["binary file changes require human review"])

    def test_classify_guarded_auto_merge_risk_blocks_file_removals_renames_and_copies(self) -> None:
        risk = classify_guarded_auto_merge_risk(
            ["docs/old.md", "docs/new.md", "tests/test_copy.py"],
            diff_stats={
                "additions": 4,
                "deletions": 3,
                "binary_files": 0,
                "deleted_files": 1,
                "renamed_files": 1,
                "copied_files": 1,
            },
        )

        self.assertFalse(risk["label_allowed"])
        self.assertEqual(risk["risk_level"], "high")
        self.assertEqual(risk["high_risk_files"], ["docs/old.md", "docs/new.md", "tests/test_copy.py"])
        self.assertEqual(
            risk["risk_reasons"],
            [
                "file deletions require human review",
                "file renames require human review",
                "file copies require human review",
            ],
        )
        self.assertEqual(risk["deleted_files"], 1)
        self.assertEqual(risk["renamed_files"], 1)
        self.assertEqual(risk["copied_files"], 1)

    def test_request_guarded_auto_merge_requires_diff_stats_for_low_risk_label(self) -> None:
        with (
            patch("scripts.run_monthly_codex_audit.github_request", return_value={}) as request,
            self.assertRaisesRegex(BridgeError, "diff stats are unavailable"),
        ):
            request_guarded_auto_merge(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                ["docs/operator_runbook.md"],
            )

        request.assert_not_called()

    def test_issue_has_label_reads_current_pr_labels(self) -> None:
        with patch(
            "scripts.run_monthly_codex_audit.github_request",
            return_value={"labels": [{"name": HUMAN_REVIEW_LABEL}, {"name": "other"}]},
        ) as request:
            has_label = issue_has_label(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                HUMAN_REVIEW_LABEL,
            )

        self.assertTrue(has_label)
        request.assert_called_once_with(
            "token",
            "GET",
            "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12",
        )

    def test_request_guarded_auto_merge_refuses_existing_human_review_label(self) -> None:
        with (
            patch(
                "scripts.run_monthly_codex_audit.github_request",
                return_value={"labels": [{"name": HUMAN_REVIEW_LABEL}]},
            ) as request,
            self.assertRaisesRegex(BridgeError, "human-review-required.*present"),
        ):
            request_guarded_auto_merge(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                ["docs/operator_runbook.md"],
                diff_stats={"additions": 10, "deletions": 2, "binary_files": 0},
            )

        request.assert_called_once_with(
            "token",
            "GET",
            "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12",
        )

    def test_request_guarded_auto_merge_adds_source_guard_label(self) -> None:
        with patch("scripts.run_monthly_codex_audit.github_request", side_effect=[{"labels": []}, {}, {}]) as request:
            guard = request_guarded_auto_merge(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                ["docs/operator_runbook.md"],
                diff_stats={"additions": 10, "deletions": 2, "binary_files": 0},
            )

        self.assertEqual(guard["label"], GUARDED_AUTO_MERGE_LABEL)
        self.assertEqual(guard["risk_level"], "low")
        request.assert_has_calls(
            [
                call("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12"),
                call("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/auto-merge-ok"),
                call(
                    "token",
                    "POST",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels",
                    {"labels": [GUARDED_AUTO_MERGE_LABEL]},
                ),
            ]
        )

    def test_request_guarded_auto_merge_creates_missing_source_guard_label(self) -> None:
        not_found = GitHubRequestError(
            "GET",
            "https://api.github.com/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/auto-merge-ok",
            404,
            '{"message":"Not Found"}',
        )
        with patch(
            "scripts.run_monthly_codex_audit.github_request",
            side_effect=[{"labels": []}, not_found, {}, {}],
        ) as request:
            guard = request_guarded_auto_merge(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                ["docs/operator_runbook.md"],
                diff_stats={"additions": 10, "deletions": 2, "binary_files": 0},
            )

        self.assertEqual(guard["label"], GUARDED_AUTO_MERGE_LABEL)
        request.assert_has_calls(
            [
                call("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12"),
                call("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/auto-merge-ok"),
                call(
                    "token",
                    "POST",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels",
                    {
                        "name": GUARDED_AUTO_MERGE_LABEL,
                        "color": "0E8A16",
                        "description": (
                            "Guarded Codex remediation PR may be auto-merged after source CI and merge guard pass"
                        ),
                    },
                ),
                call(
                    "token",
                    "POST",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels",
                    {"labels": [GUARDED_AUTO_MERGE_LABEL]},
                ),
            ]
        )

    def test_request_guarded_auto_merge_fails_closed_when_label_check_is_forbidden(self) -> None:
        forbidden = GitHubRequestError(
            "GET",
            "https://api.github.com/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/auto-merge-ok",
            403,
            '{"message":"Forbidden"}',
        )
        with (
            patch("scripts.run_monthly_codex_audit.github_request", side_effect=[{"labels": []}, forbidden]) as request,
            self.assertRaises(BridgeError),
        ):
            request_guarded_auto_merge(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                ["docs/operator_runbook.md"],
                diff_stats={"additions": 10, "deletions": 2, "binary_files": 0},
            )

        request.assert_has_calls(
            [
                call("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12"),
                call("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/auto-merge-ok"),
            ]
        )

    def test_request_guarded_auto_merge_fails_closed_when_pr_label_add_is_forbidden(self) -> None:
        forbidden = GitHubRequestError(
            "POST",
            "https://api.github.com/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels",
            403,
            '{"message":"Forbidden"}',
        )
        with (
            patch("scripts.run_monthly_codex_audit.github_request", side_effect=[{"labels": []}, {}, forbidden]) as request,
            self.assertRaises(GitHubRequestError),
        ):
            request_guarded_auto_merge(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                ["docs/operator_runbook.md"],
                diff_stats={"additions": 10, "deletions": 2, "binary_files": 0},
            )

        request.assert_has_calls(
            [
                call("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12"),
                call("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/auto-merge-ok"),
                call(
                    "token",
                    "POST",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels",
                    {"labels": [GUARDED_AUTO_MERGE_LABEL]},
                ),
            ]
        )

    def test_request_guarded_auto_merge_uses_policy_label(self) -> None:
        policy = {
            "auto_merge_label": "custom-auto-ok",
            "human_review_label": "custom-review-required",
            "risk_policy": {
                "low": {"prefixes": ["docs/"], "exact": [], "reason": "low"},
                "medium": {"exact": [], "reason": "medium"},
                "high": {"reason": "high"},
            },
        }
        with patch("scripts.run_monthly_codex_audit.github_request", side_effect=[{"labels": []}, {}, {}]) as request:
            guard = request_guarded_auto_merge(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                ["docs/operator_runbook.md"],
                policy=policy,
                diff_stats={"additions": 10, "deletions": 2, "binary_files": 0},
            )

        self.assertEqual(guard["label"], "custom-auto-ok")
        request.assert_has_calls(
            [
                call("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12"),
                call("token", "GET", "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/custom-auto-ok"),
                call(
                    "token",
                    "POST",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels",
                    {"labels": ["custom-auto-ok"]},
                ),
            ]
        )

    def test_request_guarded_auto_merge_rejects_invalid_policy_label(self) -> None:
        with (
            patch("scripts.run_monthly_codex_audit.github_request", return_value={}) as request,
            self.assertRaises(BridgeError),
        ):
            request_guarded_auto_merge(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                ["docs/operator_runbook.md"],
                policy={
                    "auto_merge_label": ["bad"],
                    "human_review_label": "human-review-required",
                    "risk_policy": {
                        "low": {"prefixes": ["docs/"], "exact": [], "reason": "low"},
                        "medium": {"exact": [], "reason": "medium"},
                        "high": {"reason": "high"},
                    },
                },
            )

        request.assert_not_called()

    def test_create_pull_request_uses_policy_marker_prefix(self) -> None:
        with patch(
            "scripts.run_monthly_codex_audit.github_request",
            return_value={"number": 12, "html_url": "https://example.test/pr/12"},
        ) as request:
            create_pull_request(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                {"number": 7, "html_url": "https://example.test/issues/7"},
                "codex/monthly-review-issue-7",
                "main",
                "review",
                ["docs/operator_runbook.md"],
                policy={"monthly_marker_prefix": "<!-- custom-remediation:issue-"},
            )

        payload = request.call_args.args[3]
        self.assertTrue(payload["body"].startswith("<!-- custom-remediation:issue-7 -->"))

    def test_request_guarded_auto_merge_rejects_high_risk_paths_before_labeling(self) -> None:
        with (
            patch("scripts.run_monthly_codex_audit.github_request", return_value={}) as request,
            self.assertRaises(BridgeError),
        ):
            request_guarded_auto_merge(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                ["src/us_equity_snapshot_pipelines/contracts.py"],
            )

        request.assert_not_called()

    def test_remove_issue_label_if_present_deletes_stale_guard_label(self) -> None:
        with patch("scripts.run_monthly_codex_audit.github_request", return_value={}) as request:
            removed = remove_issue_label_if_present(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                GUARDED_AUTO_MERGE_LABEL,
            )

        self.assertTrue(removed)
        request.assert_called_once_with(
            "token",
            "DELETE",
            "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels/auto-merge-ok",
        )

    def test_remove_issue_label_if_present_ignores_missing_label(self) -> None:
        not_found = GitHubRequestError(
            "DELETE",
            "https://api.github.com/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels/auto-merge-ok",
            404,
            '{"message":"Not Found"}',
        )
        with patch("scripts.run_monthly_codex_audit.github_request", side_effect=not_found):
            removed = remove_issue_label_if_present(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                GUARDED_AUTO_MERGE_LABEL,
            )

        self.assertFalse(removed)

    def test_remove_issue_label_if_present_fails_closed_on_permission_error(self) -> None:
        forbidden = GitHubRequestError(
            "DELETE",
            "https://api.github.com/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels/auto-merge-ok",
            403,
            '{"message":"Forbidden"}',
        )
        with (
            patch("scripts.run_monthly_codex_audit.github_request", side_effect=forbidden),
            self.assertRaises(GitHubRequestError),
        ):
            remove_issue_label_if_present(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                GUARDED_AUTO_MERGE_LABEL,
            )

    def test_guarded_auto_merge_label_uses_policy_label(self) -> None:
        self.assertEqual(
            guarded_auto_merge_label({"auto_merge_label": "custom-auto-ok"}),
            "custom-auto-ok",
        )

    def test_guarded_auto_merge_label_for_mutation_skips_invalid_policy(self) -> None:
        label, reason = guarded_auto_merge_label_for_mutation(
            {
                "policy_errors": ["invalid auto-merge policy requires human review"],
                "auto_merge_label": "auto-merge-ok",
                "human_review_label": "human-review-required",
            }
        )

        self.assertEqual(label, "")
        self.assertEqual(reason, "invalid auto-merge policy requires human review")

    def test_guarded_auto_merge_label_for_mutation_rejects_label_collision(self) -> None:
        label, reason = guarded_auto_merge_label_for_mutation(
            {
                "auto_merge_label": "same-label",
                "human_review_label": "same-label",
            }
        )

        self.assertEqual(label, "")
        self.assertEqual(reason, "auto-merge and human-review labels must be distinct requires human review")

    def test_main_clears_stale_auto_merge_label_before_no_change_feedback_retry_exit(self) -> None:
        def fake_clone(token: str, source_repo: str, source_ref: str, work_root: Path) -> Path:
            repo_dir = work_root / "source"
            (repo_dir / ".git" / "info").mkdir(parents=True)
            return repo_dir

        issue = {
            "number": 7,
            "title": "Monthly Review",
            "html_url": "https://example.test/issues/7",
            "body": "review body",
            "labels": [],
        }
        retry_pr = {
            "number": 12,
            "html_url": "https://example.test/pull/12",
            "head_ref": "codex/monthly-review-issue-7-202606",
            "base_ref": "main",
        }
        env = {
            "SOURCE_REPO": "QuantStrategyLab/UsEquitySnapshotPipelines",
            "SOURCE_REF": "main",
            "ISSUE_NUMBER": "7",
            "CODEX_AUDIT_GH_TOKEN": "token",
            "CODEX_AUDIT_TASK": "monthly_snapshot_audit",
            "CODEX_AUDIT_MODE": "review_and_fix",
            "CODEX_AUDIT_PROVIDER": "auto",
            "CODEX_AUDIT_CODEX_BACKEND": "service",
            "CODEX_AUDIT_AUTO_MERGE": "false",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("scripts.run_monthly_codex_audit.github_request", return_value=issue),
            patch("scripts.run_monthly_codex_audit.fetch_issue_comments", return_value=[]),
            patch("scripts.run_monthly_codex_audit.clone_source_repo", side_effect=fake_clone),
            patch("scripts.run_monthly_codex_audit.resolve_feedback_retry_pr", return_value=retry_pr),
            patch("scripts.run_monthly_codex_audit.remove_issue_label_if_present", return_value=True) as remove,
            patch("scripts.run_monthly_codex_audit.checkout_feedback_retry_branch"),
            patch("scripts.run_monthly_codex_audit.run_checked", return_value=""),
            patch("scripts.run_monthly_codex_audit.run_codex_backend", return_value=(0, "", "No changes.")),
            patch("scripts.run_monthly_codex_audit.git_status", return_value=""),
            patch("scripts.run_monthly_codex_audit.post_issue_comment") as comment,
        ):
            exit_code = run_audit_main()

        self.assertEqual(exit_code, 0)
        remove.assert_called_once_with(
            "token",
            "QuantStrategyLab/UsEquitySnapshotPipelines",
            12,
            GUARDED_AUTO_MERGE_LABEL,
        )
        body = comment.call_args.args[3]
        self.assertIn("No changes.", body)
        self.assertIn("Removed stale `auto-merge-ok`", body)
        self.assertIn("did not produce a verified replacement commit", body)

    def test_main_skips_stale_auto_merge_label_cleanup_when_policy_labels_collide(self) -> None:
        def fake_clone(token: str, source_repo: str, source_ref: str, work_root: Path) -> Path:
            repo_dir = work_root / "source"
            (repo_dir / ".git" / "info").mkdir(parents=True)
            policy = dict(DEFAULT_GUARDED_AUTO_MERGE_POLICY)
            policy["human_review_label"] = policy["auto_merge_label"]
            policy_path = repo_dir / ".github" / "codex_auto_merge_policy.json"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            return repo_dir

        issue = {
            "number": 7,
            "title": "Monthly Review",
            "html_url": "https://example.test/issues/7",
            "body": "review body",
            "labels": [],
        }
        retry_pr = {
            "number": 12,
            "html_url": "https://example.test/pull/12",
            "head_ref": "codex/monthly-review-issue-7-202606",
            "base_ref": "main",
        }
        env = {
            "SOURCE_REPO": "QuantStrategyLab/UsEquitySnapshotPipelines",
            "SOURCE_REF": "main",
            "ISSUE_NUMBER": "7",
            "CODEX_AUDIT_GH_TOKEN": "token",
            "CODEX_AUDIT_TASK": "monthly_snapshot_audit",
            "CODEX_AUDIT_MODE": "review_and_fix",
            "CODEX_AUDIT_PROVIDER": "auto",
            "CODEX_AUDIT_CODEX_BACKEND": "service",
            "CODEX_AUDIT_AUTO_MERGE": "false",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("scripts.run_monthly_codex_audit.github_request", return_value=issue),
            patch("scripts.run_monthly_codex_audit.fetch_issue_comments", return_value=[]),
            patch("scripts.run_monthly_codex_audit.clone_source_repo", side_effect=fake_clone),
            patch("scripts.run_monthly_codex_audit.resolve_feedback_retry_pr", return_value=retry_pr),
            patch("scripts.run_monthly_codex_audit.remove_issue_label_if_present") as remove,
            patch("scripts.run_monthly_codex_audit.checkout_feedback_retry_branch"),
            patch("scripts.run_monthly_codex_audit.run_checked", return_value=""),
            patch("scripts.run_monthly_codex_audit.run_codex_backend", return_value=(0, "", "No changes.")),
            patch("scripts.run_monthly_codex_audit.git_status", return_value=""),
            patch("scripts.run_monthly_codex_audit.post_issue_comment") as comment,
        ):
            exit_code = run_audit_main()

        self.assertEqual(exit_code, 0)
        remove.assert_not_called()
        body = comment.call_args.args[3]
        self.assertIn("No changes.", body)
        self.assertIn("Skipped stale guarded auto-merge label cleanup", body)
        self.assertIn("auto-merge and human-review labels must be distinct requires human review", body)

    def test_format_guarded_risk_details_includes_reasons_and_files(self) -> None:
        risk = classify_guarded_auto_merge_risk(
            ["src/us_equity_snapshot_pipelines/contracts.py", "pyproject.toml"]
        )

        details = format_guarded_risk_details(risk)

        self.assertIn("Risk level: `high`", details)
        self.assertIn("blocked/high-risk files require human review", details)
        self.assertIn("`src/us_equity_snapshot_pipelines/contracts.py`", details)
        self.assertIn("`pyproject.toml`", details)

    def test_request_human_review_adds_review_label(self) -> None:
        risk = classify_guarded_auto_merge_risk(["src/us_equity_snapshot_pipelines/contracts.py"])
        with patch("scripts.run_monthly_codex_audit.github_request", return_value={}) as request:
            review = request_human_review(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                risk,
            )

        self.assertEqual(review["label"], HUMAN_REVIEW_LABEL)
        self.assertEqual(review["risk_level"], "high")
        request.assert_has_calls(
            [
                call(
                    "token",
                    "GET",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/human-review-required",
                ),
                call(
                    "token",
                    "POST",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels",
                    {"labels": [HUMAN_REVIEW_LABEL]},
                ),
            ]
        )

    def test_request_human_review_creates_missing_review_label(self) -> None:
        not_found = GitHubRequestError(
            "GET",
            "https://api.github.com/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/human-review-required",
            404,
            '{"message":"Not Found"}',
        )
        risk = classify_guarded_auto_merge_risk(["src/us_equity_snapshot_pipelines/contracts.py"])
        with patch("scripts.run_monthly_codex_audit.github_request", side_effect=[not_found, {}, {}]) as request:
            review = request_human_review(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                risk,
            )

        self.assertEqual(review["label"], HUMAN_REVIEW_LABEL)
        request.assert_has_calls(
            [
                call(
                    "token",
                    "GET",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/human-review-required",
                ),
                call(
                    "token",
                    "POST",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels",
                    {
                        "name": HUMAN_REVIEW_LABEL,
                        "color": "B60205",
                        "description": "Codex remediation PR requires human review before merge",
                    },
                ),
                call(
                    "token",
                    "POST",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels",
                    {"labels": [HUMAN_REVIEW_LABEL]},
                ),
            ]
        )

    def test_request_human_review_fails_closed_when_pr_label_add_is_forbidden(self) -> None:
        forbidden = GitHubRequestError(
            "POST",
            "https://api.github.com/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels",
            403,
            '{"message":"Forbidden"}',
        )
        risk = classify_guarded_auto_merge_risk(["src/us_equity_snapshot_pipelines/contracts.py"])
        with (
            patch("scripts.run_monthly_codex_audit.github_request", side_effect=[{}, forbidden]) as request,
            self.assertRaises(GitHubRequestError),
        ):
            request_human_review(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                risk,
            )

        request.assert_has_calls(
            [
                call(
                    "token",
                    "GET",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/human-review-required",
                ),
                call(
                    "token",
                    "POST",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels",
                    {"labels": [HUMAN_REVIEW_LABEL]},
                ),
            ]
        )

    def test_request_human_review_uses_policy_label(self) -> None:
        risk = classify_guarded_auto_merge_risk(
            ["src/us_equity_snapshot_pipelines/contracts.py"],
            policy={
                "human_review_label": "custom-review-required",
                "risk_policy": {
                    "low": {"prefixes": ["docs/"], "exact": [], "reason": "low"},
                    "medium": {"exact": [], "reason": "medium"},
                    "high": {"reason": "high"},
                },
            },
        )
        with patch("scripts.run_monthly_codex_audit.github_request", return_value={}) as request:
            review = request_human_review(
                "token",
                "QuantStrategyLab/UsEquitySnapshotPipelines",
                12,
                risk,
            )

        self.assertEqual(review["label"], "custom-review-required")
        request.assert_has_calls(
            [
                call(
                    "token",
                    "GET",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/labels/custom-review-required",
                ),
                call(
                    "token",
                    "POST",
                    "/repos/QuantStrategyLab/UsEquitySnapshotPipelines/issues/12/labels",
                    {"labels": ["custom-review-required"]},
                ),
            ]
        )

    def test_workflow_uses_service_backend_only(self) -> None:
        workflow = Path(".github/workflows/codex_audit.yml").read_text(encoding="utf-8")

        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertIn('default: "task_default"', workflow)
        self.assertIn("          - task_default", workflow)
        self.assertIn("CODEX_AUDIT_PROVIDER: ${{ github.event.client_payload.provider || inputs.provider || 'task_default' }}", workflow)
        self.assertIn("CODEX_AUDIT_CODEX_BACKEND: service", workflow)
        self.assertIn("CODEX_AUDIT_SERVICE_URL: ${{ secrets.CODEX_AUDIT_SERVICE_URL }}", workflow)
        self.assertNotIn("codex_backend:", workflow)
        self.assertNotIn("self-hosted", workflow)
        self.assertNotIn("vars.CODEX_AUDIT_SERVICE_URL", workflow)
        self.assertIn("actions/checkout@v6.0.3", workflow)
        self.assertIn("actions/create-github-app-token@v3.2.0", workflow)

    def test_vps_ops_workflow_runs_only_manual_self_hosted_ops(self) -> None:
        workflow = Path(".github/workflows/vps_codex_service_ops.yml").read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("- self-hosted", workflow)
        self.assertIn("- codex-vps", workflow)
        self.assertIn('bash scripts/deploy_codex_audit_service.sh "${{ inputs.mode }}"', workflow)
        self.assertIn("CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES", workflow)
        self.assertIn("CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS", workflow)
        self.assertIn("CODEX_AUDIT_SERVICE_ALLOWED_REFS", workflow)
        self.assertIn("CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES", workflow)
        self.assertIn("CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE", workflow)
        self.assertIn("CODEX_AUDIT_SERVICE_OPENAI_USAGE_WINDOW_DAYS", workflow)
        self.assertIn("CODEX_AUDIT_SERVICE_ANTHROPIC_USAGE_WINDOW_DAYS", workflow)
        self.assertIn("OPENAI_ADMIN_KEY: ${{ secrets.OPENAI_ADMIN_KEY }}", workflow)
        self.assertIn("ANTHROPIC_ADMIN_KEY: ${{ secrets.ANTHROPIC_ADMIN_KEY }}", workflow)
        self.assertIn("QuantStrategyLab/AIAuditBridge,QuantStrategyLab/CryptoLivePoolPipelines", workflow)
        self.assertNotIn("QuantStrategyLab/CodexAuditBridge", workflow)
        self.assertIn("actions/checkout@v6.0.3", workflow)

    def test_vps_deploy_adds_nginx_audit_route_without_router_service(self) -> None:
        deploy_script = Path("scripts/deploy_codex_audit_service.sh").read_text(encoding="utf-8")

        self.assertIn("location = /v1/codex-audit", deploy_script)
        self.assertIn("location ^~ /v1/codex-audit/", deploy_script)
        self.assertIn("CODEX_AUDIT_SERVICE_JOB_DIR", deploy_script)
        self.assertIn("CODEX_AUDIT_SERVICE_EXECUTION_POLICY_PATH", deploy_script)
        self.assertIn("/etc/codex-audit-bridge-policy/execution_policy.json", deploy_script)
        self.assertIn("write_default_execution_policy_if_missing", deploy_script)
        self.assertIn("O_NOFOLLOW", deploy_script)
        self.assertIn("os.O_DIRECTORY", deploy_script)
        self.assertIn("dir_fd=fd", deploy_script)
        self.assertIn("os.open(component, flags_dir, dir_fd=fd)", deploy_script)
        self.assertIn('"max_consecutive_failures": 3', deploy_script)
        self.assertIn("codex_pr_review.yml@refs/pull/*/merge", deploy_script)
        self.assertIn("refs/pull/*/merge", deploy_script)
        self.assertIn("QuantStrategyLab/AIAuditBridge", deploy_script)
        self.assertIn("QuantStrategyLab/QuantRuntimeSettings", deploy_script)
        self.assertIn("QuantStrategyLab/QuantPlatformKit", deploy_script)
        self.assertIn("QuantStrategyLab/CryptoLivePoolPipelines", deploy_script)
        self.assertNotIn("QuantStrategyLab/CodexAuditBridge", deploy_script)
        self.assertIn("proxy_pass http://127.0.0.1:{port}", deploy_script)
        self.assertIn('"# CodexAuditBridge route start" not in block', deploy_script)
        self.assertIn("audit service did not become healthy", deploy_script)
        self.assertIn("nginx config test failed; restoring previous config", deploy_script)
        self.assertIn("zzzz-managed-allowlists.conf", deploy_script)
        self.assertIn('Environment="CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=${ALLOWED_REPOSITORIES}"', deploy_script)
        self.assertIn("systemctl_environment_brief", deploy_script)
        self.assertIn('sed -E "s/^[\\"', deploy_script)
        self.assertIn('s/[\\"\']$//"', deploy_script)
        self.assertIn("CODEX_AUDIT_SERVICE_(ALLOWED_|AUDIENCE=", deploy_script)
        self.assertIn("CODEX_ACCOUNT_USAGE=", deploy_script)
        self.assertIn("OPENAI_USAGE_WINDOW_DAYS=", deploy_script)
        self.assertIn("ANTHROPIC_USAGE_WINDOW_DAYS=", deploy_script)
        self.assertIn("CODEX_AUDIT_SERVICE_ADMIN_ENV_FILE", deploy_script)
        self.assertIn("EnvironmentFile=-${ADMIN_ENV_FILE}", deploy_script)
        self.assertIn("sudo install -m 0600 -o root -g root", deploy_script)
        self.assertIn('sudo rm -f "$ADMIN_ENV_FILE"', deploy_script)
        self.assertNotIn("Environment=OPENAI_ADMIN_KEY=", deploy_script)
        self.assertNotIn("Environment=ANTHROPIC_ADMIN_KEY=", deploy_script)
        self.assertNotIn("^OPENAI_ADMIN_KEY", deploy_script)
        self.assertNotIn("^ANTHROPIC_ADMIN_KEY", deploy_script)
        self.assertNotIn("^CODEX_AUDIT_SERVICE_TOKEN", deploy_script)
        self.assertNotIn("CODEX_SERVICE_ROUTER", deploy_script)
        self.assertNotIn("codex_service_router", deploy_script)


if __name__ == "__main__":
    unittest.main()
