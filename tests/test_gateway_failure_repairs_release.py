"""The distinct gateway release uses synthetic files and mocked host primitives."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import manage_codex_audit_service_patch as legacy
from scripts import release_gateway_failure_repairs as release

safety = release.safety


class GatewayFailureRepairsReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gateway = self.root / "service/ai_gateway_service.py"
        self.adapter = self.root / "service/adapters/codex_adapter.py"
        self.target = self.root / "checkout/service/ai_gateway_service.py"
        for path in (self.gateway, self.adapter, self.target):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.original = b"old gateway\n"
        self.updated = b"new gateway\n"
        self.adapter_bytes = b"unchanged adapter\n"
        self.gateway.write_bytes(self.original)
        self.target.write_bytes(self.updated)
        self.adapter.write_bytes(self.adapter_bytes)
        self.gateway.chmod(0o640)
        self.adapter.chmod(0o644)
        self.gateway_metadata = self.owner_mode(self.gateway)
        self.gateway_identity = legacy._file_identity(self.gateway.stat())
        self.adapter_identity = legacy._file_identity(self.adapter.stat())
        self.record = self.root / ".audit-gateway-release-c80f2d52"
        self.protected = {}
        for name in ("configuration.env", "token", "unit.service", "nginx.conf", "jobs.json", "quota.json",
                     ".audit-patch-backup-8dd1b0ca/gateway.py", ".audit-patch-backup-8dd1b0ca/retry-1/admission.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"PRIVATE existing record or state")
            self.protected[path] = path.read_bytes()
        self.unit = {"identity_verified": True, "known_working_directory": True, "load_state": "loaded",
                     "active_state": "active", "sub_state": "running", "main_pid": 123, "user": "ubuntu"}
        self.process = {"snapshot_coherent": True, "entrypoint_matches_gateway": True,
                        "initial_environment_known_cli_choice": True}
        self.counts = {"queued": 0, "running": 0, "unknown": 10, "complete": False,
                       "truncated": True, "admission_closed": False, "includes_sync_requests": False}
        self.actions = []
        for module, attribute, value in (
            (release, "GATEWAY_FILE", self.gateway), (release, "ADAPTER_FILE", self.adapter),
            (release, "TARGET_FILE", self.target), (release, "RECORD_DIRECTORY", self.record),
            (release, "PREIMAGE_SHA256", hashlib.sha256(self.original).hexdigest()),
            (release, "TARGET_SHA256", hashlib.sha256(self.updated).hexdigest()),
            (release, "ADAPTER_SHA256", hashlib.sha256(self.adapter_bytes).hexdigest()),
        ):
            mock = patch.object(module, attribute, value)
            mock.start()
            self.addCleanup(mock.stop)
        for attribute, kwargs in (
            ("unit_metadata", {"side_effect": lambda **kwargs: dict(self.unit)}),
            ("service_process_metadata", {"side_effect": lambda unit, **kwargs: dict(self.process)}),
            ("job_counts", {"return_value": self.counts}),
            ("_process_identity", {"side_effect": lambda pid: (98765, os.getuid())}),
            ("_unit_action", {"side_effect": self.action}),
            ("_local_health", {"return_value": True}),
        ):
            mock = patch.object(safety, attribute, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    @staticmethod
    def owner_mode(path):
        info = path.stat()
        return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)

    def action(self, action):
        self.actions.append(action)
        if action == "stop":
            self.unit.update(active_state="inactive", sub_state="dead", main_pid=0)
        else:
            self.unit.update(active_state="active", sub_state="running", main_pid=456)
        return True

    def run_release(self):
        return release.release_service(acknowledge_interruption=True)

    def assert_protected(self):
        self.assertEqual({p: p.read_bytes() for p in self.protected}, self.protected)
        self.assertEqual(self.adapter.read_bytes(), self.adapter_bytes)
        self.assertEqual(legacy._file_identity(self.adapter.stat()), self.adapter_identity)

    def test_fixed_distinct_production_contract_and_historical_operator_unchanged(self):
        # Tests patch candidate globals, so read source-owned constants independently.
        source = Path(release.__file__).read_text()
        for value in ("c80f2d5255e6ed58907fe55405029320f0bfcd8d",
                      "5e5484d5db2e79458ea0683d6fbc604c5c77e5c2",
                      "60012ad523cd0fbef0996a6d879e3625ace3a3c52a0c84957973d6ae01b95cdf",
                      "60fc288f0135720e1a26daf897ad5c898cada7a884753a6e6495aaf6de4f7ec0",
                      "e5175a5fb2c132b24dfc59a0af365c5af8ba2f3b8f6079fe398f97111c347cd0"):
            self.assertIn(value, source)
        self.assertEqual(legacy.REVIEWED_SOURCE_COMMIT, "8dd1b0ca830b3bdb90de2c4ca555a2f3db090d15")
        self.assertEqual(legacy.source_hash(Path(legacy.__file__)),
                         "e0e4367eefe8c6fb9ea6c973716890145957b3466e22c60e2714035e292424c8")
        self.assertNotIn("retry-2", source)

    def test_real_isolated_cli_help_works_without_module_search_or_host_actions(self):
        result = subprocess.run([sys.executable, "-I", "-B", release.__file__, "--help"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--acknowledge-interruption", result.stdout)
        self.assertEqual(self.actions, [])
        self.assertFalse(self.record.exists())

    def test_real_isolated_cli_missing_ack_refuses_before_host_inputs(self):
        result = subprocess.run([sys.executable, "-I", "-B", release.__file__, "release"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["reason"], "interruption_not_acknowledged")
        self.assertFalse(payload["release_slot_consumed"])
        self.assertEqual(self.actions, [])
        self.assertFalse(self.record.exists())

    def test_changed_helper_is_rejected_before_its_code_executes_in_isolated_cli(self):
        script = self.root / Path(release.__file__).name
        script.write_bytes(Path(release.__file__).read_bytes())
        marker = self.root / "helper-executed"
        helper = self.root / "manage_codex_audit_service_patch.py"
        helper.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n")
        result = subprocess.run([sys.executable, "-I", "-B", script, "--help"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release safety helper mismatch", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.actions, [])

    def test_fixed_helper_symlink_is_rejected_without_fallback_import(self):
        script = self.root / Path(release.__file__).name
        script.write_bytes(Path(release.__file__).read_bytes())
        helper = self.root / "manage_codex_audit_service_patch.py"
        helper.symlink_to(Path(legacy.__file__).resolve())
        result = subprocess.run([sys.executable, "-I", "-B", script, "--help"],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.actions, [])

    def test_changed_helper_disk_hash_refuses_before_metadata_or_source_inputs(self):
        with patch.object(safety, "source_hash", return_value="changed helper"), \
             patch.object(safety, "_file_snapshot") as snapshot:
            result = self.run_release()
        self.assertEqual(result["reason"], "safety_operator_mismatch")
        snapshot.assert_not_called()
        safety.unit_metadata.assert_not_called()
        self.assertEqual(self.actions, [])
        self.assertFalse(self.record.exists())

    def test_no_acknowledgement_does_not_read_inputs_or_touch_host(self):
        with patch.object(release, "_inputs") as inputs:
            result = release.release_service()
        self.assertEqual(result["reason"], "interruption_not_acknowledged")
        inputs.assert_not_called()
        self.assertEqual(self.actions, [])
        self.assertFalse(self.record.exists())

    def test_cli_requires_this_interruption_ack_and_has_no_retry_or_variable_targets(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(release.main(["release"]), 1)
        self.assertEqual(json.loads(output.getvalue())["reason"], "interruption_not_acknowledged")
        for args in (["retry-once"], ["retry2"], ["release", "--path", "/private"],
                     ["release", "--unit", "nginx"], ["release", "--sha", "abc"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                release.main(args)
        self.assertEqual(self.actions, [])

    def test_new_gateway_only_apply_preserves_metadata_and_unknown_work_warning(self):
        result = self.run_release()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.actions, ["stop", "start"])
        self.assertEqual(self.gateway.read_bytes(), self.updated)
        self.assertEqual(self.owner_mode(self.gateway), self.gateway_metadata)
        self.assertTrue(result["terminal_receipt_written"])
        self.assertTrue(result["release_slot_consumed"])
        self.assertEqual(result["job_counts"], self.counts)
        self.assertFalse(result["drain_proven"])
        self.assertTrue(result["requests_may_be_interrupted"])
        self.assertEqual(result["rollback_scope"], "gateway_code_only")
        self.assertFalse(result["running_source_identity_verified"])
        self.assertFalse(result["runtime_python_environment_verified"])
        self.assertEqual((self.record / "gateway.py").read_bytes(), self.original)
        self.assertEqual(set(p.name for p in self.record.iterdir()), {"gateway.py", "admission.json", "terminal.json"})
        admission = json.loads((self.record / "admission.json").read_bytes())
        self.assertEqual(admission["preimage_sha256"], release.PREIMAGE_SHA256)
        self.assertEqual(admission["target_sha256"], release.TARGET_SHA256)
        self.assertEqual(admission["adapter_sha256"], release.ADAPTER_SHA256)
        self.assert_protected()

    def test_target_or_gateway_preimage_or_adapter_mismatch_never_stops(self):
        for path, reason in ((self.target, "target_source_mismatch"), (self.gateway, "preimage_mismatch"),
                             (self.adapter, "adapter_mismatch")):
            data = path.read_bytes()
            path.write_bytes(b"PRIVATE unreviewed content")
            with self.subTest(reason=reason):
                result = self.run_release()
                self.assertEqual(result["reason"], reason)
                self.assertEqual(self.actions, [])
                self.assertFalse(self.record.exists())
            path.write_bytes(data)

    def test_unproven_unit_or_process_never_creates_record_or_stops(self):
        for key in self.process:
            self.process[key] = False
            self.assertEqual(self.run_release()["reason"], "service_identity_unproven")
            self.process[key] = True
        self.unit["identity_verified"] = False
        self.assertEqual(self.run_release()["reason"], "service_identity_unproven")
        self.assertEqual(self.actions, [])
        self.assertFalse(self.record.exists())

    def test_existing_partial_record_is_consumed_and_never_reused(self):
        self.record.mkdir()
        marker = self.record / "admission.json"
        marker.write_bytes(b"partial record")
        result = self.run_release()
        self.assertEqual(result["reason"], "release_already_consumed")
        self.assertTrue(result["release_slot_consumed"])
        self.assertEqual(marker.read_bytes(), b"partial record")
        self.assertEqual(self.actions, [])

    def test_existing_record_symlink_is_not_followed(self):
        destination = self.root / "foreign"
        destination.mkdir()
        self.record.symlink_to(destination, target_is_directory=True)
        result = self.run_release()
        self.assertEqual(result["reason"], "release_already_consumed")
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(self.actions, [])

    def test_admission_write_failure_consumes_slot_without_stop_and_has_no_replay(self):
        original_write = safety._write_exclusive
        def failing(path, *args, **kwargs):
            if Path(path).name == "admission.json":
                raise OSError("PRIVATE failure")
            return original_write(path, *args, **kwargs)
        with patch.object(safety, "_write_exclusive", side_effect=failing):
            result = self.run_release()
        self.assertEqual(result["reason"], "release_admission_unavailable")
        self.assertTrue(result["release_slot_consumed"])
        self.assertEqual(self.run_release()["reason"], "release_already_consumed")
        self.assertEqual(self.actions, [])
        self.assert_protected()

    def test_changed_unit_before_stop_has_durable_terminal_and_no_stop(self):
        calls = 0
        def changed(**kwargs):
            nonlocal calls
            calls += 1
            return {**self.unit, "main_pid": 123 if calls == 1 else 987}
        with patch.object(safety, "unit_metadata", side_effect=changed):
            result = self.run_release()
        self.assertEqual(result["reason"], "service_changed_before_stop")
        self.assertTrue(result["terminal_receipt_written"])
        self.assertEqual(self.actions, [])

    def test_stop_failure_or_unconfirmed_stop_has_no_write_or_auto_start(self):
        with patch.object(safety, "_unit_action", return_value=False):
            result = self.run_release()
        self.assertEqual(result["reason"], "stop_failed")
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(self.run_release()["reason"], "release_already_consumed")
        self.assert_protected()

    def test_stop_unconfirmed_leaves_files_untouched(self):
        with patch.object(safety, "_unit_action", return_value=True):
            result = self.run_release()
        self.assertEqual(result["reason"], "stop_not_confirmed")
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assert_protected()

    def test_gateway_mutation_during_stop_is_not_clobbered_or_restarted(self):
        def changed(action):
            self.action(action)
            if action == "stop":
                self.gateway.write_bytes(b"administrator source")
            return True
        with patch.object(safety, "_unit_action", side_effect=changed):
            result = self.run_release()
        self.assertEqual(result["reason"], "preimage_changed_after_stop")
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(self.gateway.read_bytes(), b"administrator source")
        self.assertEqual(self.actions, ["stop"])

    def test_adapter_mutation_during_stop_is_never_written_or_restarted(self):
        def changed(action):
            self.action(action)
            if action == "stop":
                self.adapter.write_bytes(b"administrator adapter")
            return True
        with patch.object(safety, "_unit_action", side_effect=changed):
            result = self.run_release()
        self.assertEqual(result["reason"], "adapter_changed_after_stop")
        self.assertEqual(self.adapter.read_bytes(), b"administrator adapter")
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(self.actions, ["stop"])

    def test_gateway_metadata_mutation_during_stop_is_not_clobbered(self):
        def changed(action):
            self.action(action)
            if action == "stop":
                self.gateway.chmod(0o600)
            return True
        with patch.object(safety, "_unit_action", side_effect=changed):
            result = self.run_release()
        self.assertEqual(result["reason"], "preimage_changed_after_stop")
        self.assertEqual(stat.S_IMODE(self.gateway.stat().st_mode), 0o600)
        self.assertEqual(self.actions, ["stop"])

    def test_special_mode_symlink_or_hardlink_preimages_are_refused(self):
        self.gateway.chmod(0o4640)
        self.assertEqual(self.run_release()["reason"], "release_inputs_unavailable")
        self.gateway.chmod(0o640)
        os.link(self.gateway, self.root / "hardlink")
        self.assertEqual(self.run_release()["reason"], "release_inputs_unavailable")
        self.assertEqual(self.actions, [])

    def test_apply_cas_detects_same_bytes_replaced_inode(self):
        original = safety._write_exclusive
        def changed(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if "stage" in str(path):
                replacement = self.root / "other"
                replacement.write_bytes(self.original)
                replacement.chmod(0o640)
                replacement.replace(self.gateway)
            return info
        with patch.object(safety, "_write_exclusive", side_effect=changed):
            result = self.run_release()
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["reason"], "rollback_failed")
        self.assertNotEqual(legacy._file_identity(self.gateway.stat()), self.gateway_identity)
        self.assertEqual(self.actions, ["stop"])
        self.assertEqual(self.gateway.read_bytes(), self.original)

    def test_failed_new_start_restores_only_this_gateway_preimage_once(self):
        def failed(action):
            if action == "start" and self.actions.count("start") == 0:
                self.actions.append(action)
                return False
            return self.action(action)
        with patch.object(safety, "_unit_action", side_effect=failed):
            result = self.run_release()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["reason"], "start_failed")
        self.assertEqual(self.actions, ["stop", "start", "start"])
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(self.owner_mode(self.gateway), self.gateway_metadata)
        self.assert_protected()

    def test_failed_health_or_auth_stops_then_restores_and_starts_original_once(self):
        with patch.object(safety, "_local_health", side_effect=[False, True]):
            result = self.run_release()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.actions, ["stop", "start", "stop", "start"])
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(result["startup_status"]["failure_phase"], "startup_health")
        self.assert_protected()

    def test_unconfirmed_rollback_stop_never_restores_beneath_running_service(self):
        def stop_failed(action):
            if action == "stop" and self.actions.count("stop"):
                self.actions.append(action)
                return False
            return self.action(action)
        with patch.object(safety, "_unit_action", side_effect=stop_failed), \
             patch.object(safety, "_local_health", return_value=False):
            result = self.run_release()
        self.assertEqual(result["reason"], "rollback_stop_unconfirmed")
        self.assertEqual(self.gateway.read_bytes(), self.updated)
        self.assertEqual(self.actions, ["stop", "start", "stop"])

    def test_unknown_gateway_after_start_is_never_rolled_back_over(self):
        def changed(action):
            self.action(action)
            if action == "start":
                self.gateway.write_bytes(b"administrator source")
            return True
        with patch.object(safety, "_unit_action", side_effect=changed):
            result = self.run_release()
        self.assertEqual(result["reason"], "rollback_failed")
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(self.gateway.read_bytes(), b"administrator source")
        self.assertEqual(self.actions, ["stop", "start", "stop"])

    def test_unknown_adapter_after_start_is_never_written_or_restarted(self):
        def changed(action):
            self.action(action)
            if action == "start":
                self.adapter.write_bytes(b"administrator adapter")
            return True
        with patch.object(safety, "_unit_action", side_effect=changed):
            result = self.run_release()
        self.assertEqual(result["reason"], "rollback_adapter_changed")
        self.assertEqual(self.adapter.read_bytes(), b"administrator adapter")
        self.assertEqual(self.actions, ["stop", "start", "stop"])

    def test_failed_rollback_start_is_stopped_and_not_retried(self):
        with patch.object(safety, "_local_health", return_value=False):
            result = self.run_release()
        self.assertEqual(result["reason"], "rollback_start_failed")
        self.assertTrue(result["stopped_after_rollback"])
        self.assertEqual(self.actions, ["stop", "start", "stop", "start", "stop"])
        self.assertEqual(self.gateway.read_bytes(), self.original)
        self.assertEqual(self.run_release()["reason"], "release_already_consumed")
        self.assert_protected()

    def test_durable_terminal_keeps_runtime_outcome_and_omits_write_confirmation_flag(self):
        result = self.run_release()
        terminal = json.loads((self.record / "terminal.json").read_bytes())
        self.assertEqual(terminal["status"], "applied")
        self.assertEqual(terminal["source_sha256"], result["source_sha256"])
        self.assertTrue(result["terminal_receipt_written"])
        self.assertNotIn("terminal_receipt_written", terminal)

    def test_terminal_receipt_failure_never_repeats_or_reverses_applied_release(self):
        original_write = safety._write_exclusive
        def failing(path, *args, **kwargs):
            if Path(path).name == "terminal.json":
                raise OSError("PRIVATE failure")
            return original_write(path, *args, **kwargs)
        with patch.object(safety, "_write_exclusive", side_effect=failing):
            result = self.run_release()
        self.assertEqual(result["status"], "applied")
        self.assertFalse(result["terminal_receipt_written"])
        self.assertEqual(result["receipt_error"], "terminal_receipt_unavailable")
        self.assertEqual(self.actions, ["stop", "start"])
        self.assertEqual(self.run_release()["reason"], "release_already_consumed")
        self.assertEqual(self.gateway.read_bytes(), self.updated)
        self.assert_protected()

    def test_unexpected_phase_failure_is_unknown_without_implicit_recovery(self):
        with patch.object(release, "_phases", side_effect=RuntimeError("PRIVATE internal value")):
            result = self.run_release()
        self.assertEqual(result["reason"], "release_state_unconfirmed")
        self.assertEqual(result["status"], "recovery_required")
        self.assertTrue(result["terminal_receipt_written"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(self.actions, [])
        self.assertEqual(self.run_release()["reason"], "release_already_consumed")

    def test_frozen_legacy_maintenance_still_rejects_the_new_target(self):
        root = Path(release.__file__).resolve().parents[1]
        with patch.object(legacy, "TARGET_FILES", {
            "gateway": root / "service/ai_gateway_service.py",
            "codex_adapter": root / "service/adapters/codex_adapter.py",
        }):
            self.assertEqual(legacy._maintenance_inputs({}), "target_source_mismatch")
        self.assertEqual(self.actions, [])
