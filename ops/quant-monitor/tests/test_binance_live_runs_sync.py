import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from quant_platform_kit.strategy_lifecycle.live_equity import live_run_records_to_return_series
from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SYNC = _load_script("sync_binance_live_runs")


def _archive(record, *, member_name="lifecycle-run.json"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, json.dumps(record, sort_keys=True))
    return output.getvalue()


def _interval(*, start_at, end_at, equity, flow="0"):
    return {
        "account_scope_sha256": "a" * 64,
        "start_at": start_at,
        "end_at": end_at,
        "end_equity_usdt": equity,
        "net_external_cash_flow": flow,
        "currency": "USDT",
        "valuation_basis": "checkpoint_quantities_sampled_prices",
    }


def _record(*, recorded_at="2026-09-14T00:00:00+00:00", status="ok", equity=100.0, interval=None):
    return {
        "schema_version": "strategy_lifecycle.v1",
        "strategy_profile": "crypto_live_pool_rotation",
        "domain": "crypto",
        "recorded_at": recorded_at,
        "record_kind": "execution",
        "lifecycle_stream_id": "binance",
        "execution_result": {
            "platform": "binance",
            "status": status,
            "external_cash_flow": None,
            "external_cash_flow_interval": interval,
            "total_equity_usdt": equity,
            "trend_equity_usdt": equity,
            "degraded_mode_level": None,
        },
    }


class FakeStore:
    def __init__(self):
        self.records = {}

    def save_live_run_record(self, profile, domain, payload, *, stream_id=""):
        key = (domain, profile, stream_id, payload["recorded_at"])
        self.records[key] = dict(payload)

    def list_live_run_records(self, domain, *, strategy_profile=None, stream_id=None):
        return list(self.records.values())


class BinanceLiveRunsSyncTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 14, 1, tzinfo=timezone.utc)
        self.start = datetime(2026, 9, 13, tzinfo=timezone.utc)
        self.run = {
            "id": 123,
            "run_attempt": 2,
            "status": "completed",
            "conclusion": "success",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "path": ".github/workflows/main.yml",
            "name": "Runtime · strategy",
            "display_title": "Runtime · strategy",
            "created_at": "2026-09-13T23:00:00Z",
            "updated_at": "2026-09-14T00:01:00Z",
            "head_repository": {"full_name": "QuantStrategyLab/BinancePlatform"},
        }
        self.artifact = {
            "id": 456,
            "name": "binance-live-run-123-1",
            "expired": False,
            "size_in_bytes": 512,
            "workflow_run": {"id": 123},
        }

    def _gh(self, archive):
        def api(path, *, binary=False):
            if "/actions/workflows/main.yml/runs?" in path:
                return {"workflow_runs": [self.run]}
            if "/actions/runs/123/artifacts?" in path:
                return {"artifacts": [self.artifact, {**self.artifact, "id": 457, "name": "binance-live-run-123-2"}]}
            if path.endswith("/actions/artifacts/456/zip") or path.endswith("/actions/artifacts/457/zip"):
                return archive
            raise AssertionError(path)

        return api

    def _status_path(self, root):
        path = root / "data" / "lifecycle-artifacts" / "status.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
                "schema_version": "quant_monitor_lifecycle_artifact_status.v1",
                "as_of": "2026-09-14T00:30:00+00:00",
                "domains": {
                    "cn_equity": {"status": "ready", "artifact_id": 1, "run_id": 2, "head_sha": "a" * 40, "profiles": ["cn"]},
                    "hk_equity": {"status": "ready", "artifact_id": 3, "run_id": 4, "head_sha": "b" * 40, "profiles": ["hk"]},
                    "us_equity": {"status": "ready", "artifact_id": 5, "run_id": 6, "head_sha": "c" * 40, "profiles": ["us"]},
                    "crypto": {"status": "ready", "artifact_id": 7, "run_id": 8, "head_sha": "d" * 40, "profiles": ["crypto"]},
                },
                "ok": True,
            }),
            encoding="utf-8",
        )
        return path

    def test_imports_each_run_attempt_and_preserves_recorded_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = self._status_path(root)
            store = FakeStore()
            archives = {
                456: _archive(_record(recorded_at="2026-09-13T23:30:00+00:00")),
                457: _archive(_record(recorded_at="2026-09-14T00:00:00+00:00")),
            }
            api = self._gh(archives[456])
            original = api
            def gh(path, *, binary=False):
                if binary:
                    return archives[456 if path.endswith("/456/zip") else 457]
                return original(path, binary=binary)
            result = SYNC.sync_live_runs(
                start_at=self.start,
                now=self.now,
                gh_json=gh,
                gh_bytes=lambda path: gh(path, binary=True),
                store=store,
                lifecycle_status_path=status_path,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(len(store.records), 2)
            payload = next(iter(store.records.values()))
            self.assertIn(payload["recorded_at"], {"2026-09-13T23:30:00+00:00", "2026-09-14T00:00:00+00:00"})
            self.assertEqual(result["imported_records"], 2)

    def test_repeated_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = self._status_path(root)
            store = FakeStore()
            archive = _archive(_record())
            kwargs = {
                "start_at": self.start,
                "now": self.now,
                "gh_json": self._gh(archive),
                "gh_bytes": lambda path: archive,
                "store": store,
                "lifecycle_status_path": status_path,
            }
            SYNC.sync_live_runs(**kwargs)
            SYNC.sync_live_runs(**kwargs)
            self.assertEqual(len(store.records), 1)

    def test_missing_artifact_writes_crypto_barrier_without_touching_other_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = self._status_path(root)

            def api(path, *, binary=False):
                if "/actions/workflows/main.yml/runs?" in path:
                    return {"workflow_runs": [self.run]}
                if "/actions/runs/123/artifacts?" in path:
                    return {"artifacts": []}
                raise AssertionError(path)

            result = SYNC.sync_live_runs(
                start_at=self.start,
                now=self.now,
                gh_json=api,
                gh_bytes=lambda path: b"",
                store=FakeStore(),
                lifecycle_status_path=status_path,
            )
            self.assertEqual(result["status"], "ready_with_gaps")
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["domains"]["cn_equity"]["status"], "ready")
            self.assertEqual(payload["domains"]["crypto"]["status"], "ready")
            self.assertEqual(result["unknown_days"], ["2026-09-13"])

    def test_missing_run_is_a_gap_and_later_completed_run_can_be_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = self._status_path(root)
            later_run = {
                **self.run,
                "id": 124,
                "run_attempt": 1,
                "created_at": "2026-09-14T00:30:00Z",
                "updated_at": "2026-09-14T00:31:00Z",
            }
            later_artifact = {
                "id": 458,
                "name": "binance-live-run-124-1",
                "expired": False,
                "size_in_bytes": 512,
                "workflow_run": {"id": 124},
            }
            store = FakeStore()
            archive = _archive(_record(recorded_at="2026-09-14T00:30:00Z"))
            runs = [self.run]

            def api(path, *, binary=False):
                if "/actions/workflows/main.yml/runs?" in path:
                    return {"workflow_runs": list(runs)}
                if "/actions/runs/123/artifacts?" in path:
                    return {"artifacts": []}
                if "/actions/runs/124/artifacts?" in path:
                    return {"artifacts": [later_artifact]}
                raise AssertionError(path)

            first = SYNC.sync_live_runs(
                start_at=self.start,
                now=self.now,
                gh_json=api,
                gh_bytes=lambda path: b"",
                store=store,
                lifecycle_status_path=status_path,
            )
            self.assertEqual(first["status"], "ready_with_gaps")
            self.assertEqual(len(store.records), 1)
            self.assertEqual(next(iter(store.records.values()))["execution_result"]["status"], "unknown")

            runs.append(later_run)
            second = SYNC.sync_live_runs(
                start_at=self.start,
                now=self.now,
                gh_json=api,
                gh_bytes=lambda path: archive,
                store=store,
                lifecycle_status_path=status_path,
            )
            self.assertEqual(second["status"], "ready_with_gaps")
            self.assertEqual(second["imported_records"], 1)
            self.assertEqual(len(store.records), 2)
            records = store.list_live_run_records(
                "crypto",
                strategy_profile="crypto_live_pool_rotation",
                stream_id="binance",
            )
            self.assertTrue(live_run_records_to_return_series(records).empty)

    def test_in_progress_run_is_not_persisted_as_a_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = self._status_path(root)
            pending_run = {**self.run, "status": "in_progress", "conclusion": None}

            def api(path, *, binary=False):
                if "/actions/workflows/main.yml/runs?" in path:
                    return {"workflow_runs": [pending_run]}
                raise AssertionError(path)

            store = FakeStore()
            result = SYNC.sync_live_runs(
                start_at=self.start,
                now=self.now,
                gh_json=api,
                gh_bytes=lambda path: b"",
                store=store,
                lifecycle_status_path=status_path,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(len(store.records), 0)

    def test_imported_attempt_is_reused_without_redownloading_expired_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = self._status_path(root)
            store = FakeStore()
            archive = _archive(_record())
            one_run = {**self.run, "run_attempt": 1}
            artifact_calls = 0

            def first_api(path, *, binary=False):
                nonlocal artifact_calls
                if "/actions/workflows/main.yml/runs?" in path:
                    return {"workflow_runs": [one_run]}
                if "/actions/runs/123/artifacts?" in path:
                    artifact_calls += 1
                    return {"artifacts": [self.artifact]}
                raise AssertionError(path)

            SYNC.sync_live_runs(
                start_at=self.start,
                now=self.now,
                gh_json=first_api,
                gh_bytes=lambda path: archive,
                store=store,
                lifecycle_status_path=status_path,
            )
            self.assertGreater(artifact_calls, 0)

            def second_api(path, *, binary=False):
                if "/actions/workflows/main.yml/runs?" in path:
                    return {"workflow_runs": [one_run]}
                if "/actions/runs/123/artifacts?" in path:
                    raise AssertionError("cached attempts must not list expired artifacts")
                raise AssertionError(path)

            result = SYNC.sync_live_runs(
                start_at=self.start,
                now=self.now,
                gh_json=second_api,
                gh_bytes=lambda path: (_ for _ in ()).throw(AssertionError("must not download")),
                store=store,
                lifecycle_status_path=status_path,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["imported_records"], 0)

    def test_conflicting_existing_identity_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = self._status_path(root)
            store = FakeStore()
            existing = _record(recorded_at="2026-09-14T00:00:00+00:00", equity=99.0)
            store.save_live_run_record("crypto_live_pool_rotation", "crypto", existing, stream_id="binance")
            one_run = {**self.run, "run_attempt": 1}

            def api(path, *, binary=False):
                if "/actions/workflows/main.yml/runs?" in path:
                    return {"workflow_runs": [one_run]}
                if "/actions/runs/123/artifacts?" in path:
                    return {"artifacts": [self.artifact]}
                raise AssertionError(path)

            with self.assertRaises(SYNC.BinanceLiveRunsError) as raised:
                SYNC.sync_live_runs(
                    start_at=self.start,
                    now=self.now,
                    gh_json=api,
                    gh_bytes=lambda path: _archive(_record(equity=100.0)),
                    store=store,
                    lifecycle_status_path=status_path,
                )
            self.assertEqual(raised.exception.reason, "existing_record_conflict")
            self.assertEqual(next(iter(store.records.values()))["execution_result"]["total_equity_usdt"], 99.0)

    def test_unknown_record_is_not_written_as_continuous_performance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = self._status_path(root)
            result = SYNC.sync_live_runs(
                start_at=self.start,
                now=self.now,
                gh_json=self._gh(_archive(_record(status="error", equity=None))),
                gh_bytes=lambda path: self._gh(_archive(_record(status="error", equity=None)))(path, binary=True),
                store=FakeStore(),
                lifecycle_status_path=status_path,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["imported_records"], 1)
            self.assertEqual(result["unknown_days"], [])

    def test_disabled_configuration_does_not_call_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            called = False

            def remote(_path, *, binary=False):
                nonlocal called
                called = True
                raise AssertionError("remote must not be called")

            result = SYNC.run_from_environment(
                env={"QUANT_MONITOR_ROOT": tmp},
                gh_json=remote,
                gh_bytes=remote,
            )
            self.assertEqual(result["status"], "disabled")
            self.assertFalse(called)

    def test_real_performance_store_and_return_collector_consume_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = self._status_path(root)
            archives = {
                456: _archive(_record(
                    recorded_at="2026-09-13T23:00:00+00:00",
                    equity=100.0,
                    interval=_interval(
                        start_at="2026-09-13T22:00:00Z",
                        end_at="2026-09-13T23:00:00Z",
                        equity=100.0,
                    ),
                )),
                457: _archive(_record(
                    recorded_at="2026-09-14T00:00:00+00:00",
                    equity=110.0,
                    interval=_interval(
                        start_at="2026-09-13T23:00:00Z",
                        end_at="2026-09-14T00:00:00Z",
                        equity=110.0,
                        flow="10",
                    ),
                )),
            }
            api = self._gh(archives[456])
            store = PerformanceStore(local_root=root / "store")
            result = SYNC.sync_live_runs(
                start_at=self.start,
                now=self.now,
                gh_json=api,
                gh_bytes=lambda path: archives[456 if path.endswith("/456/zip") else 457],
                store=store,
                lifecycle_status_path=status_path,
            )
            self.assertEqual(result["status"], "ready")
            records = store.list_live_run_records(
                "crypto",
                strategy_profile="crypto_live_pool_rotation",
                stream_id="binance",
            )
            series = live_run_records_to_return_series(records)
            self.assertEqual(len(records), 2)
            self.assertEqual(len(series), 1)
            self.assertAlmostEqual(float(series.iloc[0]), 0.0)


if __name__ == "__main__":
    unittest.main()
