import hashlib
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SYNC = _load_script("sync_lifecycle_artifacts")


class NestedLifecycleArtifactCompatibilityTests(unittest.TestCase):
    """Synthetic fixtures mirror the producer's path contract, without private data."""

    def setUp(self) -> None:
        self.config = SYNC.DOMAIN_CONFIGS["crypto"]

    def _files(self, *, profile="fixture_crypto", run_id="fixture-run", version=1):
        domain = self.config["domain"]
        payload = {
            "schema_version": "strategy_lifecycle.v1",
            "domain": domain,
            "strategy_profile": profile,
            "run_id": run_id,
            "param_set_id": "fixture-parameters",
            "params": {},
            "param_version": version,
            "observation_count": 2,
            "source_script": "tests.fixture",
            "sharpe_ratio": 0.8,
            "max_drawdown": -0.1,
            "cagr": 0.12,
            "volatility": 0.2,
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "computed_at": "2026-10-02T08:00:00+00:00",
        }
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        backtest_path = (
            f"data/lifecycle_store/backtest/{domain}/{profile}/"
            f"runs/{digest}/backtest_v{version}.json"
        )
        matrix_path = (
            f"external/{self.config['snapshot_repository']}/data/output/{profile}/"
            "portfolio_and_tracker_returns.csv"
        )
        matrix = (
            f"as_of,{profile},{self.config['benchmark_column']}\n"
            "2026-10-01,0.01,0.005\n2026-10-02,-0.002,-0.001\n"
        ).encode()
        return {backtest_path: json.dumps(payload).encode(), matrix_path: matrix}

    def _extract(self, root, files):
        archive_path = root / "fixture.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for path, raw in files.items():
                archive.writestr(path, raw)
        output = root / "version"
        manifest = SYNC.extract_validated_archive(archive_path, output, self.config)
        return output, manifest

    def _assert_rejected_by_archive_and_cache(self, files, *, reason=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(SYNC.LifecycleArtifactError, reason or ".*"):
                self._extract(root, files)
            self.assertFalse((root / "version").exists())
            cache = root / "cache"
            for name, raw in files.items():
                target = cache / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            manifest = {
                "profiles": ["fixture_crypto"],
                "sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
            }
            with self.assertRaisesRegex(SYNC.LifecycleArtifactError, reason or ".*"):
                SYNC.validate_stored_version(cache, manifest, self.config)

    def test_accepts_two_profile_nested_bundle_and_cache_manifest(self) -> None:
        files = self._files(profile="fixture_crypto_a", run_id="fixture-a")
        files.update(self._files(profile="fixture_crypto_b", run_id="fixture-b", version=0))
        with tempfile.TemporaryDirectory() as tmp:
            output, manifest = self._extract(Path(tmp), files)
            self.assertEqual(manifest["profiles"], ["fixture_crypto_a", "fixture_crypto_b"])
            self.assertEqual(manifest["file_count"], 4)
            self.assertEqual(set(manifest["sha256"]), set(files))
            SYNC.validate_stored_version(output, manifest, self.config)
            for name, raw in files.items():
                self.assertEqual((output / name).read_bytes(), raw)

    def test_accepts_exact_untrimmed_unicode_run_id_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, manifest = self._extract(Path(tmp), self._files(run_id="  合成-run  "))
            SYNC.validate_stored_version(output, manifest, self.config)

    def test_preserves_legacy_versions_and_timestamped_paths(self) -> None:
        for filename, version in (("backtest_v0.json", 0), ("backtest_v12_2026-10-02T08_00_00Z.json", 12)):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                files = self._files(version=version)
                nested = next(name for name in files if name.endswith(".json"))
                payload = json.loads(files.pop(nested))
                payload.pop("run_id")
                legacy = str(Path(nested).parents[2] / filename)
                files[legacy] = json.dumps(payload).encode()
                output, manifest = self._extract(Path(tmp), files)
                SYNC.validate_stored_version(output, manifest, self.config)

    def test_accepts_mixed_legacy_and_nested_distinct_run_identities(self) -> None:
        files = self._files()
        older = self._files(run_id="older-fixture-run")
        nested = next(name for name in older if name.endswith(".json"))
        files[str(Path(nested).parents[2] / "backtest_v1_older.json")] = older[nested]
        with tempfile.TemporaryDirectory() as tmp:
            output, manifest = self._extract(Path(tmp), files)
            SYNC.validate_stored_version(output, manifest, self.config)
            self.assertEqual(manifest["file_count"], 3)

    def test_preserves_unidentified_legacy_history_without_nested_files(self) -> None:
        files = self._files()
        nested = next(name for name in files if name.endswith(".json"))
        payload = json.loads(files.pop(nested))
        payload.pop("run_id")
        profile_dir = Path(nested).parents[2]
        files[str(profile_dir / "backtest_v1_2026-10-01T08-00-00Z.json")] = json.dumps(payload).encode()
        payload["computed_at"] = "2026-10-02T08:00:00+00:00"
        payload["cagr"] = 0.15
        files[str(profile_dir / "backtest_v1_2026-10-02T08-00-00Z.json")] = json.dumps(payload).encode()
        with tempfile.TemporaryDirectory() as tmp:
            output, manifest = self._extract(Path(tmp), files)
            SYNC.validate_stored_version(output, manifest, self.config)
            self.assertEqual(manifest["file_count"], 3)

    def test_rejects_unidentified_legacy_and_nested_same_profile_version_in_both_orders(self) -> None:
        for run_id in (None, "", "   ", 1, []):
            for legacy_first in (False, True):
                with self.subTest(run_id=run_id, legacy_first=legacy_first):
                    files = self._files()
                    nested = next(name for name in files if name.endswith(".json"))
                    payload = json.loads(files[nested])
                    if run_id is None:
                        payload.pop("run_id")
                    else:
                        payload["run_id"] = run_id
                    legacy = str(Path(nested).parents[2] / "backtest_v1_old.json")
                    files[legacy] = json.dumps(payload).encode()
                    if legacy_first:
                        files = dict(reversed(list(files.items())))
                    self._assert_rejected_by_archive_and_cache(
                        files, reason="ambiguous backtest identity"
                    )

    def test_accepts_unidentified_legacy_and_nested_different_versions(self) -> None:
        files = self._files()
        older = self._files(version=0)
        nested = next(name for name in older if name.endswith(".json"))
        payload = json.loads(older[nested])
        payload.pop("run_id")
        files[str(Path(nested).parents[2] / "backtest_v0_old.json")] = json.dumps(payload).encode()
        with tempfile.TemporaryDirectory() as tmp:
            output, manifest = self._extract(Path(tmp), files)
            SYNC.validate_stored_version(output, manifest, self.config)

    def test_nested_allowlist_applies_to_each_domain(self) -> None:
        for domain, config in SYNC.DOMAIN_CONFIGS.items():
            with self.subTest(domain=domain), tempfile.TemporaryDirectory() as tmp:
                self.config = config
                output, manifest = self._extract(Path(tmp), self._files())
                SYNC.validate_stored_version(output, manifest, config)

    def test_rejects_nested_payload_identity_and_contract_mismatches(self) -> None:
        for field, value in (
            ("run_id", None), ("run_id", ""), ("run_id", "   "), ("run_id", 1),
            ("run_id", ["fixture-run"]), ("run_id", "another-run"),
            ("domain", "us_equity"), ("strategy_profile", "other_profile"),
            ("param_version", 2), ("param_version", True), ("param_version", -1),
            ("schema_version", "wrong"), ("volatility", float("inf")),
        ):
            with self.subTest(field=field, value=value):
                files = self._files()
                name = next(name for name in files if name.endswith(".json"))
                payload = json.loads(files[name])
                payload[field] = value
                files[name] = json.dumps(payload).encode()
                self._assert_rejected_by_archive_and_cache(files)
        files = self._files()
        name = next(name for name in files if name.endswith(".json"))
        payload = json.loads(files[name])
        payload.pop("run_id")
        files[name] = json.dumps(payload).encode()
        self._assert_rejected_by_archive_and_cache(files)

    def test_rejects_noncanonical_nested_paths(self) -> None:
        files = self._files()
        original = next(name for name in files if name.endswith(".json"))
        digest = original.split("/")[-2]
        for replacement in (
            original.replace("/runs/", "/arbitrary/"),
            original.replace(digest, "a" * 63),
            original.replace(digest, digest.upper()),
            original.replace(digest, "g" * 64),
            original.replace(digest, "b" * 64),
            original.replace("backtest_v1.json", "backtest_v01.json"),
            original.replace("backtest_v1.json", "backtest_v1_extra.json"),
            original.replace("backtest_v1.json", "extra/backtest_v1.json"),
            original.replace("data/lifecycle_store", "data//lifecycle_store"),
            original.replace("data/lifecycle_store", "data/./lifecycle_store"),
        ):
            with self.subTest(path=replacement):
                changed = dict(files)
                changed[replacement] = changed.pop(original)
                self._assert_rejected_by_archive_and_cache(changed)

    def test_rejects_legacy_filename_version_mismatch(self) -> None:
        files = self._files()
        nested = next(name for name in files if name.endswith(".json"))
        files[str(Path(nested).parents[2] / "backtest_v2_old.json")] = files.pop(nested)
        self._assert_rejected_by_archive_and_cache(files)

    def test_rejects_ambiguous_legacy_and_nested_run_identities(self) -> None:
        for change_payload in (False, True):
            for legacy_first in (False, True):
                with self.subTest(change_payload=change_payload, legacy_first=legacy_first):
                    files = self._files()
                    nested = next(name for name in files if name.endswith(".json"))
                    payload = json.loads(files[nested])
                    if change_payload:
                        payload["cagr"] = 0.15
                    legacy = str(Path(nested).parents[2] / "backtest_v1_old.json")
                    files[legacy] = json.dumps(payload, indent=2).encode()
                    if legacy_first:
                        files = dict(reversed(list(files.items())))
                    reason = "conflicting" if change_payload else "duplicate"
                    self._assert_rejected_by_archive_and_cache(
                        files, reason=f"{reason} backtest identity"
                    )

    def test_rejects_nested_extra_file_duplicate_traversal_and_symlink(self) -> None:
        for kind in ("extra", "duplicate", "traversal", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                files = self._files()
                nested = next(name for name in files if name.endswith(".json"))
                archive_path = root / "fixture.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for name, raw in files.items():
                        archive.writestr(name, raw)
                    if kind == "extra":
                        archive.writestr(str(Path(nested).parent / "extra.json"), "{}")
                    elif kind == "duplicate":
                        with self.assertWarns(UserWarning):
                            archive.writestr(nested, files[nested])
                    elif kind == "traversal":
                        archive.writestr("data/lifecycle_store/../escape", "bad")
                    else:
                        info = zipfile.ZipInfo(str(Path(nested).parent / "backtest_v2.json"))
                        info.create_system = 3
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(info, "/tmp/outside")
                with self.assertRaises(SYNC.LifecycleArtifactError):
                    SYNC.extract_validated_archive(archive_path, root / "version", self.config)
                self.assertFalse((root / "version").exists())

    def test_rejects_cached_nested_tampering_extra_files_and_symlinks(self) -> None:
        for kind in ("hash", "extra", "symlink", "parent_symlink", "profiles"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output, manifest = self._extract(root, self._files())
                name = next(name for name in manifest["sha256"] if name.endswith(".json"))
                target = output / name
                if kind == "hash":
                    target.write_bytes(b"{}")
                elif kind == "extra":
                    (target.parent / "extra.json").write_bytes(b"{}")
                elif kind == "symlink":
                    target.unlink()
                    target.symlink_to(root / "outside")
                elif kind == "parent_symlink":
                    outside = root / "outside"
                    target.parent.rename(outside)
                    target.parent.symlink_to(outside, target_is_directory=True)
                else:
                    manifest["profiles"] = ["wrong_profile"]
                with self.assertRaises(SYNC.LifecycleArtifactError):
                    SYNC.validate_stored_version(output, manifest, self.config)


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

    def test_sync_domain_classifies_artifact_validation_stage_without_leaking_detail(self) -> None:
        selected = {
            "id": 12,
            "name": "lifecycle-preflight-34-1",
            "created_at": self.now.isoformat(),
            "_created_at": self.now.isoformat(),
            "_trusted_run": {"id": 34, "head_sha": "a" * 40},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_detail = "local/path/value must not appear"
            with (
                patch.object(SYNC, "_run_gh", return_value={"artifacts": []}),
                patch.object(SYNC, "select_trusted_artifact", return_value=selected),
                patch.object(
                    SYNC,
                    "_load_or_download_version",
                    side_effect=SYNC.LifecycleArtifactError(secret_detail),
                ),
                self.assertRaises(SYNC.LifecycleArtifactError) as context,
            ):
                SYNC._sync_domain(
                    self.config,
                    artifacts_root=root / "artifacts",
                    projects_root=root / "projects",
                    lifecycle_root=root / "lifecycle",
                    max_age=timedelta(days=7),
                    now=self.now,
                )

            status = SYNC._domain_error_status(context.exception)
            self.assertEqual(status["code"], "artifact_invalid")
            self.assertEqual(status["reason_code"], "artifact_version_validation_failed")
            self.assertNotIn(secret_detail, json.dumps(status))

    def test_sync_domain_distinguishes_cached_version_and_activation_failures(self) -> None:
        selected = {
            "id": 12,
            "name": "lifecycle-preflight-34-1",
            "created_at": self.now.isoformat(),
            "_created_at": self.now.isoformat(),
            "_trusted_run": {"id": 34, "head_sha": "a" * 40},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cached_version = root / "artifacts" / "versions" / self.config["domain"] / "12"
            cached_version.mkdir(parents=True)
            with (
                patch.object(SYNC, "_run_gh", return_value={"artifacts": []}),
                patch.object(SYNC, "select_trusted_artifact", return_value=selected),
                patch.object(
                    SYNC,
                    "_load_or_download_version",
                    side_effect=SYNC.LifecycleArtifactError("synthetic cache mismatch"),
                ),
                self.assertRaises(SYNC.LifecycleArtifactError) as context,
            ):
                SYNC._sync_domain(
                    self.config,
                    artifacts_root=root / "artifacts",
                    projects_root=root / "projects",
                    lifecycle_root=root / "lifecycle",
                    max_age=timedelta(days=7),
                    now=self.now,
                )
            self.assertEqual(
                SYNC._domain_error_status(context.exception)["reason_code"],
                "stored_artifact_validation_failed",
            )

            with (
                patch.object(SYNC, "_run_gh", return_value={"artifacts": []}),
                patch.object(SYNC, "select_trusted_artifact", return_value=selected),
                patch.object(
                    SYNC,
                    "_load_or_download_version",
                    return_value=(root / "version", {"profiles": []}),
                ),
                patch.object(
                    SYNC,
                    "activate_version",
                    side_effect=SYNC.LifecycleArtifactError("synthetic activation failure"),
                ),
                self.assertRaises(SYNC.LifecycleArtifactError) as context,
            ):
                SYNC._sync_domain(
                    self.config,
                    artifacts_root=root / "fresh-artifacts",
                    projects_root=root / "projects",
                    lifecycle_root=root / "lifecycle",
                    max_age=timedelta(days=7),
                    now=self.now,
                )
            self.assertEqual(
                SYNC._domain_error_status(context.exception)["reason_code"],
                "artifact_activation_failed",
            )

    def test_unexpected_domain_error_remains_safe_and_generic(self) -> None:
        status = SYNC._domain_error_status(ValueError("secret path and content"))
        self.assertEqual(status["code"], "artifact_sync_unexpected")
        self.assertEqual(status["reason_code"], "artifact_sync_unexpected")
        self.assertNotIn("secret path", json.dumps(status))

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
