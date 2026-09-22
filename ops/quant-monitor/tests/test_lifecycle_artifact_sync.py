import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SYNC = _load_script("sync_lifecycle_artifacts")


class LifecycleArtifactSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SYNC.DOMAIN_CONFIGS["us_equity"]
        self.now = datetime(2026, 7, 30, 7, 0, tzinfo=timezone.utc)

    def _artifact(self, artifact_id: int, run_id: int, created_at: datetime):
        return {
            "id": artifact_id,
            "name": f"lifecycle-preflight-{run_id}-1",
            "expired": False,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "workflow_run": {"id": run_id},
        }

    def _run(self, run_id: int, **overrides):
        payload = {
            "id": run_id,
            "status": "completed",
            "event": "schedule",
            "head_branch": "main",
            "path": ".github/workflows/drift-check.yml",
            "head_sha": "a" * 40,
            "head_repository": {"full_name": self.config["repository"]},
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _jobs(conclusion: str = "success"):
        return {
            "jobs": [
                {
                    "name": "preflight_backtests",
                    "conclusion": conclusion,
                    "status": "completed",
                }
            ]
        }

    def test_selects_latest_trusted_preflight_artifact(self) -> None:
        older = self._artifact(1, 11, self.now - timedelta(days=2))
        latest = self._artifact(2, 22, self.now - timedelta(hours=1))
        runs = {11: self._run(11), 22: self._run(22)}

        selected = SYNC.select_trusted_artifact(
            self.config,
            [older, latest],
            load_run=lambda run_id: runs[run_id],
            load_jobs=lambda _run_id: self._jobs(),
            now=self.now,
            max_age=timedelta(days=7),
        )

        self.assertEqual(selected["id"], 2)

    def test_rejects_untrusted_or_stale_artifacts(self) -> None:
        untrusted = self._artifact(2, 22, self.now - timedelta(hours=1))
        stale = self._artifact(1, 11, self.now - timedelta(days=8))
        runs = {
            22: self._run(22, event="pull_request"),
            11: self._run(11),
        }

        with self.assertRaisesRegex(
            SYNC.LifecycleArtifactError,
            "no trusted lifecycle artifact",
        ):
            SYNC.select_trusted_artifact(
                self.config,
                [untrusted, stale],
                load_run=lambda run_id: runs[run_id],
                load_jobs=lambda _run_id: self._jobs(),
                now=self.now,
                max_age=timedelta(days=7),
            )

    def test_requires_successful_preflight_job(self) -> None:
        artifact = self._artifact(2, 22, self.now - timedelta(hours=1))

        with self.assertRaises(SYNC.LifecycleArtifactError):
            SYNC.select_trusted_artifact(
                self.config,
                [artifact],
                load_run=lambda run_id: self._run(run_id),
                load_jobs=lambda _run_id: self._jobs("failure"),
                now=self.now,
                max_age=timedelta(days=7),
            )

    def _write_valid_archive(
        self,
        path: Path,
        *,
        profile: str = "global_etf_rotation",
        matrix_profile: str | None = None,
    ) -> None:
        matrix_profile = matrix_profile or profile
        backtest = {
            "strategy_profile": profile,
            "domain": "us_equity",
            "param_set_id": f"{profile}_wf",
            "params": {},
            "param_version": 1,
            "sharpe_ratio": 0.8,
            "max_drawdown": -0.1,
            "cagr": 0.12,
            "volatility": 0.2,
            "observation_count": 252,
            "start_date": "2025-07-29",
            "end_date": "2026-07-29",
            "computed_at": "2026-07-29T08:00:00+00:00",
            "source_script": "tests.fixture",
            "schema_version": "strategy_lifecycle.v1",
        }
        with zipfile.ZipFile(path, "w") as bundle:
            bundle.writestr(
                f"data/lifecycle_store/backtest/us_equity/{profile}/backtest_v1.json",
                json.dumps(backtest),
            )
            bundle.writestr(
                "external/UsEquitySnapshotPipelines/data/output/"
                f"{profile}/portfolio_and_tracker_returns.csv",
                f"as_of,{matrix_profile},buy_hold_SPY\n"
                "2026-07-28,0.01,0.005\n"
                "2026-07-29,-0.002,-0.001\n",
            )

    def test_extracts_validated_allowlisted_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "artifact.zip"
            output = root / "version"
            self._write_valid_archive(archive)

            result = SYNC.extract_validated_archive(archive, output, self.config)

            self.assertEqual(result["profiles"], ["global_etf_rotation"])
            self.assertTrue(
                (
                    output
                    / "external/UsEquitySnapshotPipelines/data/output/global_etf_rotation"
                    / "portfolio_and_tracker_returns.csv"
                ).is_file()
            )

    def test_allows_sparse_benchmark_values_with_enough_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "artifact.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(
                    "data/lifecycle_store/backtest/us_equity/global_etf_rotation/"
                    "backtest_v1.json",
                    json.dumps(
                        {
                            "strategy_profile": "global_etf_rotation",
                            "domain": "us_equity",
                            "param_set_id": "global_etf_rotation_wf",
                            "params": {},
                            "param_version": 1,
                            "sharpe_ratio": 0.8,
                            "max_drawdown": -0.1,
                            "cagr": 0.12,
                            "volatility": 0.2,
                            "observation_count": 252,
                            "start_date": "2025-07-29",
                            "end_date": "2026-07-29",
                            "computed_at": "2026-07-29T08:00:00+00:00",
                            "source_script": "tests.fixture",
                            "schema_version": "strategy_lifecycle.v1",
                        }
                    ),
                )
                matrix_path = (
                    "external/UsEquitySnapshotPipelines/data/output/"
                    "global_etf_rotation/portfolio_and_tracker_returns.csv"
                )
                bundle.writestr(
                    matrix_path,
                    "as_of,global_etf_rotation,buy_hold_SPY\n"
                    "2026-07-27,0.003,0.002\n"
                    "2026-07-28,0.01,0.005\n"
                    "2026-07-29,-0.002,\n",
                )

            result = SYNC.extract_validated_archive(
                archive,
                root / "version",
                self.config,
            )

            self.assertEqual(result["profiles"], ["global_etf_rotation"])

    def test_rejects_wrong_domain_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "artifact.zip"
            self._write_valid_archive(archive)
            replacement = root / "wrong-benchmark.zip"
            with (
                zipfile.ZipFile(archive) as source,
                zipfile.ZipFile(replacement, "w") as target,
            ):
                for member in source.infolist():
                    raw = source.read(member)
                    if member.filename.endswith("portfolio_and_tracker_returns.csv"):
                        raw = raw.replace(b"buy_hold_SPY", b"buy_hold_BTC")
                    target.writestr(member, raw)

            with self.assertRaises(SYNC.LifecycleArtifactError):
                SYNC.extract_validated_archive(
                    replacement,
                    root / "version",
                    self.config,
                )

    def test_rejects_traversal_and_symlink_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, configure in (
                ("traversal", lambda bundle: bundle.writestr("../escape", "bad")),
                ("symlink", self._write_symlink_member),
            ):
                archive = root / f"{name}.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    configure(bundle)
                with self.assertRaises(SYNC.LifecycleArtifactError):
                    SYNC.extract_validated_archive(
                        archive,
                        root / f"{name}-out",
                        self.config,
                    )

    @staticmethod
    def _write_symlink_member(bundle: zipfile.ZipFile) -> None:
        info = zipfile.ZipInfo(
            "external/UsEquitySnapshotPipelines/data/output/example/"
            "portfolio_and_tracker_returns.csv"
        )
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(info, "/tmp/outside")

    def test_rejects_profile_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "artifact.zip"
            self._write_valid_archive(archive, matrix_profile="wrong_profile")

            with self.assertRaisesRegex(
                SYNC.LifecycleArtifactError,
                "profile",
            ):
                SYNC.extract_validated_archive(
                    archive,
                    root / "version",
                    self.config,
                )

    def test_atomically_switches_consumer_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "artifact.zip"
            version = root / "versions" / "us_equity" / "2"
            self._write_valid_archive(archive)
            SYNC.extract_validated_archive(archive, version, self.config)
            projects_root = root / "projects"
            lifecycle_root = root / "store"

            SYNC.activate_version(
                version,
                self.config,
                projects_root=projects_root,
                lifecycle_root=lifecycle_root,
            )

            output_link = (
                projects_root
                / "UsEquitySnapshotPipelines"
                / "data"
                / "output"
            )
            backtest_link = lifecycle_root / "backtest" / "us_equity"
            self.assertTrue(output_link.is_symlink())
            self.assertTrue(backtest_link.is_symlink())
            self.assertTrue(output_link.resolve().is_dir())
            self.assertTrue(backtest_link.resolve().is_dir())
            self.assertNotIn("..", os.readlink(output_link))

    def test_detects_tampered_stored_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "artifact.zip"
            version = root / "versions" / "us_equity" / "2"
            self._write_valid_archive(archive)
            validation = SYNC.extract_validated_archive(
                archive,
                version,
                self.config,
            )
            manifest = {
                "profiles": validation["profiles"],
                "sha256": validation["sha256"],
            }

            SYNC.validate_stored_version(version, manifest, self.config)
            matrix = next(version.rglob("portfolio_and_tracker_returns.csv"))
            matrix.write_text("tampered", encoding="utf-8")

            with self.assertRaises(SYNC.LifecycleArtifactError):
                SYNC.validate_stored_version(version, manifest, self.config)

    def test_classify_gh_api_rate_limit_without_leaking_stderr(self) -> None:
        secret = "ghs_this_is_not_a_real_token_leak_check"
        classified = SYNC.classify_gh_api_failure(
            "gh: HTTP 403: API rate limit exceeded for user ID 1 "
            f"Authorization: token {secret}\n"
            "X-RateLimit-Reset: 1695398400\n"
        )
        self.assertEqual(classified["code"], "github_api_rate_limit")
        self.assertEqual(classified["reason_code"], "github_api_rate_limit")
        self.assertEqual(classified["http_status"], 403)
        self.assertEqual(
            classified["rate_limit_reset_at"],
            "2023-09-22T16:00:00+00:00",
        )
        self.assertNotIn(secret, json.dumps(classified))
        self.assertNotIn("Authorization", json.dumps(classified))

    def test_run_gh_skips_short_retry_on_rate_limit(self) -> None:
        calls: list[int] = []
        sleeps: list[float] = []

        class Result:
            def __init__(self) -> None:
                self.returncode = 1
                self.stdout = b""
                self.stderr = (
                    b"gh: HTTP 403: API rate limit exceeded\n"
                    b"X-RateLimit-Reset: 1695398400\n"
                )

        def fake_run(*_args, **_kwargs):
            calls.append(1)
            return Result()

        original_run = SYNC.subprocess.run
        original_sleep = SYNC.time.sleep
        SYNC.subprocess.run = fake_run  # type: ignore[assignment]
        SYNC.time.sleep = sleeps.append  # type: ignore[assignment]
        try:
            with self.assertRaises(SYNC.LifecycleArtifactError) as ctx:
                SYNC._run_gh(["/rate_limit"])
        finally:
            SYNC.subprocess.run = original_run  # type: ignore[assignment]
            SYNC.time.sleep = original_sleep  # type: ignore[assignment]

        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(ctx.exception.code, "github_api_rate_limit")
        self.assertEqual(ctx.exception.http_status, 403)

    def test_run_gh_retries_transient_failures(self) -> None:
        calls: list[int] = []
        sleeps: list[float] = []

        class Result:
            def __init__(self) -> None:
                self.returncode = 1
                self.stdout = "{}"
                self.stderr = "gh: HTTP 502: Bad Gateway"

        def fake_run(*_args, **_kwargs):
            calls.append(1)
            return Result()

        original_run = SYNC.subprocess.run
        original_sleep = SYNC.time.sleep
        SYNC.subprocess.run = fake_run  # type: ignore[assignment]
        SYNC.time.sleep = sleeps.append  # type: ignore[assignment]
        try:
            with self.assertRaises(SYNC.LifecycleArtifactError) as ctx:
                SYNC._run_gh(["/repos/example"])
        finally:
            SYNC.subprocess.run = original_run  # type: ignore[assignment]
            SYNC.time.sleep = original_sleep  # type: ignore[assignment]

        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(ctx.exception.code, "github_api_unavailable")

    def test_shared_upstream_annotates_multi_domain_github_failures(self) -> None:
        statuses = {
            "cn_equity": {
                "status": "error",
                "code": "github_api_rate_limit",
                "error_type": "LifecycleArtifactError",
                "reason_code": "github_api_rate_limit",
                "http_status": 403,
                "rate_limit_reset_at": "2023-09-22T16:00:00+00:00",
            },
            "hk_equity": {
                "status": "error",
                "code": "github_api_rate_limit",
                "error_type": "LifecycleArtifactError",
                "reason_code": "github_api_rate_limit",
                "shared_root_cause": "github_api_rate_limit",
            },
            "us_equity": {
                "status": "error",
                "code": "github_api_rate_limit",
                "error_type": "LifecycleArtifactError",
                "reason_code": "github_api_rate_limit",
                "shared_root_cause": "github_api_rate_limit",
            },
            "crypto": {
                "status": "error",
                "code": "github_api_rate_limit",
                "error_type": "LifecycleArtifactError",
                "reason_code": "github_api_rate_limit",
                "shared_root_cause": "github_api_rate_limit",
            },
        }
        shared = SYNC._build_shared_upstream(statuses)
        self.assertIsNotNone(shared)
        assert shared is not None
        self.assertEqual(shared["kind"], "github_api")
        self.assertEqual(shared["reason_code"], "github_api_rate_limit")
        self.assertEqual(
            shared["affected_domains"],
            ["cn_equity", "crypto", "hk_equity", "us_equity"],
        )
        SYNC._annotate_shared_root_cause(statuses, shared)
        for domain in statuses:
            self.assertEqual(statuses[domain]["shared_root_cause"], "github_api_rate_limit")


if __name__ == "__main__":
    unittest.main()
