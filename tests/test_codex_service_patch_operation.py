"""Fixed-target inspection uses synthetic files and mocked metadata commands only."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import manage_codex_audit_service_patch as operation


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

    def test_complete_inspection_is_import_free_read_only_and_has_no_apply_entrypoint(self) -> None:
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
        job = workflow.split("  inspect-audit-patch:\n", 1)[1].split("\n  org-health-token:", 1)[0]
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
        step = workflow.split("      - name: Verify exact main workflow and clean checkout", 1)[1]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1].split("      - name: Read fixed", 1)[0])
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
            "TEST_HEAD": sha, "TEST_MAIN": sha, "TEST_DIRTY": "",
        }
        command = ["/bin/bash", "-c", textwrap.dedent(fixtures) + script + '\necho INSPECTION_BOUND\n']
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "INSPECTION_BOUND")
        for key, value in (
            ("RUN_REPOSITORY", "other/repository"), ("RUN_REF", "refs/heads/spoof"),
            ("RUN_SHA", "$(echo caller-command)"), ("RUN_WORKFLOW_SHA", "b" * 40),
            ("RUN_WORKFLOW_REF", "QuantStrategyLab/AIAuditBridge/.github/workflows/other.yml@refs/heads/main"),
            ("TEST_HEAD", "b" * 40), ("TEST_MAIN", "b" * 40), ("TEST_DIRTY", " M scripts/source.py"),
        ):
            with self.subTest(key=key):
                result = subprocess.run(command, env={**env, key: value}, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("INSPECTION_BOUND", result.stdout)


if __name__ == "__main__":
    unittest.main()
