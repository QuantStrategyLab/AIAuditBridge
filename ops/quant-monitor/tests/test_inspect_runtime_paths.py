"""Synthetic-only fixtures; no host reads, systemd, providers or environment dump."""

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

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inspect_runtime_paths.py"
spec = importlib.util.spec_from_file_location("runtime_paths", SCRIPT)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
RUNTIME = m.base.RUNTIME_ROOT
RELEASE = m.base.RELEASE_ROOT + "/" + "a" * 40
MON = "/" + m.base.MONITOR
SECRET = "synthetic-never-output-secret"


def command(root, script, pre=False):
    return (
        "{ path=/bin/bash ; argv[]=/bin/bash "
        + root
        + MON
        + "/scripts/"
        + script
        + (" /run/quant-monitor/telegram.env" if pre else "")
        + " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


def props(unit="codex-quant.service", root=RELEASE):
    return {
        "User": "ubuntu",
        "Group": "ubuntu",
        "ActiveState": "inactive",
        "SubState": "dead",
        "LoadState": "loaded",
        "UnitFileState": "disabled",
        "ExecStart": command(root, m.base.SERVICES[unit]),
        "ExecStartPre": command(RUNTIME, "load_telegram_env.sh", True),
        "Environment": "",
        "EnvironmentFiles": "",
        "UnsetEnvironment": "",
        "PassEnvironment": "",
        "InvocationID": "b" * 32,
        "MainPID": "0",
        "ExecMainPID": "0",
        "Result": "exit-code",
        "ExecMainStatus": "1",
        "ExecMainCode": "1",
        "ExecMainExitTimestamp": "Mon 2026-10-05 04:50:00 UTC",
    }


class FakeReader:
    def __init__(self, files=None):
        self.files = files or {}
        self.requests = []

    def read(self, path, *, environment=False):
        self.requests.append((path, environment))
        if environment:
            if path not in self.files:
                raise FileNotFoundError
            value = self.files[path]
            if isinstance(value, Exception):
                raise value
            return value
        name = path.rsplit("/", 1)[-1]
        if name in m.CODE_NAMES:
            return (SCRIPT.parent / name).read_bytes()
        if name == "qpk-runtime.sha":
            return b"c" * 40 + b"\n"
        raise FileNotFoundError

    def path_metadata(self, path):
        return {"kind": "directory", "symlink_traversal": False}


def run(values=None, reader=None):
    mapping = values or {u: props(u) for u in m.base.SERVICES}
    return m.collect(query=mapping.get, reader=reader or FakeReader())


class PathInspectionTests(unittest.TestCase):
    def test_static_selection_is_separate_from_original_path_gate(self):
        p = props()
        p["Environment"] = "PATH=/unclassified/fixture:/usr/bin:/bin"
        result = run({"codex-quant.service": p})["services"]["codex-quant.service"]
        self.assertFalse(result["helper_model_gate_checks"]["path_absent_or_canonical"])
        self.assertEqual(result["helper_model_status"], "unknown")
        extra = result["static_adoption_identity"]
        self.assertEqual(extra["scope"], "next_invocation_static_selection")
        self.assertTrue(extra["not_historical_adoption"])
        self.assertFalse(extra["interpreter_invoked"])
        self.assertFalse(extra["import_execution_proof"])
        self.assertEqual(extra["roots"]["monitor"]["root"]["release_sha"], "a" * 40)
        self.assertEqual(extra["interpreter"]["stop_reason"], "metadata_unavailable")

    def test_static_selection_never_guesses_unknown_venv(self):
        p = props()
        p["Environment"] = "QUANT_MONITOR_VENV=/private/" + SECRET
        extra = run({"codex-quant.service": p})["services"]["codex-quant.service"]["static_adoption_identity"]
        self.assertEqual(extra["interpreter"]["stop_reason"], "unclassified_venv")
        self.assertNotIn(SECRET, json.dumps(extra))

    def test_static_daily_parent_captures_aab_before_telegram_override(self):
        unit = "codex-daily-briefing.service"
        p = props(unit)
        p["Environment"] = "AIAUDIT_BRIDGE_ROOT=" + RELEASE + " PATH=/usr/bin:/bin"
        reader = FakeReader({m.TELEGRAM: ("AIAUDIT_BRIDGE_ROOT=" + RUNTIME + "\n").encode()})
        extra = run({unit: p}, reader)["services"][unit]["static_adoption_identity"]
        self.assertEqual(extra["roots"]["consume_aab"]["root"]["release_sha"], "a" * 40)
        self.assertEqual(extra["import_candidates"]["root_source"], "captured_aab_with_pythonpath_dot")

    def test_static_selection_stops_for_startup_controls_and_unreviewed_helper(self):
        for change in ({"Environment": "BASH_ENV=/private/" + SECRET}, {"User": "other"}):
            p = props()
            p.update(change)
            extra = run({"codex-quant.service": p})["services"]["codex-quant.service"]["static_adoption_identity"]
            self.assertEqual(extra["status"], "unknown")
            self.assertEqual(extra["stop_reason"], "existing_non_path_precondition_unknown_or_false")
            self.assertNotIn(SECRET, json.dumps(extra))

    def test_static_interpreter_fixed_metadata_without_execution(self):
        class MetadataReader(FakeReader):
            def __init__(self):
                super().__init__()
                self.metadata_requests = []

            def identity_entry(self, path):
                self.metadata_requests.append(path)
                if path.endswith("/.venv"):
                    return {"kind": "directory"}
                if path.endswith("/.venv/bin/python"):
                    return {"kind": "symlink", "accepted_link_target": "/usr/bin/python3.12"}
                if path in ("/usr/bin/python3.12", RUNTIME + MON + "/.venv/bin/python3"):
                    return {"kind": "regular", "executable_mode": True}
                if path == "/usr/bin/python3":
                    return {"kind": "symlink", "accepted_link_target": "/usr/bin/python3.12"}
                return {"kind": "missing"}

        reader = MetadataReader()
        with mock.patch.object(m.subprocess, "run", side_effect=AssertionError("no interpreter execution")):
            chosen = m.static_interpreter(reader, "/usr/bin:/bin", RUNTIME + MON + "/.venv")
            self.assertEqual(chosen["candidate_kind"], "venv")
            overridden = m.static_interpreter(reader, "/usr/bin:/bin", RUNTIME + MON + "/.venv", common_prepend=False)
            self.assertEqual(overridden["candidate_kind"], "system")
            unknown = m.static_interpreter(reader, "/private/" + SECRET + ":/usr/bin")
            self.assertEqual(unknown["stop_reason"], "unclassified_path_prefix")
        self.assertFalse(chosen["binary_bytes_verified"])
        self.assertNotIn(SECRET, json.dumps([chosen, unknown]))
        self.assertFalse(any(SECRET in p for p in reader.metadata_requests))
        self.assertTrue(overridden["candidate_executable_metadata_verified"])

        original = reader.identity_entry
        reader.identity_entry = lambda path: {"kind": "missing"} if path == "/usr/bin/python3.12" else original(path)
        missing_target = m.static_interpreter(reader, "/usr/bin:/bin")
        self.assertEqual(missing_target["stop_reason"], "python3_target_metadata_unknown")
        self.assertFalse(missing_target["candidate_executable_metadata_verified"])

    def test_static_import_readset_is_only_direct_consumer_candidates(self):
        for path in ("client/config.py", "client/gateway_client.py", "service/provider_scenarios.py"):
            self.assertFalse(m.code_allowed(RUNTIME + "/" + path))
        for relative in m.DAILY_IMPORT_FILES + m.HEALTH_IMPORT_FILES:
            self.assertTrue(m.code_allowed(RUNTIME + "/" + relative))

    def test_static_alias_only_accepts_exact_installer_shared_target(self):
        class LinkReader:
            def __init__(self, target):
                self.target = target

            def identity_entry(self, path):
                return {"kind": "symlink", "accepted_link_target": self.target}

        venv = RELEASE + MON + "/.venv"
        expected = RUNTIME + MON + "/.venv"
        self.assertEqual(m.identity_alias(LinkReader(expected), venv, "venv"), expected)
        self.assertIsNone(m.identity_alias(LinkReader("/private/" + SECRET), venv, "venv"))
        qpk = RELEASE + MON + "/data/lifecycle-projects/QuantPlatformKit"
        self.assertEqual(m.identity_alias(LinkReader(RUNTIME + MON + "/data"), qpk, "qpk_mirror"),
                         RUNTIME + MON + "/data/lifecycle-projects/QuantPlatformKit")

    def test_identity_readset_rejects_arbitrary_paths_before_any_open(self):
        with mock.patch.object(m.os, "open", side_effect=AssertionError("must not open")):
            for path in ("/private/" + SECRET, RUNTIME + "/service/" + SECRET,
                         RELEASE + MON + "/.venv/../../private", "/usr/local/bin/python3"):
                with self.assertRaises(ValueError):
                    m.Reader().identity_entry(path)

    def test_static_identity_field_is_closed_when_entrypoint_is_unsupported(self):
        p = props()
        p["ExecStart"] = "unsupported " + SECRET
        extra = run({"codex-quant.service": p})["services"]["codex-quant.service"]["static_adoption_identity"]
        self.assertEqual(extra["stop_reason"], "entrypoint_not_supported")
        self.assertNotIn(SECRET, json.dumps(extra))

    def test_static_identity_is_unknown_if_snapshot_changes(self):
        before, after = props(), props()
        after["InvocationID"] = "c" * 32
        values = iter((before, after))
        result = m.collect(query=lambda unit: next(values) if unit == "codex-quant.service" else None,
                           reader=FakeReader())["services"]["codex-quant.service"]
        self.assertFalse(result["invocation_snapshot_stable"])
        self.assertEqual(result["static_adoption_identity"]["status"], "unknown")
        self.assertEqual(result["static_adoption_identity"]["stop_reason"],
                         "configuration_or_invocation_changed_or_unavailable")

    def test_reviewed_helper_hashes_match_public_source(self):
        for name, hashes in m.EXPECTED.items():
            self.assertIn(
                hashlib.sha256((SCRIPT.parent / name).read_bytes()).hexdigest(), hashes
            )

    def test_fixed_classes_reject_traversal_and_unknown_without_echo(self):
        for value in (
            RUNTIME,
            RELEASE,
            RUNTIME + MON,
            RELEASE + MON + "/.venv",
            m.PROJECTS,
        ):
            self.assertEqual(m.classify(value)["status"], "known")
        for value in (
            RELEASE + "/../other",
            RUNTIME + "/private/" + SECRET,
            "/tmp/" + SECRET,
        ):
            self.assertEqual(m.classify(value)["status"], "unknown")
            self.assertNotIn(SECRET, json.dumps(m.classify(value)))
        self.assertEqual(m.classify(None)["status"], "absent")
        self.assertEqual(m.classify("")["status"], "empty")

    def test_health_defaults_selected_root_and_public_hashes(self):
        result = run()
        service = result["services"]["codex-quant.service"]
        self.assertEqual(service["helper_model_status"], "supported")
        self.assertEqual(
            service["effective_paths"]["QUANT_MONITOR_ROOT"]["root"]["release_sha"],
            "a" * 40,
        )
        self.assertEqual(
            service["effective_paths"]["QUANT_MONITOR_VENV"]["status"], "absent"
        )
        self.assertEqual(service["common_selected_paths"]["venv"]["category"], "venv")
        self.assertEqual(
            service["effective_paths"]["PROJECTS_ROOT"]["status"], "unknown"
        )
        self.assertEqual(service["invocation_before"]["ExecMainStatus"], 1)
        self.assertTrue(service["configuration_snapshot_stable"])
        self.assertFalse(result["process_environment_proof"])
        self.assertFalse(result["next_invocation_environment_proof"])
        deps = result["dependencies"]["immutable_release:" + "a" * 40]["files"]
        self.assertEqual(
            deps["sync_lifecycle_artifacts.py"]["sha256"],
            hashlib.sha256(
                (SCRIPT.parent / "sync_lifecycle_artifacts.py").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(deps["qpk-runtime.sha"]["pin"], "c" * 40)

    def test_helper_model_gate_checks_path_and_user_guards(self):
        cases = (
            ("Environment", "", "path_absent_or_canonical", True),
            ("Environment", "PATH=/usr/bin:/bin", "path_absent_or_canonical", True),
            ("Environment", "PATH=/bin:/usr/bin", "path_absent_or_canonical", True),
            ("Environment", "PATH=", "path_absent_or_canonical", False),
            ("Environment", "PATH=/private/" + SECRET,
             "path_absent_or_canonical", False),
            ("PassEnvironment", "PATH", "path_absent_or_canonical", None),
            ("User", "ubuntu", "user_is_ubuntu", True),
            ("User", "", "user_is_ubuntu", False),
            ("User", SECRET, "user_is_ubuntu", False),
            ("User", None, "user_is_ubuntu", None),
        )
        for unit in m.base.SERVICES:
            for key, value, check, expected in cases:
                with self.subTest(unit=unit, key=key, value=value):
                    p = props(unit)
                    p[key] = value
                    service = run({unit: p})["services"][unit]
                    self.assertIs(service["helper_model_gate_checks"][check], expected)
                    self.assertEqual(
                        service["helper_model_status"],
                        "supported" if expected is True else "unknown",
                    )
                    if expected is not True:
                        self.assertIsNone(service["effective_paths"])
                        self.assertEqual(service["selected_call_roots"], {})

    def test_helper_model_gate_checks_each_shell_startup_guard(self):
        for unit in m.base.SERVICES:
            for control in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"):
                for key, value, expected in (
                    ("Environment", "", True),
                    ("Environment", control + "=", True),
                    ("Environment", control + "=" + SECRET, False),
                    ("PassEnvironment", control, None),
                ):
                    with self.subTest(unit=unit, control=control, value=value):
                        p = props(unit)
                        p[key] = value
                        service = run({unit: p})["services"][unit]
                        self.assertIs(
                            service["helper_model_gate_checks"][
                                control.lower() + "_absent_or_empty"
                            ],
                            expected,
                        )
                        self.assertEqual(
                            service["helper_model_status"],
                            "supported" if expected is True else "unknown",
                        )
                        if expected is not True:
                            self.assertIsNone(service["effective_paths"])

    def test_helper_model_gate_checks_helper_mismatch_and_unknown(self):
        for unit, script in m.base.SERVICES.items():
            names = [
                (script, "entrypoint_helper_reviewed"),
                ("source_telegram_env.sh", "telegram_helper_reviewed"),
            ]
            if unit == "codex-quant.service":
                names.append(("common_env.sh", "common_helper_reviewed"))
            for name, check in names:
                for expected in (False, None):
                    with self.subTest(unit=unit, name=name, expected=expected):
                        reader = FakeReader()
                        original = reader.read

                        def changed(path, **kwargs):
                            if path.endswith("/" + name):
                                if expected is None:
                                    raise FileNotFoundError
                                return b"unrecognized helper"
                            return original(path, **kwargs)

                        reader.read = changed
                        service = run({unit: props(unit)}, reader)["services"][unit]
                        self.assertIs(
                            service["helper_model_gate_checks"][check], expected
                        )
                        self.assertIsNone(service["effective_paths"])
                        self.assertEqual(service["helper_model_status"], "unknown")

    def test_observed_release_projection_can_distinguish_remaining_guards(self):
        releases = {
            "codex-quant.service": "38d172262ed60be3eb9579256649f5a1aa31f45a",
            "codex-daily-briefing.service": "1d06c4a2687a14545280855faf2cf854e524f8c7",
        }
        # These synthetic variants have the same known projection. The observed
        # snapshot did not expose which PATH/User/startup predicate blocked it.
        for check in (
            "path_absent_or_canonical", "user_is_ubuntu", "bash_env_absent_or_empty"
        ):
            with self.subTest(check=check):
                mapping = {}
                for unit, sha in releases.items():
                    root = m.base.RELEASE_ROOT + "/" + sha
                    p = props(unit, root)
                    p["EnvironmentFiles"] = m.TELEGRAM + " (ignore_errors=yes)"
                    p["Environment"] = (
                        "QUANT_MONITOR_ROOT=" + root + MON
                        + " AIAUDIT_BRIDGE_ROOT=" + root
                        + " QUANT_MONITOR_VENV=/private/" + SECRET
                        + " PATH=" + (
                            "/private/" + SECRET
                            if check == "path_absent_or_canonical"
                            else "/usr/bin:/bin"
                        )
                    )
                    if check == "user_is_ubuntu":
                        p["User"] = SECRET
                    if check == "bash_env_absent_or_empty":
                        p["Environment"] += " BASH_ENV=" + SECRET
                    mapping[unit] = p
                reader = FakeReader()
                result = run(mapping, reader)
                for unit, sha in releases.items():
                    service = result["services"][unit]
                    self.assertEqual(service["query_status"], "ok")
                    self.assertTrue(service["configuration_snapshot_stable"])
                    config = service["configuration"]
                    self.assertEqual(config["environment_syntax"], "supported")
                    self.assertFalse(config["pass_environment_affects_paths"])
                    self.assertEqual(config["unset_variables"], [])
                    file = config["environment_files"][0]
                    self.assertEqual(file["category"], "runtime_telegram")
                    self.assertEqual(file["status"], "missing")
                    self.assertTrue(file["optional"])
                    self.assertEqual(file["overrides"], [])
                    self.assertEqual(
                        service["systemd_execution_path_overrides"],
                        {"PATH": True, "PYTHONPATH": False},
                    )
                    self.assertEqual(
                        service["systemd_paths"]["QUANT_MONITOR_ROOT"]["root"][
                            "release_sha"
                        ],
                        sha,
                    )
                    self.assertEqual(
                        service["systemd_paths"]["QUANT_MONITOR_VENV"]["status"],
                        "unknown",
                    )
                    self.assertIs(service["helper_model_gate_checks"][check], False)
                    self.assertIsNone(service["effective_paths"])
                    self.assertEqual(service["selected_call_roots"], {})
                    self.assertEqual(service["helper_model_status"], "unknown")
                    self.assertTrue(all(
                        value is True
                        for key, value in service["helper_model_gate_checks"].items()
                        if key.endswith("helper_reviewed")
                    ))
                self.assertFalse(any(SECRET in path for path, _ in reader.requests))
                self.assertNotIn(SECRET, json.dumps(result))

    def test_helper_model_gate_checks_are_fixed_nonsecret_flags(self):
        p = props()
        values = ["/private/" + SECRET, SECRET]
        p["Environment"] = "PATH=" + values[0] + " BASH_ENV=" + values[0]
        p["User"] = values[1]
        result = run({"codex-quant.service": p})
        checks = result["services"]["codex-quant.service"]["helper_model_gate_checks"]
        self.assertEqual(set(checks), {
            "entrypoint_helper_reviewed", "telegram_helper_reviewed",
            "common_helper_reviewed", "path_absent_or_canonical", "user_is_ubuntu",
            "bash_env_absent_or_empty", "env_absent_or_empty",
            "shellopts_absent_or_empty", "bashopts_absent_or_empty",
        })
        self.assertTrue(all(
            value is None or type(value) is bool for value in checks.values()
        ))
        encoded = json.dumps(result)
        for value in values:
            self.assertNotIn(value, encoded)
            self.assertNotIn(hashlib.sha256(value.encode()).hexdigest(), encoded)
        p["ExecStart"] += SECRET
        service = run({"codex-quant.service": p})["services"]["codex-quant.service"]
        self.assertIsNone(service["helper_model_gate_checks"])
        self.assertEqual(service["helper_model_status"], "unknown")

    def test_environmentfile_overrides_environment_then_unset(self):
        p = props()
        p["Environment"] = (
            "QUANT_MONITOR_ROOT="
            + RELEASE
            + MON
            + " LIFECYCLE_LOCAL_ROOT="
            + RELEASE
            + MON
            + "/data/lifecycle-store GH_TOKEN="
            + SECRET
        )
        p["EnvironmentFiles"] = m.TELEGRAM + " (ignore_errors=yes)"
        p["UnsetEnvironment"] = "LIFECYCLE_LOCAL_ROOT"
        reader = FakeReader(
            {
                m.TELEGRAM: (
                    "QUANT_MONITOR_ROOT="
                    + RUNTIME
                    + MON
                    + "\nTELEGRAM_TOKEN="
                    + SECRET
                    + "\n"
                ).encode()
            }
        )
        result = run({"codex-quant.service": p}, reader)
        service = result["services"]["codex-quant.service"]
        self.assertEqual(
            service["systemd_paths"]["QUANT_MONITOR_ROOT"]["root"]["kind"],
            "runtime_checkout",
        )
        self.assertEqual(
            service["systemd_paths"]["QUANT_MONITOR_ROOT"]["provenance"],
            "systemd_environment_file",
        )
        self.assertEqual(
            service["systemd_paths"]["LIFECYCLE_LOCAL_ROOT"]["status"], "absent"
        )
        self.assertEqual(
            service["systemd_paths"]["LIFECYCLE_LOCAL_ROOT"]["provenance"],
            "systemd_unset",
        )
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(
            hashlib.sha256(SECRET.encode()).hexdigest(), json.dumps(result)
        )

    def test_telegram_root_override_keeps_initial_shell_call_root(self):
        p = props()
        p["Environment"] = "QUANT_MONITOR_ROOT=" + RELEASE + MON
        reader = FakeReader(
            {m.TELEGRAM: ("QUANT_MONITOR_ROOT=" + RUNTIME + MON + "\n").encode()}
        )
        service = run({"codex-quant.service": p}, reader)["services"][
            "codex-quant.service"
        ]
        self.assertEqual(
            service["selected_call_roots"]["shell_monitor"]["root"]["kind"],
            "immutable_release",
        )
        self.assertEqual(
            service["effective_paths"]["QUANT_MONITOR_ROOT"]["root"]["kind"],
            "runtime_checkout",
        )
        self.assertEqual(
            service["effective_paths"]["QUANT_MONITOR_ROOT"]["provenance"],
            "source_telegram_env",
        )

    def test_daily_parent_aab_is_captured_before_telegram(self):
        p = props("codex-daily-briefing.service")
        p["Environment"] = (
            "AIAUDIT_BRIDGE_ROOT=" + RELEASE + " QUANT_MONITOR_ROOT=" + RELEASE + MON
        )
        data = (
            "AIAUDIT_BRIDGE_ROOT="
            + RUNTIME
            + "\nQUANT_MONITOR_ROOT="
            + RUNTIME
            + MON
            + "\n"
        ).encode()
        service = run(
            {"codex-daily-briefing.service": p}, FakeReader({m.TELEGRAM: data})
        )["services"]["codex-daily-briefing.service"]
        self.assertEqual(
            service["selected_call_roots"]["consume_aab"]["root"]["kind"],
            "immutable_release",
        )
        self.assertEqual(
            service["selected_call_roots"]["daily_child_monitor"]["root"]["kind"],
            "runtime_checkout",
        )
        self.assertEqual(
            service["effective_paths"]["AIAUDIT_BRIDGE_ROOT"]["root"]["kind"],
            "runtime_checkout",
        )
        self.assertEqual(len(service["telegram_sources"]), 2)

    def test_daily_aab_default_is_local_alias_not_exported_environment(self):
        service = run(
            {"codex-daily-briefing.service": props("codex-daily-briefing.service")}
        )["services"]["codex-daily-briefing.service"]
        self.assertEqual(
            service["selected_call_roots"]["consume_aab"]["status"], "unknown"
        )
        self.assertEqual(
            service["effective_paths"]["AIAUDIT_BRIDGE_ROOT"]["status"], "absent"
        )
        self.assertEqual(
            service["daily_child_effective_paths"]["AIAUDIT_BRIDGE_ROOT"]["root"][
                "kind"
            ],
            "immutable_release",
        )

    def test_unsupported_shell_and_aliases_make_inference_unknown(self):
        for data in (
            b"eval something\n",
            b"QUANT_MONITOR_ROOT=$(whoami)\n",
            ("ROOT=" + RUNTIME + MON + "\n").encode(),
            b"PATH=$HOME/bin\n",
            b"BASH_ENV=/private/startup-script\n",
            b"PATH=/private/bin\n",
            b"UID=0\n",
        ):
            service = run(
                {"codex-quant.service": props()}, FakeReader({m.TELEGRAM: data})
            )["services"]["codex-quant.service"]
            self.assertEqual(service["helper_model_status"], "unknown")
            self.assertEqual(
                service["effective_paths"]["QUANT_MONITOR_ROOT"]["status"], "unknown"
            )
            self.assertEqual(
                service["selected_call_roots"]["shell_monitor"]["status"], "unknown"
            )

    def test_home_defaults_are_unknown_unless_explicit_known_home(self):
        for value, unset in (("", ""), ("HOME=", ""), ("HOME=/home/ubuntu", "HOME")):
            p = props()
            p["Environment"] = value
            p["UnsetEnvironment"] = unset
            service = run({"codex-quant.service": p})["services"]["codex-quant.service"]
            self.assertEqual(
                service["effective_paths"]["PROJECTS_ROOT"]["status"], "unknown"
            )
        p = props()
        p["Environment"] = "HOME=/home/ubuntu"
        service = run({"codex-quant.service": p})["services"]["codex-quant.service"]
        self.assertEqual(
            service["effective_paths"]["PROJECTS_ROOT"]["category"], "legacy_projects"
        )

    def test_daily_changed_child_root_still_gates_initial_child_entrypoint(self):
        p = props("codex-daily-briefing.service")
        p["Environment"] = "QUANT_MONITOR_ROOT=" + RELEASE + MON
        reader = FakeReader(
            {m.TELEGRAM: ("QUANT_MONITOR_ROOT=" + RUNTIME + MON + "\n").encode()}
        )
        original = reader.read

        def changed(path, **kwargs):
            return (
                b"unrecognized actual child entrypoint"
                if path == RELEASE + MON + "/scripts/daily_briefing.sh"
                else original(path, **kwargs)
            )

        reader.read = changed
        service = run({"codex-daily-briefing.service": p}, reader)["services"][
            "codex-daily-briefing.service"
        ]
        self.assertEqual(service["daily_child_helper_model_status"], "unknown")
        self.assertNotIn("daily_child_effective_paths", service)

    def test_workflow_new_mode_is_isolated_and_source_gate_identical(self):
        workflow = (
            SCRIPT.parents[3] / ".github/workflows/vps_codex_service_ops.yml"
        ).read_text()
        job = workflow.split("  inspect-quant-paths:\n", 1)[1]
        old = workflow.split("  inspect-quant-runtime:\n", 1)[1].split(
            "\n  inspect-audit-patch:", 1
        )[0]
        marker = "      - name: Verify exact main workflow and clean checkout before host inspection\n"

        def gate(part):
            return part.split(marker, 1)[1].split("      - name:", 1)[0]

        self.assertEqual(
            gate(job),
            gate(old).replace(
                '          [ "$(git rev-parse HEAD)" = "$RUN_SHA" ]\n          [ -z "$(git status --porcelain --untracked-files=all)" ]',
                '          checkout_head="$(git rev-parse HEAD)"\n          [ "$checkout_head" = "$RUN_SHA" ]\n          checkout_status="$(git status --porcelain --untracked-files=all)"\n          [ -z "$checkout_status" ]',
            ),
        )
        self.assertIn(
            "inputs.mode != 'inspect-quant-paths'",
            workflow.split("  inspect-quant-runtime:", 1)[0],
        )
        self.assertIn("inputs.mode == 'inspect-quant-paths'", job)
        self.assertIn("env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C", job)
        self.assertIn(" /usr/bin/python3 -I -B \\\n", job)
        self.assertIn("inspect_runtime_paths.py", job)
        for forbidden in (
            "sudo",
            "secrets.",
            "systemctl",
            "health_check.sh",
            "daily_briefing_pipeline.sh",
            "source_telegram_env.sh",
            "load_telegram_env.sh",
            "acknowledge_interruption",
        ):
            self.assertNotIn(forbidden, job)

    def test_new_actual_workflow_gate_fails_closed_on_metadata_command_failure(self):
        workflow = (
            SCRIPT.parents[3] / ".github/workflows/vps_codex_service_ops.yml"
        ).read_text()
        job = workflow.split("  inspect-quant-paths:\n", 1)[1]
        step = job.split(
            "      - name: Verify exact main workflow and clean checkout", 1
        )[1]
        gate = textwrap.dedent(
            step.split("        run: |\n", 1)[1].split("      - name:", 1)[0]
        )
        fixture = textwrap.dedent("""
            git() {
              if [ "$1" = rev-parse ]; then
                if [ "$TEST_HEAD_FAIL" = yes ]; then return 91; fi
                printf '%s\\n' "$TEST_HEAD"
              elif [ "$1" = status ]; then
                if [ "$TEST_STATUS_FAIL" = yes ]; then return 92; fi
                printf '%s' "$TEST_DIRTY"
              else return 99; fi
            }
            gh() {
              if [ "$TEST_MAIN_FAIL" = yes ]; then return 93; fi
              printf '%s\\n' "$TEST_MAIN"
            }
            timeout() { shift; "$@"; }
        """)
        sha = "a" * 40
        env = {
            "PATH": "/usr/bin:/bin",
            "RUN_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
            "RUN_REF": "refs/heads/main",
            "RUN_SHA": sha,
            "RUN_WORKFLOW_SHA": sha,
            "RUN_WORKFLOW_REF": "QuantStrategyLab/AIAuditBridge/.github/workflows/vps_codex_service_ops.yml@refs/heads/main",
            "TEST_HEAD": sha,
            "TEST_MAIN": sha,
            "TEST_DIRTY": "",
            "TEST_HEAD_FAIL": "no",
            "TEST_STATUS_FAIL": "no",
            "TEST_MAIN_FAIL": "no",
        }
        args = ["/bin/bash", "-c", fixture + gate + "\necho FIXED_INSPECTION_BOUND\n"]
        success = subprocess.run(
            args, env=env, capture_output=True, text=True, timeout=5
        )
        self.assertEqual(success.returncode, 0)
        self.assertEqual(success.stdout.strip(), "FIXED_INSPECTION_BOUND")
        failures = [
            ("TEST_HEAD_FAIL", "yes"),
            ("TEST_STATUS_FAIL", "yes"),
            ("TEST_MAIN_FAIL", "yes"),
            ("TEST_HEAD", "b" * 40),
            ("TEST_MAIN", "b" * 40),
            ("TEST_DIRTY", " M fixture.py"),
            ("RUN_REPOSITORY", "other/repository"),
            ("RUN_REF", "refs/heads/other"),
            ("RUN_SHA", "invalid"),
            ("RUN_WORKFLOW_SHA", "b" * 40),
            (
                "RUN_WORKFLOW_REF",
                "QuantStrategyLab/AIAuditBridge/.github/workflows/other.yml@refs/heads/main",
            ),
        ]
        for key, value in failures:
            with self.subTest(key=key):
                failed = subprocess.run(
                    args,
                    env={**env, key: value},
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertNotEqual(failed.returncode, 0)
                self.assertNotIn("FIXED_INSPECTION_BOUND", failed.stdout)

    def test_unrecognized_helpers_prevent_semantic_inference(self):
        reader = FakeReader()
        original = reader.read

        def changed(path, **kwargs):
            return (
                b"unrecognized helper"
                if path.endswith("/common_env.sh")
                else original(path, **kwargs)
            )

        reader.read = changed
        service = run({"codex-quant.service": props()}, reader)["services"][
            "codex-quant.service"
        ]
        self.assertIsNone(service["effective_paths"])
        self.assertEqual(service["helper_model_status"], "unknown")

    def test_unknown_env_file_is_never_read_or_echoed(self):
        p = props()
        p["EnvironmentFiles"] = "/private/" + SECRET + " (ignore_errors=yes)"
        reader = FakeReader()
        result = run({"codex-quant.service": p}, reader)
        self.assertFalse(any(SECRET in path for path, _ in reader.requests))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIsNone(result["services"]["codex-quant.service"]["effective_paths"])

    def test_unknown_source_selector_no_arbitrary_read(self):
        p = props()
        p["Environment"] = "QUANT_SENTINEL_ENV_FILE=/private/" + SECRET
        reader = FakeReader()
        result = run({"codex-quant.service": p}, reader)
        self.assertFalse(any(SECRET in path for path, _ in reader.requests))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(
            result["services"]["codex-quant.service"]["helper_model_status"], "unknown"
        )

    def test_fallback_dotenv_only_for_missing_fixed_telegram(self):
        path = RELEASE + MON + "/.env"
        reader = FakeReader(
            {
                path: (
                    "QUANT_PROJECTS_ROOT="
                    + RUNTIME
                    + MON
                    + "/data/lifecycle-projects\n"
                ).encode()
            }
        )
        service = run({"codex-quant.service": props()}, reader)["services"][
            "codex-quant.service"
        ]
        self.assertEqual(service["telegram_sources"][0]["category"], "monitor_dotenv")
        self.assertEqual(
            service["effective_paths"]["QUANT_PROJECTS_ROOT"]["root"]["kind"],
            "runtime_checkout",
        )
        denied = run(
            {"codex-quant.service": props()},
            FakeReader({m.TELEGRAM: PermissionError(), path: b""}),
        )
        self.assertEqual(
            denied["services"]["codex-quant.service"]["telegram_sources"][0][
                "category"
            ],
            "runtime_telegram",
        )

    def test_systemd_export_file_syntax_is_unknown(self):
        p = props()
        p["EnvironmentFiles"] = m.TELEGRAM + " (ignore_errors=yes)"
        result = run(
            {"codex-quant.service": p},
            FakeReader({m.TELEGRAM: b"export QUANT_MONITOR_ROOT=/not-supported\n"}),
        )
        self.assertEqual(
            result["services"]["codex-quant.service"]["configuration"][
                "environment_files"
            ][0]["status"],
            "unsupported",
        )
        self.assertIsNone(result["services"]["codex-quant.service"]["effective_paths"])

    def test_unset_matching_value_and_pass_environment(self):
        p = props()
        p["Environment"] = "QUANT_MONITOR_ROOT=" + RUNTIME + MON
        p["UnsetEnvironment"] = "QUANT_MONITOR_ROOT=" + RUNTIME + MON
        service = run({"codex-quant.service": p})["services"]["codex-quant.service"]
        self.assertEqual(
            service["systemd_paths"]["QUANT_MONITOR_ROOT"]["status"], "absent"
        )
        p["PassEnvironment"] = "QUANT_MONITOR_ROOT"
        p["Environment"] = ""
        service = run({"codex-quant.service": p})["services"]["codex-quant.service"]
        self.assertEqual(
            service["systemd_paths"]["QUANT_MONITOR_ROOT"]["status"], "unknown"
        )

    def test_explicit_environment_overrides_named_pass_environment(self):
        p = props()
        p["Environment"] = "QUANT_MONITOR_ROOT=" + RUNTIME + MON
        p["PassEnvironment"] = "QUANT_MONITOR_ROOT AIAUDIT_BRIDGE_ROOT"
        service = run({"codex-quant.service": p})["services"]["codex-quant.service"]
        self.assertEqual(
            service["systemd_paths"]["QUANT_MONITOR_ROOT"]["status"], "known"
        )
        self.assertEqual(
            service["systemd_paths"]["AIAUDIT_BRIDGE_ROOT"]["status"], "unknown"
        )

    def test_bash_env_or_unexpected_command_prevents_inference(self):
        for key, value in (
            ("Environment", "BASH_ENV=/private/" + SECRET),
            ("ExecStart", command(RELEASE, "health_check.sh") + SECRET),
            ("User", SECRET),
        ):
            p = props()
            p[key] = value
            result = run({"codex-quant.service": p})
            self.assertIsNone(
                result["services"]["codex-quant.service"]["effective_paths"]
            )
            self.assertNotIn(SECRET, json.dumps(result))

    def test_invocation_or_configuration_change_is_reported(self):
        before = props()
        after = dict(
            before,
            MainPID="22",
            InvocationID="d" * 32,
            Environment="QUANT_MONITOR_ROOT=" + RUNTIME + MON,
        )
        results = iter([before, after, None, None])
        service = m.collect(query=lambda _: next(results), reader=FakeReader())[
            "services"
        ]["codex-quant.service"]
        self.assertFalse(service["invocation_snapshot_stable"])
        self.assertFalse(service["configuration_snapshot_stable"])
        self.assertEqual(service["invocation_after"]["MainPID"], 22)

    def test_systemd_unrecognized_path_prevents_shell_semantic_model(self):
        for value in ("", "/private/bin", "$HOME/bin"):
            p = props()
            p["Environment"] = "PATH=" + value
            service = run({"codex-quant.service": p})["services"]["codex-quant.service"]
            self.assertIsNone(service["effective_paths"])
            self.assertTrue(service["systemd_execution_path_overrides"]["PATH"])
        p = props()
        p["PassEnvironment"] = "PATH"
        service = run({"codex-quant.service": p})["services"]["codex-quant.service"]
        self.assertIsNone(service["systemd_execution_path_overrides"]["PATH"])

    def test_query_has_fixed_properties_and_clean_environment(self):
        stdout = (
            "Environment=GH_TOKEN="
            + SECRET
            + "\nEnvironmentFiles=\nUnsetEnvironment=\nPassEnvironment=\nMainPID=0\n"
        )
        with mock.patch.object(
            m.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=stdout)
        ) as call:
            self.assertIsNotNone(m.query_systemd("codex-quant.service"))
            args, kwargs = call.call_args
            self.assertEqual(
                args[0],
                [
                    "/usr/bin/systemctl",
                    "--no-pager",
                    "show",
                    "codex-quant.service",
                    "--property=" + ",".join(m.PROPERTIES),
                ],
            )
            self.assertEqual(
                set(kwargs["env"]), {"PATH", "LANG", "LC_ALL", "SYSTEMD_COLORS"}
            )
            self.assertEqual(kwargs["timeout"], 5)
            self.assertIsNone(m.query_systemd(SECRET))
            self.assertEqual(call.call_count, 1)

    def test_query_errors_and_duplicate_properties_are_unknown(self):
        for proc in (
            mock.Mock(returncode=1, stdout=SECRET),
            mock.Mock(returncode=0, stdout="Environment=\nEnvironment=\n"),
            mock.Mock(returncode=0, stdout="x" * 131073),
        ):
            with mock.patch.object(m.subprocess, "run", return_value=proc):
                self.assertIsNone(m.query_systemd("codex-quant.service"))
        with mock.patch.object(
            m.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixed", 5)
        ):
            self.assertIsNone(m.query_systemd("codex-quant.service"))

    def test_safe_reader_checks_real_fixture_files_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "fixture"
            file.write_bytes(b"public fixture")
            reader = m.Reader()
            with mock.patch.object(m, "code_allowed", return_value=True):
                self.assertEqual(reader.read(str(file)), b"public fixture")
                link = root / "link"
                link.symlink_to(file)
                with self.assertRaises(OSError):
                    reader.read(str(link))
                folder = root / "folder"
                folder.mkdir()
                (folder / "code").write_bytes(b"fixture")
                alias = root / "alias"
                alias.symlink_to(folder, target_is_directory=True)
                with self.assertRaises(OSError):
                    reader.read(str(alias / "code"))
                file.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
                with self.assertRaises(OSError):
                    reader.read(str(file))
            with self.assertRaises(ValueError):
                reader.read("/private/" + SECRET)

    def test_literals_do_not_execute_and_discard_secret_keys(self):
        values, flags = m.parse_file(
            (
                "GH_TOKEN='"
                + SECRET
                + "'\nQUANT_MONITOR_ROOT='"
                + RUNTIME
                + MON
                + "'\n"
            ).encode()
        )
        self.assertFalse(flags["unsupported_syntax"])
        self.assertEqual(set(values), {"QUANT_MONITOR_ROOT"})
        self.assertIs(m.literal("$(command)"), m.UNKNOWN)
        self.assertIs(m.literal('"$HOME/Projects"'), m.UNKNOWN)

    def test_cli_rejects_arguments_without_echo(self):
        with mock.patch.object(sys, "argv", ["inspector", SECRET]):
            with self.assertRaises(SystemExit) as raised:
                m.main()
            self.assertNotIn(SECRET, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
