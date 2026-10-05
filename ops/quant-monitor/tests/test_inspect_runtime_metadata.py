"""Synthetic-only tests: never query this machine's systemd or runtime."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inspect_runtime_metadata.py"
spec = importlib.util.spec_from_file_location("runtime_metadata", SCRIPT)
assert spec and spec.loader
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)

RUNTIME = "/home/ubuntu/quant-monitor-runtime/AIAuditBridge"
SHA = "a" * 40
RELEASE = f"/opt/quant-monitor/releases/{SHA}"
SUFFIX = "/ops/quant-monitor"
SECRET = "synthetic-never-print-secret"


def command(root: str, script: str, args: str = "") -> str:
    target = f"{root}{SUFFIX}/scripts/{script}"
    return ("{ path=/bin/bash ; argv[]=/bin/bash " + target + args
            + " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")


def service(root: str, script: str) -> dict[str, str]:
    return {
        "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
        "UnitFileState": "static", "User": "ubuntu", "Group": "ubuntu",
        "FragmentPath": "/etc/systemd/system/" + (
            "codex-quant.service" if script == "health_check.sh" else "codex-daily-briefing.service"),
        "DropInPaths": "", "WorkingDirectory": root + SUFFIX,
        "ExecStart": command(root, script),
        "ExecStartPre": command(root, "load_telegram_env.sh", " /run/quant-monitor/telegram.env"),
    }


class FakeReader:
    def __init__(self) -> None:
        self.roots: list[str] = []

    def inspect(self, root: str) -> dict:
        self.roots.append(root)
        return {"sync_lifecycle_artifacts": {"status": "unknown"},
                "git_head": None, "dirty_count": None, "links": {}}


class RuntimeMetadataTests(unittest.TestCase):
    def test_runtime_and_immutable_roots_only(self) -> None:
        self.assertEqual(metadata.classify_root(RUNTIME)["kind"], "runtime_checkout")
        self.assertEqual(metadata.classify_root(RELEASE)["release_sha"], SHA)
        for root in ["/tmp/AIAuditBridge", RELEASE + "/../evil", RELEASE + "/", RUNTIME + "/other", "/opt/quant-monitor/releases/main"]:
            self.assertIsNone(metadata.classify_root(root))

    def test_consistent_units_and_timer(self) -> None:
        values = {
            "codex-quant.service": service(RELEASE, "health_check.sh"),
            "codex-daily-briefing.service": service(RELEASE, "daily_briefing_pipeline.sh"),
            "codex-quant.timer": {"ActiveState": "active", "SubState": "waiting", "Unit": "codex-quant.service", "NextElapseUSecMonotonic": "45min 2s"},
            "codex-daily-briefing.timer": {"ActiveState": "active", "SubState": "waiting", "Unit": "codex-daily-briefing.service", "NextElapseUSecRealtime": "Mon 2026-10-05 22:30:00 UTC"},
        }
        reader = FakeReader()
        result = metadata.collect_metadata(query=values.get, reader=reader)
        self.assertEqual(reader.roots, [RELEASE])
        self.assertTrue(result["services"]["codex-quant.service"]["code_root_consistent"])
        self.assertTrue(result["services"]["codex-quant.service"]["exec_start"]["expected_entrypoint"])
        self.assertEqual(result["timers"]["codex-quant.timer"]["next_trigger_monotonic_us"], 2702000000)
        self.assertEqual(result["timers"]["codex-daily-briefing.timer"]["next_trigger_utc"], "2026-10-05T22:30:00Z")
        self.assertTrue(result["services_share_code_root"])

    def test_mixed_template_release_roots_are_false(self) -> None:
        values = {"codex-quant.service": service(RELEASE, "health_check.sh"),
                  "codex-daily-briefing.service": service(RUNTIME, "daily_briefing_pipeline.sh")}
        values["codex-quant.service"]["ExecStartPre"] = command(RUNTIME, "load_telegram_env.sh", " /run/quant-monitor/telegram.env")
        result = metadata.collect_metadata(query=values.get, reader=FakeReader())
        self.assertIs(result["services_share_code_root"], False)
        self.assertFalse(result["services"]["codex-quant.service"]["code_root_consistent"])

    def test_unknown_args_and_sensitive_fields_are_never_output(self) -> None:
        props = service(RELEASE, "health_check.sh")
        props["ExecStart"] = command(RELEASE, "health_check.sh", " --token=" + SECRET)
        props["Environment"] = "GH_TOKEN=" + SECRET
        props["EnvironmentFiles"] = "/private/" + SECRET
        props["User"] = SECRET
        props["FragmentPath"] = "/private/" + SECRET
        props["DropInPaths"] = "/private/" + SECRET
        result = metadata.collect_metadata(query=lambda unit: props if unit.endswith(".service") else None, reader=FakeReader())
        rendered = json.dumps(result)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn(hashlib.sha256(SECRET.encode()).hexdigest(), rendered)
        self.assertNotIn("/private/", rendered)
        self.assertFalse(result["services"]["codex-quant.service"]["exec_start"]["expected_shape"])
        self.assertIsNone(result["services"]["codex-quant.service"]["code_root_consistent"])
        self.assertEqual(set(result["uninspected_environment_paths"]), {"QUANT_MONITOR_ROOT", "AIAUDIT_BRIDGE_ROOT", "QUANT_PLATFORM_KIT_ROOT", "QUANT_MONITOR_VENV", "PROJECTS_ROOT", "QUANT_PROJECTS_ROOT", "LIFECYCLE_LOCAL_ROOT"})
        self.assertEqual(set(result["uninspected_environment_paths"].values()), {"unknown"})

    def test_unknown_path_does_not_read_files(self) -> None:
        props = service("/private/" + SECRET, "health_check.sh")
        reader = FakeReader()
        result = metadata.collect_metadata(query=lambda unit: props, reader=reader)
        self.assertEqual(reader.roots, [])
        self.assertIsNone(result["services"]["codex-quant.service"]["code_root_consistent"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_failed_query_is_unknown_and_not_false(self) -> None:
        result = metadata.collect_metadata(query=lambda unit: None, reader=FakeReader())
        self.assertEqual(result["services"]["codex-quant.service"]["query_status"], "unknown")
        self.assertIsNone(result["services_share_code_root"])
        self.assertIsNone(result["services"]["codex-quant.service"]["code_root_consistent"])

    def test_command_ambiguity_rejected(self) -> None:
        for raw in [command(RELEASE, "health_check.sh") * 2,
                    command(RELEASE, "health_check.sh").replace("path=/bin/bash", "path=/bin/sh"),
                    command(RELEASE, "health_check.sh").replace("argv[]=/bin/bash", "argv[]=/bin/bash -c"),
                    command(RELEASE, "health_check.sh").replace(" ; ignore_errors=no", " ; x=" + SECRET + " ; ignore_errors=no"),
                    "/bin/bash " + RELEASE + SUFFIX + "/scripts/health_check.sh"]:
            parsed = metadata.parse_exec(raw, "health_check.sh", pre=False)
            self.assertFalse(parsed["expected_shape"])
            self.assertNotIn(SECRET, json.dumps(parsed))

    def test_query_has_fixed_readonly_properties_and_clean_environment(self) -> None:
        completed = mock.Mock(returncode=0, stdout="ActiveState=active\nEnvironment=" + SECRET + "\n")
        with mock.patch.object(metadata.subprocess, "run", return_value=completed) as run:
            result = metadata.query_systemd("codex-quant.service")
        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], ["/usr/bin/systemctl", "--no-pager", "show", "codex-quant.service"])
        self.assertNotIn("Environment", " ".join(args[0]))
        self.assertNotIn("EnvironmentFiles", " ".join(args[0]))
        self.assertNotIn("cat", args[0])
        self.assertNotIn(SECRET, repr(kwargs))
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(result, {"ActiveState": "active"})
        with mock.patch.object(metadata.subprocess, "run") as run:
            self.assertIsNone(metadata.query_systemd("arbitrary.service"))
            run.assert_not_called()

    def test_query_errors_do_not_echo_output(self) -> None:
        for error in [PermissionError(SECRET), metadata.subprocess.TimeoutExpired("systemctl", 5, output=SECRET)]:
            with mock.patch.object(metadata.subprocess, "run", side_effect=error):
                self.assertIsNone(metadata.query_systemd("codex-quant.service"))

    def test_secure_reads_only_fixed_code_and_link_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "AIAuditBridge"
            scripts = root / "ops/quant-monitor/scripts"
            scripts.mkdir(parents=True)
            code = b"# synthetic public code\n"
            (scripts / "sync_lifecycle_artifacts.py").write_bytes(code)
            monitor = scripts.parent
            (monitor / "data/lifecycle-projects/QuantPlatformKit").mkdir(parents=True)
            (monitor / ".venv").symlink_to("/private/" + SECRET)
            (root / ".git/refs/heads").mkdir(parents=True)
            (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
            (root / ".git/refs/heads/main").write_text(SHA + "\n")
            with mock.patch.object(metadata, "RUNTIME_ROOT", str(root)):
                result = metadata.FixedReader().inspect(str(root))
            self.assertEqual(result["sync_lifecycle_artifacts"]["sha256"], hashlib.sha256(code).hexdigest())
            self.assertEqual(result["sync_lifecycle_artifacts"]["size_bytes"], len(code))
            self.assertEqual(result["git_head"], SHA)
            self.assertIsNone(result["dirty_count"])
            self.assertEqual(result["links"]["data"]["kind"], "directory")
            self.assertEqual(result["links"]["qpk"]["kind"], "directory")
            self.assertFalse(result["links"]["venv"]["matches_known_runtime_target"])
            self.assertNotIn(SECRET, json.dumps(result))

    def test_symlink_code_and_symlink_ancestor_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "AIAuditBridge"
            scripts = root / "ops/quant-monitor/scripts"
            scripts.mkdir(parents=True)
            outside = Path(temp) / "secret"
            outside.write_text(SECRET)
            (scripts / "sync_lifecycle_artifacts.py").symlink_to(outside)
            with mock.patch.object(metadata, "RUNTIME_ROOT", str(root)):
                result = metadata.FixedReader().inspect(str(root))
            self.assertIsNone(result["sync_lifecycle_artifacts"]["sha256"])
            self.assertNotIn(SECRET, json.dumps(result))
            linked_root = Path(temp) / "linked"
            linked_root.symlink_to(root, target_is_directory=True)
            with mock.patch.object(metadata, "RUNTIME_ROOT", str(linked_root)):
                result = metadata.FixedReader().inspect(str(linked_root))
            self.assertEqual(result["sync_lifecycle_artifacts"]["status"], "unknown")

    def test_unknown_data_link_never_traversed_for_qpk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "AIAuditBridge"
            monitor = root / "ops/quant-monitor"
            monitor.mkdir(parents=True)
            (monitor / "data").symlink_to("/private/" + SECRET)
            with mock.patch.object(metadata, "RUNTIME_ROOT", str(root)):
                result = metadata.FixedReader().inspect(str(root))
            self.assertEqual(result["links"]["qpk"]["kind"], "unknown")
            self.assertNotIn(SECRET, json.dumps(result))

    def test_reader_denies_nonfixed_file_and_root(self) -> None:
        reader = metadata.FixedReader()
        with mock.patch.object(metadata.os, "open") as opened:
            with self.assertRaises(ValueError):
                reader.read_file(RUNTIME, "ops/quant-monitor/data/account.json")
            with self.assertRaises(ValueError):
                reader.read_file("/tmp", "ops/quant-monitor/scripts/sync_lifecycle_artifacts.py")
            opened.assert_not_called()

    def test_known_release_data_link_checks_only_fixed_runtime_qpk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime/AIAuditBridge"
            release_base = Path(temp) / "releases"
            release = release_base / SHA
            for root in (runtime, release):
                (root / "ops/quant-monitor").mkdir(parents=True)
            (runtime / "ops/quant-monitor/data/lifecycle-projects/QuantPlatformKit").mkdir(parents=True)
            (release / "ops/quant-monitor/data").symlink_to(runtime / "ops/quant-monitor/data")
            (release / "ops/quant-monitor/.venv").symlink_to(runtime / "ops/quant-monitor/.venv")
            with mock.patch.object(metadata, "RUNTIME_ROOT", str(runtime)), mock.patch.object(metadata, "RELEASE_ROOT", str(release_base)):
                result = metadata.FixedReader().inspect(str(release))
            self.assertIs(result["links"]["data"]["matches_known_runtime_target"], True)
            self.assertIs(result["links"]["venv"]["matches_known_runtime_target"], True)
            self.assertEqual(result["links"]["qpk"]["kind"], "directory")

    def test_permission_denial_remains_unknown(self) -> None:
        with mock.patch.object(metadata.os, "open", side_effect=PermissionError(SECRET)):
            result = metadata.FixedReader().inspect(RUNTIME)
        self.assertEqual(result["sync_lifecycle_artifacts"]["status"], "unknown")
        self.assertEqual(result["links"]["data"]["kind"], "unknown")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_code_size_limit_and_nonregular_file_are_not_opened(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "AIAuditBridge"
            scripts = root / "ops/quant-monitor/scripts"
            scripts.mkdir(parents=True)
            code = scripts / "sync_lifecycle_artifacts.py"
            code.write_bytes(b"a" * (metadata.PUBLIC_FILES[metadata.SYNC_FILE] + 1))
            with mock.patch.object(metadata, "RUNTIME_ROOT", str(root)):
                result = metadata.FixedReader().inspect(str(root))
            self.assertIsNone(result["sync_lifecycle_artifacts"]["sha256"])
            code.unlink()
            code.mkdir()
            with mock.patch.object(metadata, "RUNTIME_ROOT", str(root)):
                result = metadata.FixedReader().inspect(str(root))
            self.assertIsNone(result["sync_lifecycle_artifacts"]["sha256"])

    def test_cli_rejects_arbitrary_path_without_query_or_echo(self) -> None:
        completed = subprocess.run([sys.executable, str(SCRIPT), "--path", "/private/" + SECRET], capture_output=True, text=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr.strip(), "unsupported arguments")

    def test_duplicate_or_oversized_query_output_is_unknown(self) -> None:
        for text in ("ActiveState=active\nActiveState=inactive\n", "ActiveState=" + "x" * (128 * 1024)):
            with mock.patch.object(metadata.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=text)):
                self.assertIsNone(metadata.query_systemd("codex-quant.service"))

    @staticmethod
    def workflow_jobs() -> tuple[str, str, str]:
        workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        quant = workflow.split("  inspect-quant-runtime:\n", 1)[1].split("\n  inspect-audit-patch:", 1)[0]
        audit = workflow.split("  inspect-audit-patch:\n", 1)[1].split("\n  apply-audit-patch:", 1)[0]
        return workflow, quant, audit

    def test_workflow_has_narrow_choice_and_excludes_general_secret_job(self) -> None:
        workflow, quant, _ = self.workflow_jobs()
        general = workflow.split("  vps-codex-service-ops:\n", 1)[1].split("\n  inspect-quant-runtime:", 1)[0]
        self.assertIn("          - inspect-quant-runtime\n", workflow)
        self.assertIn("        default: inspect\n", workflow)
        self.assertIn("inputs.mode != 'inspect-quant-runtime'", general)
        expected = "if: github.event_name == 'workflow_dispatch' && github.repository == 'QuantStrategyLab/AIAuditBridge' && github.ref == 'refs/heads/main' && inputs.mode == 'inspect-quant-runtime'"
        self.assertIn(expected, quant)
        for value in ("environment: codex-vps-ops", "- self-hosted", "- codex-vps", "timeout-minutes: 5",
                      "ref: ${{ github.sha }}", "persist-credentials: false"):
            self.assertIn(value, quant)
        for forbidden in ("sudo", "secrets.", "acknowledge_interruption", "acknowledge-interruption",
                          "systemctl", "deploy_codex_audit_service.sh", "manage_codex_audit_service_patch.py",
                          "health_check.sh", "daily_briefing_pipeline.sh", "source_telegram_env", "load_telegram_env"):
            self.assertNotIn(forbidden, quant)

    def test_workflow_reuses_exact_old_inspection_gate_and_clean_runner_command(self) -> None:
        _, quant, audit = self.workflow_jobs()
        boundary = "      - name: Verify exact main workflow and clean checkout before host inspection\n"
        def gate(job: str) -> str:
            return job.split(boundary, 1)[1].split("      - name:", 1)[0]
        self.assertEqual(gate(quant), gate(audit))
        run = quant.split("      - name: Read fixed quant runtime metadata only\n", 1)[1]
        self.assertEqual(run.strip(), 'run: |\n          set -euo pipefail\n          env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \\\n            /usr/bin/python3 -I -B \\\n            "$GITHUB_WORKSPACE/ops/quant-monitor/scripts/inspect_runtime_metadata.py"')
        self.assertLess(quant.index('"$current_main" = "$RUN_SHA"'), quant.index("inspect_runtime_metadata.py"))
        self.assertNotIn("env:", run)
        self.assertNotIn("GH_TOKEN", run)

    def test_workflow_actual_source_gate_rejects_stale_spoofed_dirty_checkout(self) -> None:
        _, quant, _ = self.workflow_jobs()
        step = quant.split("      - name: Verify exact main workflow and clean checkout", 1)[1]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
        fixtures = """
        git() {
          if [ "$1" = rev-parse ]; then printf '%s\\n' "$TEST_HEAD";
          elif [ "$1" = status ]; then printf '%s' "$TEST_DIRTY";
          else return 99; fi
        }
        gh() { printf '%s\\n' "$TEST_MAIN"; }
        timeout() { shift; "$@"; }
        """
        env = {"PATH": "/usr/bin:/bin", "RUN_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
               "RUN_REF": "refs/heads/main", "RUN_SHA": SHA, "RUN_WORKFLOW_SHA": SHA,
               "RUN_WORKFLOW_REF": "QuantStrategyLab/AIAuditBridge/.github/workflows/vps_codex_service_ops.yml@refs/heads/main",
               "TEST_HEAD": SHA, "TEST_MAIN": SHA, "TEST_DIRTY": ""}
        argv = ["/bin/bash", "-c", textwrap.dedent(fixtures) + script + "\necho INSPECTION_BOUND\n"]
        valid = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(valid.stdout.strip(), "INSPECTION_BOUND")
        failures = [("RUN_REPOSITORY", "other/repository"), ("RUN_REF", "refs/heads/spoof"),
                    ("RUN_SHA", "$(echo caller-command)"), ("RUN_WORKFLOW_SHA", "b" * 40),
                    ("RUN_WORKFLOW_REF", "QuantStrategyLab/AIAuditBridge/.github/workflows/other.yml@refs/heads/main"),
                    ("TEST_HEAD", "b" * 40), ("TEST_MAIN", "b" * 40), ("TEST_DIRTY", " M scripts/source.py")]
        for key, value in failures:
            with self.subTest(key=key):
                result = subprocess.run(argv, env={**env, key: value}, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("INSPECTION_BOUND", result.stdout)

    def test_all_four_units_are_queried_without_host_file_or_mutation_calls(self) -> None:
        values = {"codex-quant.service": service(RELEASE, "health_check.sh"),
                  "codex-daily-briefing.service": service(RELEASE, "daily_briefing_pipeline.sh"),
                  "codex-quant.timer": {"Unit": "codex-quant.service", "ActiveState": "active"},
                  "codex-daily-briefing.timer": {"Unit": "codex-daily-briefing.service", "ActiveState": "active"}}
        before = json.dumps(values, sort_keys=True)
        def mocked_run(argv: list[str], **kwargs):
            return mock.Mock(returncode=0, stdout="".join(key + "=" + value + "\n" for key, value in values[argv[3]].items()))
        with mock.patch.object(metadata.subprocess, "run", side_effect=mocked_run) as run, \
             mock.patch.object(metadata.os, "open", side_effect=AssertionError("host filesystem forbidden")):
            result = metadata.collect_metadata(query=metadata.query_systemd, reader=FakeReader())
        self.assertEqual(run.call_count, 4)
        self.assertEqual([call.args[0][3] for call in run.call_args_list], list(metadata.SERVICES) + list(metadata.TIMERS))
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], ["/usr/bin/systemctl", "--no-pager", "show"])
            self.assertEqual(len(call.args[0]), 5)
            self.assertEqual(call.kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "SYSTEMD_COLORS": "0"})
        self.assertEqual(before, json.dumps(values, sort_keys=True))
        self.assertTrue(result["services_share_code_root"])

    def test_malformed_values_are_unknown(self) -> None:
        self.assertIsNone(metadata.parse_timestamp(SECRET))
        self.assertIsNone(metadata.parse_timestamp("Mon 2026-13-05 22:30:00 UTC"))
        self.assertIsNone(metadata.parse_duration(SECRET))
        self.assertIsNone(metadata.parse_duration("1h " + SECRET))
        self.assertIsNone(metadata.parse_duration("0"))


if __name__ == "__main__":
    unittest.main()
