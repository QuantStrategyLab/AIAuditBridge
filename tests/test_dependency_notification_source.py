"""Focused tests for controlled GitHub PR → trusted intake schema v1 adapter."""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from scripts import run_dependency_notification_source_dry_run as cli
from service import dependency_notification_source as source
from service import dependency_notification_triage as triage


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
HEAD_SHA_2 = "c" * 40
HEAD_SHA_3 = "d" * 40


def _pr(
    *,
    number: int = 101,
    login: str = "dependabot[bot]",
    base_sha: str = BASE_SHA,
    head_sha: str = HEAD_SHA,
    body: str = "version-update:semver-minor\n",
    labels: list | None = None,
    title: str = "Bump anyio from 4.0.0 to 4.1.0",
):
    return {
        "number": number,
        "title": title,
        "body": body,
        "user": {"login": login},
        "base": {"sha": base_sha, "ref": "main"},
        "head": {"sha": head_sha, "ref": "dependabot/pip/anyio-4.1.0"},
        "labels": labels or [{"name": "dependencies"}],
    }


def _files(*names: str):
    return [{"filename": name, "status": "modified"} for name in names]


class ParseAllowlistTests(unittest.TestCase):
    def test_empty_allowlist_rejected(self) -> None:
        with self.assertRaises(source.DependencyNotificationSourceError):
            source.parse_repo_allowlist(None)
        with self.assertRaises(source.DependencyNotificationSourceError):
            source.parse_repo_allowlist("")
        with self.assertRaises(source.DependencyNotificationSourceError):
            source.parse_repo_allowlist("  ,  ")

    def test_parses_and_dedupes_repos(self) -> None:
        repos = source.parse_repo_allowlist(
            "QuantStrategyLab/A, QuantStrategyLab/B,QuantStrategyLab/A"
        )
        self.assertEqual(
            repos,
            ("QuantStrategyLab/A", "QuantStrategyLab/B"),
        )


class BuildTrustedEventTests(unittest.TestCase):
    def test_dependabot_patch_manifest_lock_ci_success(self) -> None:
        event = source.build_trusted_event(
            repository="QuantStrategyLab/ExampleRepo",
            pr=_pr(body="version-update:semver-patch\n"),
            files=_files("pyproject.toml", "uv.lock"),
            ci_status="success",
        )
        self.assertEqual(event["update_class"], "patch")
        self.assertEqual(event["author_login"], "dependabot[bot]")
        self.assertEqual(event["ci_status"], "success")
        self.assertTrue(event["dependency_only_manifest_change"])
        self.assertFalse(event["qpk_pin_changed"])
        self.assertNotIn("title", event)
        self.assertNotIn("body", event)
        payload = source.build_trusted_intake_payload([event])
        preview = triage.run_trusted_intake_dry_run(payload)
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["counts"]["quiet"], 1)

    def test_workflow_and_qpk_surfaces_escalate(self) -> None:
        workflow = source.build_trusted_event(
            repository="QuantStrategyLab/QuantRuntimeSettings",
            pr=_pr(number=10, head_sha=HEAD_SHA, body="version-update:semver-patch\n"),
            files=_files(".github/workflows/validate.yml"),
            ci_status="success",
        )
        qpk = source.build_trusted_event(
            repository="QuantStrategyLab/UsEquityStrategies",
            pr=_pr(number=11, head_sha=HEAD_SHA_2, body="version-update:semver-minor\n"),
            files=_files("requirements.txt"),
            ci_status="success",
            qpk_pin_changed=True,
        )
        action = source.build_trusted_event(
            repository="QuantStrategyLab/ExampleRepo",
            pr=_pr(number=12, head_sha=HEAD_SHA_3, body="version-update:semver-patch\n"),
            files=_files(".github/actions/setup/action.yml"),
            ci_status="success",
        )
        preview = triage.run_trusted_intake_dry_run(
            source.build_trusted_intake_payload([workflow, qpk, action])
        )
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["counts"]["telegram"], 3)

    def test_missing_structured_fields_fail_closed_unknown(self) -> None:
        event = source.build_trusted_event(
            repository="QuantStrategyLab/ExampleRepo",
            pr=_pr(body="Bumps anyio. No version-update marker."),
            files=_files("pyproject.toml", "uv.lock"),
            ci_status="success",
        )
        self.assertEqual(event["update_class"], "unknown")
        preview = triage.run_trusted_intake_dry_run(source.build_trusted_intake_payload([event]))
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["events"][0]["decision"], "telegram")
        self.assertTrue(preview["events"][0]["review_required"])

    def test_title_alone_never_marks_quiet(self) -> None:
        event = source.build_trusted_event(
            repository="QuantStrategyLab/ExampleRepo",
            pr=_pr(
                title="chore(deps): bump anyio patch",
                body="human prose without markers",
                labels=[],
            ),
            files=_files("pyproject.toml", "uv.lock"),
            ci_status="success",
        )
        self.assertEqual(event["update_class"], "unknown")
        preview = triage.run_trusted_intake_dry_run(source.build_trusted_intake_payload([event]))
        self.assertNotEqual(preview["events"][0]["decision"], "quiet")

    def test_invalid_sha_or_files_produce_fail_closed_event(self) -> None:
        event = source.build_trusted_event(
            repository="QuantStrategyLab/ExampleRepo",
            pr=_pr(base_sha="short", head_sha="also-short", body="version-update:semver-patch\n"),
            files=[],
            ci_status="",
        )
        self.assertEqual(event["update_class"], "unknown")
        self.assertEqual(event["ci_status"], "unknown")
        self.assertFalse(event["dependency_only_manifest_change"])
        self.assertEqual(event["changed_files"], ["__missing_changed_files__"])
        self.assertEqual(event["base_sha"], "0" * 40)
        self.assertEqual(event["head_sha"], "0" * 40)
        preview = triage.run_trusted_intake_dry_run(source.build_trusted_intake_payload([event]))
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["events"][0]["decision"], "telegram")
        self.assertTrue(preview["events"][0]["review_required"])


class CollectAndCliTests(unittest.TestCase):
    def test_allowlist_rejects_unlisted_repo(self) -> None:
        with self.assertRaises(source.DependencyNotificationSourceError) as ctx:
            source.assert_repo_allowed(
                "QuantStrategyLab/Other",
                ("QuantStrategyLab/ExampleRepo",),
            )
        self.assertIn("allowlist", str(ctx.exception).lower())

    def test_collect_events_mock_get_only_and_preview(self) -> None:
        allowlist = ("QuantStrategyLab/ExampleRepo",)
        pr = _pr(body="version-update:semver-minor\n")
        calls: list[tuple[str, str]] = []

        def fake_request(token, method, path, payload=None):
            calls.append((method, path))
            self.assertEqual(token, "token")
            self.assertIsNone(payload)
            if method != "GET":
                raise AssertionError(f"unexpected method {method}")
            if path.startswith("/repos/QuantStrategyLab/ExampleRepo/pulls?") or path.startswith(
                "/repos/QuantStrategyLab/ExampleRepo/pulls&"
            ):
                return [pr]
            if "/pulls/101/files" in path:
                return _files("requirements.txt", "requirements-lock.txt")
            if "/commits/" in path and path.endswith("/check-runs?per_page=100"):
                return {
                    "check_runs": [
                        {"status": "completed", "conclusion": "success", "name": "test"}
                    ]
                }
            if "/pulls?" in path or "/pulls&" in path:
                return []
            raise AssertionError(f"unexpected path {path}")

        def fake_list_all(token, path, *, max_pages=20):
            calls.append(("GET", path))
            if "/pulls?" in path or path.endswith("/pulls"):
                return [pr]
            if "/files" in path:
                return _files("requirements.txt")
            raise AssertionError(path)

        with (
            patch.object(source, "github_request", side_effect=fake_request),
            patch.object(source, "github_list_all", side_effect=fake_list_all),
        ):
            events, summary = source.collect_trusted_events(
                token="token",
                allowlist=allowlist,
                max_prs_per_repo=5,
            )
        self.assertEqual(summary["repositories"], 1)
        self.assertEqual(summary["events"], 1)
        self.assertTrue(all(method == "GET" for method, _ in calls))
        self.assertFalse(any(m in {"POST", "PATCH", "PUT", "DELETE"} for m, _ in calls))
        preview = triage.run_trusted_intake_dry_run(source.build_trusted_intake_payload(events))
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["counts"]["quiet"], 1)

    def test_api_failure_and_pagination_limit_fail_closed(self) -> None:
        allowlist = ("QuantStrategyLab/ExampleRepo",)

        def boom(token, method, path, payload=None):
            raise source.GitHubRequestError("GET", "https://api.github.com/x", 500, "secret-body")

        with patch.object(source, "github_request", side_effect=boom):
            with patch.object(
                source,
                "github_list_all",
                side_effect=source.DependencyAuditError(
                    "GitHub pagination limit reached: /repos/x/pulls"
                ),
            ):
                result = source.collect_or_fail_closed(
                    token="token",
                    allowlist=allowlist,
                )
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "telegram")
        self.assertTrue(result["review_required"])
        blob = json.dumps(result)
        self.assertNotIn("secret-body", blob)
        self.assertNotIn("token", blob.lower().replace("token_missing", ""))

    def test_cli_requires_allowlist_and_redacts_output(self) -> None:
        env = {
            key: value
            for key, value in __import__("os").environ.items()
            if key
            not in {
                "DEPENDENCY_NOTIFICATION_REPO_ALLOWLIST",
                "CODEX_AUDIT_GH_TOKEN",
                "GH_TOKEN",
                "GITHUB_TOKEN",
            }
        }
        with (
            patch.dict("os.environ", env, clear=True),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = cli.main([])
        self.assertNotEqual(code, 0)

    def test_cli_dry_run_preview_with_mocks(self) -> None:
        pr = _pr(body="version-update:semver-patch\n")

        def fake_list_all(token, path, *, max_pages=20):
            if "/files" in path:
                return _files("package.json", "package-lock.json")
            return [pr]

        def fake_request(token, method, path, payload=None):
            self.assertEqual(method, "GET")
            if "check-runs" in path:
                return {
                    "check_runs": [
                        {"status": "completed", "conclusion": "success", "name": "ci"}
                    ]
                }
            return {}

        with (
            patch.object(cli, "resolve_source_token", return_value="token"),
            patch.object(source, "github_list_all", side_effect=fake_list_all),
            patch.object(source, "github_request", side_effect=fake_request),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = cli.main(
                [
                    "--repos",
                    "QuantStrategyLab/SchwabTokenAutoRefresher",
                ]
            )
        self.assertEqual(code, 0)

    def test_cli_network_failure_nonzero_redacted(self) -> None:
        with (
            patch.object(cli, "resolve_source_token", return_value="token"),
            patch.object(
                source,
                "collect_or_fail_closed",
                return_value={
                    "ok": False,
                    "action": "telegram",
                    "review_required": True,
                    "confidence": "unknown",
                    "error": "github_api_failed",
                    "reasons": ["github_api_failed"],
                    "counts": {
                        "events": 0,
                        "quiet": 0,
                        "telegram": 0,
                        "github_issue": 0,
                        "deduped": 0,
                    },
                    "events": [],
                    "source": {"safe_summary": "github_api_failed"},
                    "dispatch": {
                        "action": "telegram",
                        "telegram_sent": False,
                        "github_issue": None,
                        "telegram_dry_run": {
                            "present": True,
                            "safe_summary": "intake_validation_failed",
                        },
                        "github_dry_run": {"present": False},
                        "errors": [],
                        "skipped": [],
                    },
                },
            ),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = cli.main(["--repos", "QuantStrategyLab/ExampleRepo"])
        self.assertNotEqual(code, 0)

    def test_no_write_methods_on_github_request(self) -> None:
        seen: list[str] = []

        def tracker(token, method, path, payload=None):
            seen.append(method)
            if method != "GET":
                raise AssertionError(method)
            if "check-runs" in path:
                return {"check_runs": []}
            return []

        with (
            patch.object(source, "github_request", side_effect=tracker),
            patch.object(source, "github_list_all", return_value=[]),
        ):
            source.collect_trusted_events(
                token="token",
                allowlist=("QuantStrategyLab/ExampleRepo",),
            )
        self.assertTrue(all(m == "GET" for m in seen))


if __name__ == "__main__":
    unittest.main()
