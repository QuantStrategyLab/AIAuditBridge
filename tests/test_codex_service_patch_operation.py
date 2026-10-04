"""Fixed-target inspection uses synthetic files and mocked metadata commands only."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import manage_codex_audit_service_patch as operation


class CodexServiceMaintenanceTests(unittest.TestCase):
    """Synthetic two-file maintenance; no systemd, provider or real HTTP calls."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = {name: self.root / name / "source.py" for name in ("gateway", "codex_adapter")}
        self.targets = {name: self.root / "checkout" / name / "source.py" for name in self.files}
        self.original = {name: f"old {name}\n".encode() for name in self.files}
        self.updated = {name: f"new {name}\n".encode() for name in self.files}
        self.metadata = {}
        for index, name in enumerate(self.files):
            self.files[name].parent.mkdir()
            self.targets[name].parent.mkdir(parents=True)
            self.files[name].write_bytes(self.original[name])
            self.targets[name].write_bytes(self.updated[name])
            self.files[name].chmod(0o640 + index * 4)
            info = self.files[name].stat()
            self.metadata[name] = (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
        self.protected = {self.root / name: f"PRIVATE_{name}".encode() for name in (
            "configuration.env", "token", "unit.service", "nginx.conf", "jobs.json", "quota.json",
        )}
        for path, data in self.protected.items():
            path.write_bytes(data)
        self.backup = self.root / "exclusive-backup"
        self.unit = {"identity_verified": True, "known_working_directory": True, "load_state": "loaded",
                     "active_state": "active", "sub_state": "running", "main_pid": 123, "user": "ubuntu"}
        self.actions = []
        self.process = {"snapshot_coherent": True, "entrypoint_matches_gateway": True,
                        "initial_environment_known_cli_choice": True}
        self.counts = {"queued": 0, "running": 0, "unknown": 10, "complete": False,
                       "truncated": True, "admission_closed": False, "includes_sync_requests": False}
        self.production_preimages = dict(operation.PREIMAGE_SHA256)
        self.production_targets = dict(operation.TARGET_SHA256)
        for attr, value in (
            ("SOURCE_FILES", self.files), ("TARGET_FILES", self.targets), ("BACKUP_DIRECTORY", self.backup),
            ("PREIMAGE_SHA256", {n: hashlib.sha256(d).hexdigest() for n, d in self.original.items()}),
            ("TARGET_SHA256", {n: hashlib.sha256(d).hexdigest() for n, d in self.updated.items()}),
        ):
            mock = patch.object(operation, attr, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.real_unit_action = operation._unit_action
        self.real_health = operation._local_health
        for attr, kwargs in (
            ("unit_metadata", {"side_effect": lambda: dict(self.unit)}),
            ("service_process_metadata", {"side_effect": lambda unit: dict(self.process)}),
            ("job_counts", {"return_value": self.counts}),
            ("_unit_action", {"side_effect": self.action}),
            ("_local_health", {"return_value": True}),
        ):
            mock = patch.object(operation, attr, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def action(self, action: str) -> bool:
        self.actions.append(action)
        if action == "stop":
            self.unit.update(active_state="inactive", sub_state="dead", main_pid=0)
        else:
            self.unit.update(active_state="active", sub_state="running", main_pid=456)
        return True

    def assert_original(self) -> None:
        self.assertEqual({n: p.read_bytes() for n, p in self.files.items()}, self.original)

    def assert_protected(self) -> None:
        self.assertEqual({p: p.read_bytes() for p in self.protected}, self.protected)

    def test_reviewed_production_hashes_and_source_commit_are_fixed(self) -> None:
        checkout = Path(__file__).resolve().parents[1]
        self.assertEqual(operation.REVIEWED_SOURCE_COMMIT, "8dd1b0ca830b3bdb90de2c4ca555a2f3db090d15")
        self.assertEqual(self.production_preimages, {
            "gateway": "1f21f4f626f8374bd957667dc471a5bc5b75bafda4f81cc95dc95b31911f5014",
            "codex_adapter": "954476a6e82eade56a51b71c1b8087766bfa57c9aac54730a5f7376cb5f67013",
        })
        for path, digest in (
            ("service/ai_gateway_service.py", "60012ad523cd0fbef0996a6d879e3625ace3a3c52a0c84957973d6ae01b95cdf"),
            ("service/adapters/codex_adapter.py", "e5175a5fb2c132b24dfc59a0af365c5af8ba2f3b8f6079fe398f97111c347cd0"),
        ):
            self.assertEqual(hashlib.sha256((checkout / path).read_bytes()).hexdigest(), digest)
        self.assertEqual(self.production_targets, {
            "gateway": "60012ad523cd0fbef0996a6d879e3625ace3a3c52a0c84957973d6ae01b95cdf",
            "codex_adapter": "e5175a5fb2c132b24dfc59a0af365c5af8ba2f3b8f6079fe398f97111c347cd0",
        })

    def test_cli_requires_explicit_acknowledgement_and_refuses_paths_units_or_restart(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(operation.main(["maintain"]), 1)
        self.assertEqual(json.loads(output.getvalue())["reason"], "interruption_not_acknowledged")
        self.assertEqual(self.actions, [])
        for args in (["maintain", "--unit", "nginx"], ["maintain", "--path", "/private"], ["restart"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                operation.main(args)
        self.assertEqual(self.actions, [])
        self.assertFalse(self.backup.exists())

    def test_acknowledged_incomplete_snapshot_is_never_a_drain_and_two_files_preserve_metadata(self) -> None:
        result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.actions, ["stop", "start"])
        self.assertFalse(result["drain_proven"])
        self.assertTrue(result["requests_may_be_interrupted"])
        self.assertEqual(result["job_counts"], self.counts)
        self.assertFalse(result["all_builtin_tools_disabled_proven"])
        self.assertEqual({n: p.read_bytes() for n, p in self.files.items()}, self.updated)
        for name, path in self.files.items():
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), self.metadata[name])
            self.assertEqual((self.backup / f"{name}.py").read_bytes(), self.original[name])
        self.assert_protected()
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_missing_acknowledgement_stale_source_or_preimage_do_nothing(self) -> None:
        self.assertEqual(operation.maintain_service(acknowledge_interruption=False)["reason"], "interruption_not_acknowledged")
        for destination, reason in ((self.targets["gateway"], "target_source_mismatch"),
                                    (self.files["gateway"], "preimage_mismatch")):
            original = destination.read_bytes()
            destination.write_bytes(b"unreviewed PRIVATE source")
            self.assertEqual(operation.maintain_service(acknowledge_interruption=True)["reason"], reason)
            self.assertFalse(self.backup.exists())
            self.assertEqual(self.actions, [])
            destination.write_bytes(original)
        self.assert_original()
        self.assert_protected()

    def test_unproven_entrypoint_or_known_cli_choice_never_stops_service(self) -> None:
        for key in self.process:
            with self.subTest(key=key):
                self.process[key] = False
                result = operation.maintain_service(acknowledge_interruption=True)
                self.assertEqual(result["reason"], "service_identity_unproven")
                self.process[key] = True
        self.assertEqual(self.actions, [])
        self.assertFalse(self.backup.exists())

    def test_backup_is_create_only_and_checked_before_stop(self) -> None:
        self.backup.mkdir()
        marker = self.backup / "gateway.py"
        marker.write_bytes(b"do not overwrite")
        result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["reason"], "backup_unavailable")
        self.assertEqual(marker.read_bytes(), b"do not overwrite")
        self.assertEqual(self.actions, [])
        self.assert_original()

    def test_stop_timeout_or_unit_not_stopped_never_replaces_or_auto_starts(self) -> None:
        for success in (False, True):
            with self.subTest(success=success), patch.object(operation, "_unit_action", return_value=success):
                result = operation.maintain_service(acknowledge_interruption=True)
                self.assertIn(result["reason"], {"stop_failed", "stop_not_confirmed"})
                self.assert_original()
                self.assertEqual(result["status"], "maintenance_failed")
            # A distinct invocation cannot reuse a backup; this test uses a fresh fixture.
            for path in self.backup.iterdir():
                path.unlink()
            self.backup.rmdir()
        self.assert_protected()

    def test_preimage_changed_during_stop_is_not_clobbered(self) -> None:
        def changed(action):
            self.action(action)
            if action == "stop":
                self.files["gateway"].write_bytes(b"external change")
            return True
        with patch.object(operation, "_unit_action", side_effect=changed):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(self.files["gateway"].read_bytes(), b"external change")
        self.assertEqual(self.actions, ["stop"])
        self.assertEqual(self.unit["active_state"], "inactive")

    def test_metadata_changed_during_stop_or_special_source_mode_fails_closed(self) -> None:
        path = self.files["gateway"]
        path.chmod(0o4640)
        self.assertEqual(operation.maintain_service(acknowledge_interruption=True)["status"], "refused")
        self.assertEqual(self.actions, [])
        path.chmod(self.metadata["gateway"][2])

        def changed(action):
            self.action(action)
            if action == "stop":
                path.chmod(0o600)
            return True
        with patch.object(operation, "_unit_action", side_effect=changed):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["reason"], "preimage_changed_after_stop")
        self.assertEqual(self.actions, ["stop"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assert_original()

    def test_second_replace_failure_restores_both_files_and_starts_original_once(self) -> None:
        original_replace = operation._atomic_replace
        calls = []

        def fail_second(path, data, metadata):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("PRIVATE second replacement")
            return original_replace(path, data, metadata)

        with patch.object(operation, "_atomic_replace", side_effect=fail_second):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["reason"], "replacement_failed")
        self.assertEqual(calls[2:], list(self.files.values()))
        self.assertEqual(self.actions, ["stop", "start"])
        self.assert_original()
        self.assert_protected()
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_start_timeout_stops_before_two_file_rollback_then_starts_original_once(self) -> None:
        starts = 0

        def start_timeout(action):
            nonlocal starts
            success = self.action(action)
            if action == "start":
                starts += 1
                return starts != 1
            return success

        with patch.object(operation, "_unit_action", side_effect=start_timeout):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.actions, ["stop", "start", "stop", "start"])
        self.assert_original()

    def test_post_start_source_mutation_enters_recovery_without_overwriting_external_bytes(self) -> None:
        def mutated_health():
            self.files["gateway"].write_bytes(b"external post-start source")
            return True
        with patch.object(operation, "_local_health", side_effect=mutated_health):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["reason"], "rollback_failed")
        self.assertEqual(self.files["gateway"].read_bytes(), b"external post-start source")
        self.assertEqual(self.files["codex_adapter"].read_bytes(), self.original["codex_adapter"])
        self.assertEqual(self.actions, ["stop", "start", "stop"])
        self.assertEqual(self.unit["active_state"], "inactive")
        self.assert_protected()

    def test_failed_health_restores_code_only_and_failed_rollback_stop_does_not_write(self) -> None:
        with patch.object(operation, "_local_health", side_effect=[False, True]):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "rolled_back")
        self.assert_original()
        self.assert_protected()

    def test_failed_rollback_stop_never_replaces_running_target_code(self) -> None:
        def fail_recovery_stop(action):
            if action == "stop" and self.actions == ["stop", "start"]:
                self.actions.append("stop")
                return False
            return self.action(action)
        with patch.object(operation, "_unit_action", side_effect=fail_recovery_stop), \
             patch.object(operation, "_local_health", return_value=False):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["reason"], "rollback_stop_unconfirmed")
        self.assertEqual({n: p.read_bytes() for n, p in self.files.items()}, self.updated)
        self.assert_protected()

    def test_rollback_original_start_failure_is_not_retried_and_closes_admission(self) -> None:
        with patch.object(operation, "_local_health", return_value=False):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["reason"], "rollback_start_failed")
        self.assertTrue(result["stopped_after_rollback"])
        self.assertEqual(self.actions, ["stop", "start", "stop", "start", "stop"])
        self.assert_original()
        self.assert_protected()

    def test_atomic_replace_cannot_delete_preexisting_stage_or_accept_source_symlinks(self) -> None:
        path = self.files["gateway"]
        stage = path.with_name(f".{path.name}.audit-patch-stage")
        stage.write_bytes(b"other attempt")
        with self.assertRaises(FileExistsError):
            operation._atomic_replace(path, self.updated["gateway"], path.stat())
        self.assertEqual(stage.read_bytes(), b"other attempt")
        self.assert_original()
        path.unlink()
        path.symlink_to(self.targets["gateway"])
        result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self.actions, [])

    def test_read_atime_changes_do_not_invalidate_otherwise_identical_snapshot(self) -> None:
        path = self.files["gateway"]
        os.utime(path, ns=(1, path.stat().st_mtime_ns))
        data, info = operation._file_snapshot(path)
        self.assertEqual(data, self.original["gateway"])
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), self.metadata["gateway"])

    def test_health_probe_is_bounded_fixed_get_only_and_never_sends_auth_or_follows_redirects(self) -> None:
        # Bypass only this fixture's mock; the real implementation opens no
        # socket because every HTTPConnection is replaced with an owned fake.
        health = self.real_health
        connections = []

        def connection(*args, **kwargs):
            response = Mock(status=200 if not connections else 401)
            response.read.return_value = b'{"status":"healthy"}' if not connections else b"PRIVATE_AUTH_ERROR"
            client = Mock()
            client.getresponse.return_value = response
            connections.append(client)
            self.assertEqual(args, ("127.0.0.1", 8797))
            self.assertEqual(kwargs, {"timeout": 2})
            return client

        with patch.object(operation.http.client, "HTTPConnection", side_effect=connection):
            self.assertTrue(health())
        self.assertEqual([c.request.call_args.args for c in connections], [("GET", "/healthz"), ("GET", "/v1/ai/health")])
        for client in connections:
            self.assertEqual(client.request.call_args.kwargs, {})
            client.getresponse.return_value.read.assert_called_once_with(4097)
            client.close.assert_called_once()
        for status, payload in ((302, b"redirect"), (200, b"x" * 4097), (200, b'{"status":"bad"}')):
            client = Mock()
            client.getresponse.return_value = Mock(status=status, read=Mock(return_value=payload))
            with self.subTest(status=status), patch.object(operation.http.client, "HTTPConnection", return_value=client), \
                 patch.object(operation.time, "monotonic", side_effect=[0, 0, 21]), patch.object(operation.time, "sleep"):
                self.assertFalse(health())

    def test_rollback_failure_attempts_both_files_and_leaves_unit_stopped(self) -> None:
        original_replace = operation._atomic_replace
        calls = []

        def fail_second_and_first_rollback(path, data, metadata):
            calls.append(path)
            if len(calls) in (2, 3):
                raise OSError("PRIVATE rollback failure")
            return original_replace(path, data, metadata)

        with patch.object(operation, "_atomic_replace", side_effect=fail_second_and_first_rollback):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["reason"], "rollback_failed")
        self.assertEqual(calls[2:], list(self.files.values()))
        self.assertEqual(self.actions, ["stop"])
        self.assertEqual(self.unit["active_state"], "inactive")
        self.assert_protected()

    def test_corrupted_backup_is_not_used_and_other_file_is_still_restored(self) -> None:
        original_replace = operation._atomic_replace
        calls = 0

        def corrupt_then_fail(path, data, metadata):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.backup / "gateway.py").write_bytes(b"tampered backup")
                raise OSError("synthetic second replacement failure")
            return original_replace(path, data, metadata)
        with patch.object(operation, "_atomic_replace", side_effect=corrupt_then_fail):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["reason"], "rollback_failed")
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(self.files["gateway"].read_bytes(), self.updated["gateway"])
        self.assertEqual(self.files["codex_adapter"].read_bytes(), self.original["codex_adapter"])
        self.assertEqual(self.actions, ["stop"])
        self.assert_protected()

    def test_failed_backup_write_does_not_stop_or_reuse_partial_backup(self) -> None:
        with patch.object(operation, "_write_exclusive", side_effect=OSError("PRIVATE disk full")):
            result = operation.maintain_service(acknowledge_interruption=True)
        self.assertEqual(result["reason"], "backup_unavailable")
        self.assertEqual(self.actions, [])
        self.assertTrue(self.backup.exists())
        self.assert_original()
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_stage_write_failure_removes_only_its_own_stage_and_never_changes_original(self) -> None:
        path = self.files["gateway"]
        with patch.object(operation.os, "write", side_effect=OSError("synthetic full disk")), self.assertRaises(OSError):
            operation._atomic_replace(path, self.updated["gateway"], path.stat())
        self.assertFalse(path.with_name(f".{path.name}.audit-patch-stage").exists())
        self.assert_original()

    def test_atomic_replace_rechecks_cas_after_staging_and_preserves_external_changes(self) -> None:
        path = self.files["gateway"]
        metadata = path.stat()
        write = operation._write_exclusive

        def changed(*args, **kwargs):
            result = write(*args, **kwargs)
            path.write_bytes(b"external mutation during staging")
            return result
        with patch.object(operation, "_write_exclusive", side_effect=changed), self.assertRaises(ValueError):
            operation._atomic_replace(path, self.updated["gateway"], metadata)
        self.assertEqual(path.read_bytes(), b"external mutation during staging")
        self.assertFalse(path.with_name(f".{path.name}.audit-patch-stage").exists())

    def test_started_acceptance_rejects_same_pid_or_wrong_route_without_http(self) -> None:
        self.unit["main_pid"] = 123
        with patch.object(operation, "_local_health") as health:
            self.assertFalse(operation._started(123))
            health.assert_not_called()
        self.unit["main_pid"] = 456
        self.process["entrypoint_matches_gateway"] = False
        with patch.object(operation, "_local_health") as health:
            self.assertFalse(operation._started(123))
            health.assert_not_called()

    def test_systemd_actions_use_only_fixed_stop_start_clean_environment_and_timeout(self) -> None:
        with patch.object(operation.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(self.real_unit_action("stop"))
            self.assertTrue(self.real_unit_action("start"))
            self.assertFalse(self.real_unit_action("restart"))
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ["/usr/bin/systemctl", "stop", operation.UNIT], ["/usr/bin/systemctl", "start", operation.UNIT],
        ])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"], operation.METADATA_ENV)
            self.assertEqual(call.kwargs["timeout"], operation.UNIT_ACTION_TIMEOUT_SECONDS)
            self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("shell", call.kwargs)
        with patch.object(operation.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixed", 30)):
            self.assertFalse(self.real_unit_action("stop"))
            self.assertFalse(self.real_unit_action("start"))

    def test_maintenance_workflow_binds_source_and_requires_explicit_interruption_acknowledgement(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        job = workflow.split("  apply-audit-patch:\n", 1)[1].split("\n  org-health-token:", 1)[0]
        self.assertIn("inputs.acknowledge_interruption", job)
        self.assertIn("inputs.mode != 'apply-audit-patch'", workflow)
        for value in ("environment: codex-vps-ops", "- self-hosted", "- codex-vps", "persist-credentials: false", "ref: ${{ github.sha }}",
                      '"$RUN_WORKFLOW_SHA" = "$RUN_SHA"', '"$(git rev-parse HEAD)" = "$RUN_SHA"', '"$current_main" = "$RUN_SHA"',
                      '"$ACKNOWLEDGE_INTERRUPTION" = true', "env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C", "maintain --acknowledge-interruption"):
            self.assertIn(value, job)
        self.assertLess(job.index("current_main="), job.index("manage_codex_audit_service_patch.py"))
        for forbidden in ("deploy_codex_audit_service.sh", "repair-ssh", "install-org-health-token", "secrets.", "nginx", "-k", "restart"):
            self.assertNotIn(forbidden, job)


class CodexServiceInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()

    def job(self, index: int, payload: object) -> Path:
        path = self.jobs / f"job_{index:024d}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_file_hash_is_bounded_and_never_imports_deployed_source(self) -> None:
        source = self.root / "source.py"
        data = b"raise RuntimeError('private source marker')\n"
        source.write_bytes(data)
        self.assertEqual(operation.source_hash(source), hashlib.sha256(data).hexdigest())
        with patch.object(operation, "MAX_SOURCE_BYTES", 4):
            self.assertIsNone(operation.source_hash(source))
        self.assertIsNone(operation.source_hash(self.root / "missing.py"))
        symlink = self.root / "symlink.py"
        symlink.symlink_to(source)
        self.assertIsNone(operation.source_hash(symlink))
        self.assertIsNone(operation.source_hash(self.root))

    def test_job_summary_counts_status_only_and_preserves_every_byte(self) -> None:
        for index, status in enumerate(("queued", "running", "succeeded", "failed")):
            self.job(index, {"status": status, "prompt": "PRIVATE_PROMPT", "output": "PRIVATE_OUTPUT"})
        (self.jobs / "quota.json").write_text('{"private_account": "SECRET"}', encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.jobs.iterdir()}
        result = operation.job_counts(self.jobs)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["running"], 1)
        self.assertEqual(result["unknown"], 0)
        self.assertTrue(result["complete"])
        self.assertFalse(result["admission_closed"])
        self.assertFalse(result["includes_sync_requests"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.jobs.iterdir()})
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_bad_status_json_name_and_symlink_are_unknown_without_disclosure(self) -> None:
        self.job(0, {"status": "PRIVATE_STATUS"})
        self.job(1, ["PRIVATE_OUTPUT"])
        self.job(2, {"status": None})
        self.job(3, {"status": "queued"}).write_bytes(b"invalid PRIVATE_JSON")
        self.job(4, {}).unlink()
        (self.jobs / f"job_{4:024d}.json").symlink_to(self.root / "private-target")
        (self.jobs / "unexpected-private-name.json").write_text("PRIVATE", encoding="utf-8")
        result = operation.job_counts(self.jobs)
        self.assertEqual(result["unknown"], 6)
        self.assertEqual(result["unknown_reasons"]["invalid_status"], 3)
        self.assertEqual(result["unknown_reasons"]["invalid_json_or_duplicate_key"], 1)
        self.assertEqual(result["unknown_reasons"]["unreadable_or_not_regular"], 1)
        self.assertEqual(result["unknown_reasons"]["invalid_name"], 1)
        self.assertFalse(result["complete"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("unexpected", json.dumps(result))

    def test_missing_directory_or_symlink_is_unknown_not_idle(self) -> None:
        for path in (self.root / "missing", self.root / "link"):
            if path.name == "link":
                path.symlink_to(self.jobs, target_is_directory=True)
            with self.subTest(path=path.name):
                result = operation.job_counts(path)
                self.assertGreaterEqual(result["unknown"], 1)
                self.assertFalse(result["complete"])

    def test_oversized_record_and_total_budget_are_unknown(self) -> None:
        self.job(0, {"status": "queued", "prompt": "x" * 100})
        with patch.object(operation, "MAX_JOB_BYTES", 32):
            result = operation.job_counts(self.jobs)
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["unknown_reasons"]["per_file_limit"], 1)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        with patch.object(operation, "MAX_TOTAL_JOB_BYTES", 8):
            result = operation.job_counts(self.jobs)
        self.assertGreaterEqual(result["unknown"], 1)
        self.assertEqual(result["unknown_reasons"]["total_byte_limit"], 1)
        self.assertTrue(result["truncated"])

    def test_directory_entry_cap_marks_unexamined_records_unknown(self) -> None:
        for index in range(3):
            self.job(index, {"status": "running"})
        with patch.object(operation, "MAX_JOB_ENTRIES", 2):
            result = operation.job_counts(self.jobs)
        self.assertEqual(result["running"], 2)
        self.assertGreaterEqual(result["unknown"], 1)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["unknown_reasons"]["entry_limit"], 1)

    def test_duplicate_status_is_unknown_and_failed_reads_consume_total_budget(self) -> None:
        self.job(0, {}).write_text('{"status":"running","status":"succeeded"}', encoding="utf-8")
        self.assertEqual(operation.job_counts(self.jobs)["unknown"], 1)
        self.job(1, {"status": "queued"})
        with patch.object(operation, "MAX_TOTAL_JOB_BYTES", 8), \
             patch.object(operation, "_regular_bytes", return_value=(None, "byte_limit", 8)) as reads:
            result = operation.job_counts(self.jobs)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(result["unknown"], 2)
        self.assertTrue(result["truncated"])

    def process_fixture(self, env: bytes | None = None, argv: bytes | None = None):
        proc = self.root / "proc"
        directory = proc / "123"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "stat").write_bytes(b"123 (python3) S " + b"0 " * 18 + b"98765 0\n")
        (directory / "cmdline").write_bytes(argv if argv is not None else b"python3\0-m\0service.ai_gateway_service\0")
        (directory / "environ").write_bytes(env if env is not None else (
            b"PRIVATE_TOKEN=DO_NOT_OUTPUT\0PATH=" + operation.SERVICE_PATH.encode() + b"\0"
        ))
        uid = directory.stat().st_uid
        unit = {"main_pid": 123, "user": "synthetic", "identity_verified": True, "known_working_directory": True}
        return proc, directory, uid, unit

    def inspect_process_fixture(self, proc, uid, unit):
        with patch.object(operation, "PROC_ROOT", proc), \
             patch.object(operation.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=uid)), \
             patch.object(operation, "unit_metadata", return_value=unit), \
             patch.object(operation.shutil, "which", return_value="/usr/local/bin/codex"), \
             patch.object(operation, "source_hash", return_value="a" * 64), \
             patch.object(operation, "metadata_output") as command:
            result = operation.service_process_metadata(unit)
        command.assert_not_called()
        return result

    def test_fixed_process_proof_is_initial_environment_only_without_secret_or_argv_output(self) -> None:
        proc, _, uid, unit = self.process_fixture()
        result = self.inspect_process_fixture(proc, uid, unit)
        self.assertTrue(result["snapshot_coherent"])
        self.assertTrue(result["entrypoint_matches_gateway"])
        self.assertTrue(result["initial_environment_complete"])
        self.assertTrue(result["initial_environment_known_cli_choice"])
        self.assertEqual(result["known_cli_launch_sha256"], "a" * 64)
        self.assertFalse(result["runtime_python_environment_verified"])
        self.assertFalse(result["running_source_identity_verified"])
        self.assertNotIn("DO_NOT_OUTPUT", json.dumps(result))
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
        self.assertNotIn("python3", json.dumps(result))
        self.assertNotIn("/usr/local/bin", json.dumps(result))

    def test_initial_env_unsupported_empty_relative_or_ambiguous_paths_fail_closed(self) -> None:
        service_path = operation.SERVICE_PATH.encode()
        for env in (
            b"PATH=" + service_path + b"\0CODEX_AUDIT_SERVICE_CODEX_BIN=\0",
            b"PATH=" + service_path + b"\0CODEX_AUDIT_SERVICE_CODEX_BIN=./codex\0",
            b"PATH=" + service_path + b"\0CODEX_AUDIT_SERVICE_CODEX_BIN=/private/custom\0",
            b"PATH=/usr/bin::/usr/local/bin\0", b"PATH=./bin:/usr/bin\0", b"PRIVATE_TOKEN=DO_NOT_OUTPUT\0",
            b"PATH=/usr/bin\0PATH=/usr/local/bin\0", b"PATH=" + service_path,
            b"PATH=" + service_path + b"\0CODEX_AUDIT_SERVICE_CODEX_BIN=codex\0CODEX_AUDIT_SERVICE_CODEX_BIN=codex\0",
            b"PATH=" + b"x" * 4097 + b"\0", b"PATH=\xff\0",
        ):
            with self.subTest(env=env):
                proc, _, uid, unit = self.process_fixture(env=env)
                result = self.inspect_process_fixture(proc, uid, unit)
                self.assertFalse(result["initial_environment_known_cli_choice"])
                self.assertIsNone(result["known_cli_launch_sha256"])
                self.assertNotIn("private", json.dumps(result).lower())

    def test_known_absolute_override_and_absence_follow_only_documented_initial_choice(self) -> None:
        for configured in (b"", b"CODEX_AUDIT_SERVICE_CODEX_BIN=codex\0", b"CODEX_AUDIT_SERVICE_CODEX_BIN=/usr/local/bin/codex\0"):
            with self.subTest(configured=configured):
                proc, _, uid, unit = self.process_fixture(env=b"PATH=" + operation.SERVICE_PATH.encode() + b"\0" + configured)
                self.assertTrue(self.inspect_process_fixture(proc, uid, unit)["initial_environment_known_cli_choice"])
        proc, _, uid, unit = self.process_fixture()
        with patch.object(operation, "MAX_ENV_RECORDS", 1):
            self.assertFalse(self.inspect_process_fixture(proc, uid, unit)["initial_environment_complete"])

    def test_invalid_unit_or_bad_stat_cannot_read_environment_or_lookup_configured_binary(self) -> None:
        _, _, uid, unit = self.process_fixture()
        for altered in ({**unit, "main_pid": True}, {**unit, "main_pid": 0},
                        {**unit, "known_working_directory": False}, {**unit, "identity_verified": False}):
            with self.subTest(unit=altered), patch.object(operation, "_regular_bytes") as read, \
                 patch.object(operation.shutil, "which") as lookup:
                self.assertFalse(operation.service_process_metadata(altered)["snapshot_coherent"])
                read.assert_not_called()
                lookup.assert_not_called()
        proc, directory, uid, unit = self.process_fixture()
        for stat_data in (b"PRIVATE_INVALID_STAT", b"999 (python3) S " + b"0 " * 18 + b"98765", b"123 (python3) S 0"):
            with self.subTest(stat_data=stat_data):
                directory.joinpath("stat").write_bytes(stat_data)
                self.assertFalse(self.inspect_process_fixture(proc, uid, unit)["snapshot_coherent"])
        directory.joinpath("stat").write_bytes(b"123 (python3) S " + b"0 " * 18 + b"98765 0\n")
        with patch.object(operation, "MAX_PROC_STAT_BYTES", 8):
            self.assertFalse(self.inspect_process_fixture(proc, uid, unit)["snapshot_coherent"])

    def test_process_bounds_uid_pid_starttime_and_entrypoint_mismatch_never_prove_identity(self) -> None:
        for argv in (b"python3\0-m\0scripts.codex_audit_service\0", b"python3\0-m\0service.ai_gateway_service\0SECRET_ARG\0"):
            with self.subTest(argv=argv):
                proc, _, uid, unit = self.process_fixture(argv=argv)
                result = self.inspect_process_fixture(proc, uid, unit)
                self.assertFalse(result["entrypoint_matches_gateway"])
                self.assertFalse(result["initial_environment_known_cli_choice"])
                self.assertNotIn("SECRET_ARG", json.dumps(result))
        proc, directory, uid, unit = self.process_fixture()
        with patch.object(operation, "MAX_PROC_ENV_BYTES", 8):
            self.assertFalse(self.inspect_process_fixture(proc, uid, unit)["initial_environment_complete"])
        with patch.object(operation, "MAX_PROC_ARG_BYTES", 8):
            self.assertFalse(self.inspect_process_fixture(proc, uid, unit)["entrypoint_matches_gateway"])
        self.assertFalse(self.inspect_process_fixture(proc, uid + 1, unit)["snapshot_coherent"])
        with patch.object(operation, "PROC_ROOT", proc), \
             patch.object(operation.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=uid)), \
             patch.object(operation, "unit_metadata", return_value={**unit, "main_pid": 456}):
            self.assertFalse(operation.service_process_metadata(unit)["snapshot_coherent"])
        before = directory.joinpath("stat").read_bytes()
        calls = 0
        original = operation._regular_bytes

        def replace_stat(path, *args, **kwargs):
            nonlocal calls
            if Path(path).name == "stat":
                calls += 1
                if calls == 2:
                    directory.joinpath("stat").write_bytes(before.replace(b"98765", b"98766"))
            return original(path, *args, **kwargs)

        with patch.object(operation, "_regular_bytes", side_effect=replace_stat):
            self.assertFalse(self.inspect_process_fixture(proc, uid, unit)["snapshot_coherent"])

    def test_source_replacement_during_read_is_unknown(self) -> None:
        source = self.root / "source.py"
        source.write_bytes(b"bounded source")
        replacement = self.root / "replacement.py"
        replacement.write_bytes(b"other source")
        original_stat = operation.os.stat

        def replace_then_stat(path, **kwargs):
            replacement.replace(source)
            return original_stat(path, **kwargs)

        with patch.object(operation.os, "stat", side_effect=replace_then_stat):
            self.assertIsNone(operation.source_hash(source))

    def test_unit_metadata_queries_only_fixed_safe_properties(self) -> None:
        output = (
            "Id=codex-audit-service.service\nLoadState=loaded\nActiveState=active\n"
            "SubState=running\nMainPID=123\nWorkingDirectory=/opt/codex-audit-bridge\n"
            "User=runner\nNoNewPrivileges=yes\nEnvironment=PRIVATE_SECRET\n"
        )
        with patch.object(operation, "metadata_output", return_value=output) as command:
            result = operation.unit_metadata()
        argv = command.call_args.args[0]
        self.assertEqual(argv[:3], ["/usr/bin/systemctl", "show", "codex-audit-service"])
        self.assertNotIn("Environment", " ".join(argv))
        self.assertNotIn("ExecStart", " ".join(argv))
        self.assertEqual(result["main_pid"], 123)
        self.assertTrue(result["known_working_directory"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_unknown_unit_metadata_does_not_echo_paths_or_untrusted_values(self) -> None:
        for output in (None, "ActiveState=PRIVATE\nMainPID=PRIVATE\nWorkingDirectory=/private\nUser=private secret\n"):
            with self.subTest(output=output), patch.object(operation, "metadata_output", return_value=output):
                result = operation.unit_metadata()
                self.assertFalse(result["known_working_directory"])
                self.assertIsNone(result["main_pid"])
                self.assertNotIn("private", json.dumps(result).lower())

    def test_cli_uses_only_version_and_help_with_bounded_capability_claim(self) -> None:
        responses = ["codex-cli 0.123.4\n", "  --disable <FEATURE>\n  -c, --config <KEY=VALUE>\n", (
            "  --ignore-user-config\n  --ephemeral\n  -C, --cd <DIR>\n"
            "  --sandbox <MODE>\n  --output-last-message <FILE>\n  --skip-git-repo-check\n"
        )]
        with patch.object(operation.shutil, "which", return_value="/usr/local/bin/codex"), \
             patch.object(operation, "metadata_output", side_effect=responses) as commands:
            result = operation.cli_capabilities()
        self.assertEqual([c.args[0] for c in commands.call_args_list], [
            ["/usr/local/bin/codex", "--version"],
            ["/usr/local/bin/codex", "--help"],
            ["/usr/local/bin/codex", "exec", "--help"],
        ])
        self.assertEqual(result["version"], "0.123.4")
        self.assertTrue(all(result["flags"].values()))
        self.assertFalse(result["feature_names_verified"])
        self.assertFalse(result["all_builtin_tools_disabled_proven"])

    def test_cli_missing_unknown_version_or_help_never_echoes_output(self) -> None:
        with patch.object(operation.shutil, "which", return_value=None), \
             patch.object(operation, "metadata_output") as command:
            result = operation.cli_capabilities()
            command.assert_not_called()
            self.assertIsNone(result["version"])
        with patch.object(operation.shutil, "which", return_value="/usr/local/bin/codex"), \
             patch.object(operation, "metadata_output", side_effect=["PRIVATE_VERSION", None, "PRIVATE_HELP"]):
            result = operation.cli_capabilities()
            self.assertIsNone(result["version"])
            self.assertFalse(any(result["flags"].values()))
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_metadata_runner_has_fixed_argv_clean_env_and_output_caps(self) -> None:
        stdout = Mock()
        stdout.fileno.return_value = 77
        process = SimpleNamespace(stdout=stdout, poll=Mock(return_value=0), wait=Mock(return_value=0), kill=Mock())
        selector = Mock()
        selector.select.return_value = [(None, None)]
        selector.__enter__ = Mock(return_value=selector)
        selector.__exit__ = Mock(return_value=False)
        command = ["/usr/local/bin/codex", "--version"]
        with patch.object(operation.subprocess, "Popen", return_value=process) as popen, \
             patch.object(operation.selectors, "DefaultSelector", return_value=selector), \
             patch.object(operation.os, "read", side_effect=[b"codex-cli 0.123.4\n", b""]):
            self.assertEqual(operation.metadata_output(command), "codex-cli 0.123.4\n")
        self.assertEqual(popen.call_args.args[0], command)
        kwargs = popen.call_args.kwargs
        self.assertEqual(set(kwargs["env"]), {"PATH", "LANG", "LC_ALL", "HOME", "CODEX_HOME"})
        self.assertEqual(kwargs["env"]["HOME"], "/nonexistent")
        self.assertEqual(kwargs["env"]["CODEX_HOME"], "/nonexistent")
        self.assertEqual(kwargs["cwd"], "/")
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stdin"], operation.subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], operation.subprocess.DEVNULL)
        with patch.object(operation.subprocess, "Popen", return_value=process), \
             patch.object(operation.selectors, "DefaultSelector", return_value=selector), \
             patch.object(operation, "MAX_COMMAND_BYTES", 8), \
             patch.object(operation.os, "read", return_value=b"PRIVATE_OVERSIZED"):
            self.assertIsNone(operation.metadata_output(command))
        with patch.object(operation.subprocess, "Popen", return_value=process), \
             patch.object(operation, "COMMAND_TIMEOUT_SECONDS", 0):
            self.assertIsNone(operation.metadata_output(command))
        with patch.object(operation.subprocess, "Popen") as popen:
            for argv in (["/usr/bin/systemctl", "restart", operation.UNIT], ["/tmp/codex", "--help"],
                         ["/usr/local/bin/codex", "exec", "prompt"], ["/usr/local/bin/codex", "features", "list"]):
                with self.subTest(argv=argv):
                    self.assertIsNone(operation.metadata_output(argv))
            popen.assert_not_called()

    def test_complete_inspection_is_import_free_read_only_and_rejects_arbitrary_operations(self) -> None:
        source = self.root / "gateway.py"
        adapter = self.root / "adapter.py"
        source.write_text("raise RuntimeError('PRIVATE')", encoding="utf-8")
        adapter.write_text("raise RuntimeError('PRIVATE')", encoding="utf-8")
        self.job(0, {"status": "running", "prompt": "PRIVATE"})
        before = {p: p.read_bytes() for p in (source, adapter, *self.jobs.iterdir())}
        out = io.StringIO()
        with patch.object(operation, "SOURCE_FILES", {"gateway": source, "codex_adapter": adapter}), \
             patch.object(operation, "JOB_DIRECTORY", self.jobs), \
             patch.object(operation, "unit_metadata", return_value={"main_pid": 123}), \
             patch.object(operation, "service_process_metadata", return_value={"snapshot_coherent": False}), \
             patch.object(operation, "cli_capabilities", return_value={"version": "0.123.4"}), \
             contextlib.redirect_stdout(out):
            self.assertEqual(operation.main(["inspect"]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["job_counts"]["running"], 1)
        self.assertEqual(set(result), {"operation", "source_sha256", "unit", "job_counts", "cli", "service_process"})
        self.assertNotIn("PRIVATE", out.getvalue())
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        for args in (["apply"], ["restart"], ["inspect", "--unit", "other"], ["inspect", "--path", "/private"]):
            with self.subTest(args=args), patch.object(operation, "inspect_service") as inspect, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                operation.main(args)
            inspect.assert_not_called()

    def test_workflow_binds_main_workflow_checkout_before_only_inspection(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        self.assertIn("- inspect-audit-patch", workflow)
        self.assertIn("inputs.mode != 'inspect-audit-patch'", workflow)
        job = workflow.split("  inspect-audit-patch:\n", 1)[1].split("\n  apply-audit-patch:", 1)[0]
        for value in ("environment: codex-vps-ops", "- self-hosted", "- codex-vps", "persist-credentials: false", "ref: ${{ github.sha }}"):
            self.assertIn(value, job)
        self.assertIn('"$RUN_WORKFLOW_SHA" = "$RUN_SHA"', job)
        self.assertIn('"$(git rev-parse HEAD)" = "$RUN_SHA"', job)
        self.assertIn('"$current_main" = "$RUN_SHA"', job)
        self.assertIn("vps_codex_service_ops.yml@refs/heads/main", job)
        self.assertLess(job.index("current_main="), job.index("manage_codex_audit_service_patch.py"))
        self.assertIn("env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C", job)
        self.assertIn("/usr/bin/python3 -I -B", job)
        for forbidden in ("deploy_codex_audit_service.sh", "systemctl stop", "systemctl restart", "secrets.", "nginx", "-k", "apply-audit-patch"):
            self.assertNotIn(forbidden, job)

    def test_actual_workflow_binding_script_rejects_stale_spoofed_or_dirty_source(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        # Exercise the actual shell guards. Functions replace all git/GitHub
        # metadata commands; no CLI, network, token or host operation is used.
        fixtures = '''
        git() {
          if [ "$1" = rev-parse ]; then printf '%s\\n' "$TEST_HEAD";
          elif [ "$1" = status ]; then printf '%s' "$TEST_DIRTY";
          else return 99; fi
        }
        gh() { printf '%s\\n' "$TEST_MAIN"; }
        timeout() { shift; "$@"; }
        '''
        sha = "a" * 40
        env = {
            "PATH": "/usr/bin:/bin", "RUN_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
            "RUN_REF": "refs/heads/main", "RUN_SHA": sha, "RUN_WORKFLOW_SHA": sha,
            "RUN_WORKFLOW_REF": "QuantStrategyLab/AIAuditBridge/.github/workflows/vps_codex_service_ops.yml@refs/heads/main",
            "TEST_HEAD": sha, "TEST_MAIN": sha, "TEST_DIRTY": "", "ACKNOWLEDGE_INTERRUPTION": "true",
        }
        for mode, boundary in (("inspect-audit-patch", "apply-audit-patch"), ("apply-audit-patch", "org-health-token")):
            job = workflow.split(f"  {mode}:\n", 1)[1].split(f"\n  {boundary}:", 1)[0]
            step = job.split("      - name: Verify exact main workflow and clean checkout", 1)[1]
            script = textwrap.dedent(step.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
            command = ["/bin/bash", "-c", textwrap.dedent(fixtures) + script + '\necho INSPECTION_BOUND\n']
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "INSPECTION_BOUND")
            failures = [
                ("RUN_REPOSITORY", "other/repository"), ("RUN_REF", "refs/heads/spoof"),
                ("RUN_SHA", "$(echo caller-command)"), ("RUN_WORKFLOW_SHA", "b" * 40),
                ("RUN_WORKFLOW_REF", "QuantStrategyLab/AIAuditBridge/.github/workflows/other.yml@refs/heads/main"),
                ("TEST_HEAD", "b" * 40), ("TEST_MAIN", "b" * 40), ("TEST_DIRTY", " M scripts/source.py"),
            ]
            if mode == "apply-audit-patch":
                failures += [("ACKNOWLEDGE_INTERRUPTION", "false"), ("ACKNOWLEDGE_INTERRUPTION", "")]
            for key, value in failures:
                with self.subTest(mode=mode, key=key):
                    result = subprocess.run(command, env={**env, key: value}, capture_output=True, text=True, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("INSPECTION_BOUND", result.stdout)


if __name__ == "__main__":
    unittest.main()
