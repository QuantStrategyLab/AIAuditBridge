import json
import os
import re
import shlex
import textwrap
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployScriptTests(unittest.TestCase):
    def test_staging_preflight_ignores_shared_mirror_python_startup(self) -> None:
        """A clean caller must not execute Python startup code from the old mirror."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aab = root / "AIAuditBridge"
            (aab / ".git").mkdir(parents=True)
            monitor = aab / "ops" / "quant-monitor"
            (monitor / "scripts").mkdir(parents=True)
            shutil.copyfile(ROOT / "scripts" / "common_env.sh", monitor / "scripts" / "common_env.sh")
            shutil.copyfile(ROOT / "requirements-linux-py312.lock", monitor / "requirements-linux-py312.lock")
            shutil.copyfile(ROOT / "qpk-runtime.sha", monitor / "qpk-runtime.sha")
            mirror = root / "mirrors" / "QuantPlatformKit"
            (mirror / ".git").mkdir(parents=True)
            (mirror / "src").mkdir()
            marker = root / "old-mirror-startup-executed"
            (mirror / "src" / "sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('synthetic startup only')\n",
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            source_sha = "a" * 40
            git = fake_bin / "git"
            git.write_text(
                "#!/bin/bash\nset -euo pipefail\n"
                '[[ "$1" == -C ]] || exit 90\nshift 2\n'
                'case "$1" in\n'
                "status) exit 0 ;;\n"
                f"rev-parse) printf '%s\\n' '{source_sha}' ;;\n"
                'show) cat "$QUANT_MONITOR_ROOT/requirements-linux-py312.lock" ;;\n'
                "cat-file) exit 1 ;;\n"
                "*) exit 91 ;;\nesac\n",
                encoding="utf-8",
            )
            git.chmod(0o755)
            python = fake_bin / "python3"
            # Execute only setup's standard-library platform preflight. No venv/pip.
            python.write_text(
                "#!/bin/bash\nset -euo pipefail\n"
                '[[ "$*" == - || "$*" == "-I -" ]] || exit 92\n'
                f'exec "{sys.executable}" "$@"\n',
                encoding="utf-8",
            )
            python.chmod(0o755)
            stage = root / "new-candidate-venv"
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), source_sha, str(aab)],
                env={
                    "HOME": str(root), "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "LANG": "C", "LC_ALL": "C", "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "",
                    "QUANT_MONITOR_ROOT": str(monitor), "AIAUDIT_BRIDGE_ROOT": str(aab),
                    "QUANT_PLATFORM_KIT_ROOT": str(mirror),
                    "QUANT_PROJECTS_ROOT": str(mirror.parent),
                    "LIFECYCLE_LOCAL_ROOT": str(root / "lifecycle-store"),
                    "QUANT_MONITOR_VENV": str(stage),
                },
                capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("pinned QuantPlatformKit commit is unavailable", result.stderr)
            self.assertFalse(stage.exists(), "fixture must stop before creating any venv")
            self.assertFalse(marker.exists(), "shared mirror startup ran before the missing-commit refusal")


    def test_staging_pip_and_backend_environment_ignore_shared_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aab = root / "AIAuditBridge"
            (aab / ".git").mkdir(parents=True)
            monitor = aab / "ops/quant-monitor"
            (monitor / "scripts").mkdir(parents=True)
            for rel in ("scripts/common_env.sh", "requirements-linux-py312.lock", "qpk-runtime.sha"):
                shutil.copyfile(ROOT / rel, monitor / rel)
            mirror = root / "mirrors/QuantPlatformKit"
            (mirror / ".git").mkdir(parents=True)
            (mirror / "src/pip").mkdir(parents=True)
            (mirror / "src/pip/__init__.py").write_text("")
            (mirror / "src/pip/__main__.py").write_text("raise SystemExit('must not execute')\n")
            marker = root / "startup"
            (mirror / "src/sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            )
            receipt = root / "pip-selection.json"
            probe = (
                "import importlib.util, json, os, sys\nfrom pathlib import Path\n"
                "spec = importlib.util.find_spec('pip')\n"
                f"old = bool(spec and Path(spec.origin).is_relative_to({str(mirror)!r}))\n"
                f"Path({str(receipt)!r}).write_text(json.dumps({{"
                "'old_mirror_pip': old, 'isolated': sys.flags.isolated, "
                "'pythonpath_in_child_env': 'PYTHONPATH' in os.environ, "
                "'argv': sys.argv[1:]}))\n"
                "raise SystemExit(17)\n"
            )
            pip_probe = (
                "#!/bin/bash\nset -euo pipefail\nargs=()\n"
                'if [[ "${1:-}" == -I ]]; then args=(-I); shift; fi\n'
                '[[ "$1" == -m && "$2" == pip && "$3" == install ]] || exit 93\n'
                f'exec {shlex.quote(sys.executable)} "${{args[@]}}" -c {shlex.quote(probe)} "$@"\n'
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            sha = "a" * 40
            git = fake_bin / "git"
            git.write_text(
                "#!/bin/bash\nset -euo pipefail\n"
                '[[ "$1" == -C ]] || exit 90\nshift 2\ncase "$1" in\n'
                "status|cat-file) exit 0 ;;\n"
                f"rev-parse) printf '%s\\n' '{sha}' ;;\n"
                'show) cat "$QUANT_MONITOR_ROOT/requirements-linux-py312.lock" ;;\n'
                "*) exit 91 ;;\nesac\n"
            )
            git.chmod(0o755)
            python = fake_bin / "python3"
            python.write_text(
                "#!/bin/bash\nset -euo pipefail\nargs=()\n"
                'if [[ "${1:-}" == -I ]]; then args=(-I); shift; fi\n'
                f'if [[ "$*" == - ]]; then exec {shlex.quote(sys.executable)} "${{args[@]}}" -; fi\n'
                '[[ "$#" == 3 && "$1" == -m && "$2" == venv ]] || exit 92\n'
                'mkdir "$3/bin"\n'
                f'printf %s {shlex.quote(pip_probe)} > "$3/bin/python"\n'
                'chmod 755 "$3/bin/python"\n'
            )
            python.chmod(0o755)
            stage = root / "candidate-placeholder"
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "scripts/setup_vps_runtime.sh"), sha, str(aab)],
                env={
                    "HOME": str(root), "PATH": f"{fake_bin}:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                    "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "",
                    "QUANT_MONITOR_ROOT": str(monitor), "AIAUDIT_BRIDGE_ROOT": str(aab),
                    "QUANT_PLATFORM_KIT_ROOT": str(mirror), "QUANT_PROJECTS_ROOT": str(mirror.parent),
                    "LIFECYCLE_LOCAL_ROOT": str(root / "lifecycle-store"), "QUANT_MONITOR_VENV": str(stage),
                },
                capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("locked dependency install failed", result.stderr)
            self.assertNotIn("[setup] ok", result.stdout)
            observed = json.loads(receipt.read_text())
            self.assertEqual(observed["argv"][:-1], ["-m", "pip", "install", "--require-hashes", "--only-binary=:all:", "-r"])
            self.assertEqual(observed["isolated"], 1)
            self.assertFalse(observed["old_mirror_pip"])
            self.assertFalse(observed["pythonpath_in_child_env"])
            self.assertFalse(marker.exists())

    def test_common_env_separates_code_and_lifecycle_data_roots(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            env = os.environ.copy()
            for name in (
                "PROJECTS_ROOT",
                "QUANT_PROJECTS_ROOT",
                "QUANT_PLATFORM_KIT_ROOT",
                "LIFECYCLE_LOCAL_ROOT",
            ):
                env.pop(name, None)
            env["HOME"] = home
            monitor_root = Path(home) / "monitor"
            monitor_root.mkdir()
            env["QUANT_MONITOR_ROOT"] = str(monitor_root)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    (
                        f"source {ROOT / 'scripts' / 'common_env.sh'}; "
                        'printf "%s\\n%s\\n%s\\n%s\\n" '
                        '"${PROJECTS_ROOT-}" "${QUANT_PROJECTS_ROOT-}" '
                        '"${LIFECYCLE_LOCAL_ROOT-}" "${QUANT_PLATFORM_KIT_ROOT-}"'
                    ),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertEqual(
            result.stdout.splitlines(),
            [
                str(Path(home) / "Projects"),
                str(monitor_root / "data" / "lifecycle-projects"),
                str(monitor_root / "data" / "lifecycle-store"),
                str(monitor_root / "data" / "lifecycle-projects" / "QuantPlatformKit"),
            ],
        )

    def _inactive_stage_scripts(self) -> tuple[str, list[str]]:
        workflow = (ROOT.parents[1] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        job = workflow.split("\n  stage-quant-runtime-inactive:\n", 1)[1].split("\n  release-gateway-failure-repairs:\n", 1)[0]
        scripts = [textwrap.dedent(block) for block in re.findall(r"(?m)^        run: \|\n((?:^          .*\n|^\n)*)", job)]
        self.assertEqual(len(scripts), 3)
        return workflow, scripts

    def test_inactive_stage_is_a_separate_fixed_mode_without_service_operations(self) -> None:
        workflow, scripts = self._inactive_stage_scripts()
        job = workflow.split("\n  stage-quant-runtime-inactive:\n", 1)[1].split("\n  release-gateway-failure-repairs:\n", 1)[0]
        generic = workflow.split("\n  vps-codex-service-ops:\n", 1)[1].split("\n  inspect-recorded-daily-errors:\n", 1)[0]
        self.assertIn("inputs.mode != 'stage-quant-runtime-inactive'", generic)
        self.assertIn("inputs.mode == 'stage-quant-runtime-inactive'", job)
        self.assertIn("!inputs.acknowledge_interruption && inputs.ssh_unban_ip == ''", job)
        self.assertIn("environment: codex-vps-ops", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn("ref: ${{ github.sha }}", job)
        self.assertNotRegex(job, r"systemctl|sudo|daemon-reload|health_check\.sh|daily_briefing_pipeline\.sh|deploy_codex|install_immutable_release|sync_strategy_repos")
        self.assertNotRegex(scripts[1], r"\brm\b|\bchmod\b|\bchown\b|\bgit\b")
        self.assertIn('env -i "${transport[@]}"', scripts[1])
        self.assertIn('"$candidate/bin/python" -I -B -', scripts[2])
        self.assertNotIn("import quant_platform_kit", scripts[2])

    def test_inactive_stage_gate_rejects_wrong_source_event_or_inputs(self) -> None:
        _, scripts = self._inactive_stage_scripts()
        sha = "a" * 40
        env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C", "LC_ALL": "C",
               "GITHUB_WORKSPACE": str(ROOT.parents[1]), "RUN_EVENT_NAME": "workflow_dispatch",
               "RUN_MODE": "stage-quant-runtime-inactive", "RUN_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
               "RUN_REF": "refs/heads/main", "RUN_ACK": "false", "RUN_UNBAN_IP": "",
               "RUN_SHA": sha, "RUN_WORKFLOW_SHA": sha,
               "RUN_WORKFLOW_REF": "QuantStrategyLab/AIAuditBridge/.github/workflows/vps_codex_service_ops.yml@refs/heads/main",
               "FIXTURE_HEAD": sha, "FIXTURE_MAIN": sha, "FIXTURE_DIRTY": ""}
        stubs = (
            'git() { case "$*" in "rev-parse HEAD") printf "%s\\n" "$FIXTURE_HEAD" ;; '
            '"status --porcelain --untracked-files=all") printf %s "$FIXTURE_DIRTY" ;; *) return 90 ;; esac; }\n'
            'gh() { [[ "$*" == "api repos/QuantStrategyLab/AIAuditBridge/git/ref/heads/main --jq .object.sha" ]] || return 91; '
            'printf "%s\\n" "$FIXTURE_MAIN"; }\n'
            'timeout() { [[ "$1" == 30s ]] || return 92; shift; "$@"; }\n'
        )
        for overrides in ({}, {"FIXTURE_MAIN": "b" * 40}, {"FIXTURE_HEAD": "b" * 40},
                          {"RUN_WORKFLOW_SHA": "b" * 40}, {"RUN_EVENT_NAME": "push"},
                          {"RUN_MODE": "deploy"}, {"RUN_REF": "refs/heads/other"},
                          {"RUN_ACK": "true"}, {"RUN_UNBAN_IP": "192.0.2.1"},
                          {"FIXTURE_DIRTY": " M unreviewed"}, {"RUN_SHA": "main"}):
            with self.subTest(overrides=overrides):
                result = subprocess.run(["/bin/bash", "-c", stubs + scripts[0]], env={**env, **overrides},
                                        capture_output=True, text=True, timeout=5, check=False)
                self.assertEqual(result.returncode == 0, not overrides, result.stderr)

    def test_inactive_stage_clean_environment_residue_and_setup_failure(self) -> None:
        _, scripts = self._inactive_stage_scripts()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "checkout"
            setup = workspace / "ops/quant-monitor/scripts/setup_vps_runtime.sh"
            setup.parent.mkdir(parents=True)
            candidate = root / "new-candidate"
            runtime = root / "shared-runtime"
            runtime.mkdir()
            shared_marker = runtime / "preserved"
            shared_marker.write_text("original")
            receipt = root / "received-env"
            stage = scripts[1].replace("/home/ubuntu/quant-monitor-data03-286757-py312-v1", str(candidate))
            stage = stage.replace("/home/ubuntu/quant-monitor-runtime/AIAuditBridge/ops/quant-monitor", str(runtime))
            stage = stage.replace("HOME=/home/ubuntu", f"HOME={root}")
            captured_names = ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "BASH_ENV", "UNRELATED_SECRET",
                              "PYTHONNOUSERSITE", "PIP_CONFIG_FILE", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL",
                              "PIP_TRUSTED_HOST", "HTTPS_PROXY", "REQUESTS_CA_BUNDLE", "GH_TOKEN")
            capture = "\n".join(f'printf "%s=%s\\n" {name} "${{{name}-unset}}"' for name in captured_names)
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "GITHUB_WORKSPACE": str(workspace),
                   "RUN_SHA": "a" * 40, "GH_TOKEN": "synthetic-token",
                   "HTTPS_PROXY": "https://proxy.invalid", "REQUESTS_CA_BUNDLE": "/synthetic/ca.pem"}
            # These values are introduced after the fixture shell starts. Only the
            # production env-i boundary, not fixture shell startup, receives them.
            poison = "export PYTHONPATH=/synthetic/old PYTHONHOME=/synthetic/home PYTHONUSERBASE=/synthetic/user BASH_ENV=/synthetic/bash UNRELATED_SECRET=synthetic PIP_EXTRA_INDEX_URL=https://unreviewed.invalid PIP_TRUSTED_HOST=unreviewed.invalid\n"

            def run_setup(exit_code: int) -> subprocess.CompletedProcess:
                setup.write_text("#!/bin/bash\nset -euo pipefail\n{\n" + capture +
                                 f"\n}} > {shlex.quote(str(receipt))}\n" +
                                 'mkdir "$QUANT_MONITOR_VENV"\n' +
                                 f'printf partial > "$QUANT_MONITOR_VENV/residue"\nexit {exit_code}\n')
                return subprocess.run(["/bin/bash", "-c", poison + stage], env=env,
                                      capture_output=True, text=True, timeout=5, check=False)

            failed = run_setup(17)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("residue is unaccepted and preserved", failed.stderr)
            values = dict(line.split("=", 1) for line in receipt.read_text().splitlines())
            for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "BASH_ENV", "UNRELATED_SECRET",
                         "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST"):
                self.assertEqual(values[name], "unset")
            self.assertEqual(values["PYTHONNOUSERSITE"], "1")
            self.assertEqual(values["PIP_CONFIG_FILE"], "/dev/null")
            self.assertEqual(values["PIP_INDEX_URL"], "https://pypi.org/simple")
            self.assertEqual(values["HTTPS_PROXY"], env["HTTPS_PROXY"])
            self.assertEqual(values["REQUESTS_CA_BUNDLE"], env["REQUESTS_CA_BUNDLE"])
            self.assertEqual(values["GH_TOKEN"], "synthetic-token")
            receipt.unlink()
            refused = run_setup(0)
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(receipt.exists())
            self.assertEqual((candidate / "residue").read_text(), "partial")
            self.assertEqual(shared_marker.read_text(), "original")
            # A distinct fresh fixture directory demonstrates successful setup;
            # production has no directory override or automatic retry.
            stage = stage.replace(str(candidate), str(root / "fresh-candidate"))
            self.assertEqual(run_setup(0).returncode, 0)
            self.assertEqual(shared_marker.read_text(), "original")

    def test_inactive_stage_failure_codes_preserve_status_and_never_emit_raw_output(self) -> None:
        _, scripts = self._inactive_stage_scripts()
        known = {
            "[setup] pinned QuantPlatformKit commit is unavailable in the existing mirror; staging refused without mirror changes": "qpk_commit_unavailable",
            "[setup] requirements-linux-py312.lock only covers CPython 3.12 on Linux x86_64 with glibc >= 2.34; refusing unsupported environment (no silent fallback)": "unsupported_platform",
            "[setup] locked dependency install failed": "locked_dependency_install_failed",
            "[setup] QuantPlatformKit install failed": "qpk_install_failed",
            "[setup] pip check failed after locked install": "pip_check_failed",
            "[setup] gh CLI is required for trusted lifecycle artifact synchronization": "gh_unavailable",
            "[setup] gh CLI authentication is required for lifecycle artifacts": "gh_auth_unavailable",
        }
        secret_text = "synthetic-secret-DO-NOT-EMIT https://proxy.invalid/password /private/fixture/path"
        cases = [(message, reason, 17) for message, reason in known.items()]
        cases.extend([
            (secret_text, "unknown", 42),
            ("[setup] locked dependency install failed " + secret_text, "unknown", 9),
            ("[setup] pip check failed after locked install", None, 0),
        ])
        cases = [(*case, 0) for case in cases]
        cases.append(("[setup] pip check failed after locked install", "unknown", 0, 23))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "checkout"
            setup = workspace / "ops/quant-monitor/scripts/setup_vps_runtime.sh"
            setup.parent.mkdir(parents=True)
            for number, (message, reason, exit_code, filter_exit) in enumerate(cases):
                with self.subTest(reason=reason, exit_code=exit_code, filter_exit=filter_exit):
                    candidate = root / f"candidate-{number}"
                    stage = scripts[1].replace("/home/ubuntu/quant-monitor-data03-286757-py312-v1", str(candidate))
                    stage = stage.replace("HOME=/home/ubuntu", f"HOME={root}")
                    if filter_exit:
                        self.assertEqual(stage.count("return 0"), 1)
                        stage = stage.replace("return 0", f"return {filter_exit}")
                    # Real setup is not executed: only fixture output and residue.
                    payload = secret_text + "\n" + message + "\n"
                    setup.write_text("#!/bin/bash\nset -euo pipefail\n"
                                     'mkdir "$QUANT_MONITOR_VENV"\n'
                                     'printf original > "$QUANT_MONITOR_VENV/residue"\n'
                                     f"printf %s {shlex.quote(payload)} >&2\nexit {exit_code}\n")
                    result = subprocess.run(
                        ["/bin/bash", "-c", "export PYTHONPATH=/synthetic/old\n" + stage],
                        env={"PATH": "/usr/bin:/bin", "HOME": str(root), "GH_TOKEN": "synthetic-token",
                             "GITHUB_WORKSPACE": str(workspace), "RUN_SHA": "a" * 40},
                        capture_output=True, text=True, timeout=5, check=False,
                    )
                    self.assertEqual(result.returncode, exit_code or filter_exit)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn(secret_text, result.stderr)
                    self.assertNotIn("proxy.invalid", result.stderr)
                    self.assertNotIn("/private/", result.stderr)
                    self.assertNotIn("[setup]", result.stderr)
                    self.assertNotIn("inactive_candidate_metadata_verified", result.stdout + result.stderr)
                    self.assertEqual((candidate / "residue").read_text(), "original")
                    if reason:
                        self.assertEqual(result.stderr,
                                         f"[inactive-stage] setup_failure={reason}; candidate residue is unaccepted and preserved\n")
                    else:
                        self.assertEqual(result.stderr, "")

    def test_inactive_stage_metadata_rejects_wrong_version_path_hash_and_interpreter(self) -> None:
        import ast
        import contextlib
        import hashlib
        import importlib.metadata
        import io
        from types import SimpleNamespace

        _, scripts = self._inactive_stage_scripts()
        code = scripts[2].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        hashes = next(ast.literal_eval(node.value) for node in ast.walk(ast.parse(code))
                      if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "hashes" for t in node.targets))
        self.assertEqual(set(hashes), {"drift_detector.py", "health_dashboard.py", "performance_monitor.py", "return_collector.py"})
        versions = dict(re.findall(r"(?m)^([A-Za-z0-9_-]+)==([^ ]+)", (ROOT / "requirements-linux-py312.lock").read_text()))
        versions["quant-platform-kit"] = "1.0.0"
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "candidate"
            site = prefix / "lib/python3.12/site-packages"
            package = site / "quant_platform_kit/strategy_lifecycle"
            package.mkdir(parents=True)
            for name in hashes:
                (package / name).write_text(name)

            def fixture_hash(data):
                # Synthetic package bytes, with the production four-hash comparison
                # intact. Unrecognized bytes deliberately never match.
                return SimpleNamespace(hexdigest=lambda: hashes.get(data.decode(), "0" * 64))

            for scenario in ("valid", "wrong_version", "outside_prefix", "wrong_hash", "not_isolated", "wrong_prefix"):
                with self.subTest(scenario=scenario):
                    def distribution(name):
                        version = versions[name]
                        if scenario == "wrong_version" and name == "pandas":
                            version = "0.0.0"
                        location = Path(tmp) / "old-mirror" if scenario == "outside_prefix" and name == "quant-platform-kit" else site
                        return SimpleNamespace(version=version, locate_file=lambda rel: location / rel)

                    (package / "return_collector.py").write_text("mismatch" if scenario == "wrong_hash" else "return_collector.py")
                    stdout = io.StringIO()
                    argv = ["candidate-check", str(prefix), str(ROOT / "requirements-linux-py312.lock"), "a" * 40]
                    flags = SimpleNamespace(isolated=0 if scenario == "not_isolated" else 1, no_user_site=1)
                    with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "prefix", str(prefix if scenario != "wrong_prefix" else Path(tmp) / "old-venv")), \
                         mock.patch.object(sys, "base_prefix", "/synthetic/base"), mock.patch.object(sys, "flags", flags), \
                         mock.patch.object(sys, "version_info", (3, 12, 0)), \
                         mock.patch.object(importlib.metadata, "distribution", distribution), \
                         mock.patch.object(hashlib, "sha256", fixture_hash), contextlib.redirect_stdout(stdout):
                        if scenario == "valid":
                            exec(compile(code, "<inactive-candidate-check>", "exec"), {})
                        else:
                            with self.assertRaisesRegex(SystemExit, "candidate metadata check failed; residue preserved"):
                                exec(compile(code, "<inactive-candidate-check>", "exec"), {})
                    if scenario == "valid":
                        result = json.loads(stdout.getvalue())
                        self.assertEqual(result["locked_versions_checked"], 27)
                        self.assertEqual(result["qpk_module_hashes_checked"], 4)
                        self.assertFalse(result["application_import_proof"])
                        self.assertFalse(result["runtime_adoption_proof"])
                    else:
                        self.assertEqual(stdout.getvalue(), "")

    def test_strategy_sync_uses_dedicated_mirrors(self) -> None:
        script = (ROOT / "scripts" / "sync_strategy_repos.sh").read_text(encoding="utf-8")
        pin = (ROOT / "qpk-runtime.sha").read_text(encoding="utf-8").strip()

        self.assertRegex(pin, r"^[0-9a-f]{40}$")
        self.assertIn('MIRROR_ROOT="${QUANT_PROJECTS_ROOT:-$ROOT/data/lifecycle-projects}"', script)
        self.assertIn('dir="$MIRROR_ROOT/$repo"', script)
        self.assertIn('QPK_RUNTIME_SHA="$(<"$ROOT/qpk-runtime.sha")"', script)
        self.assertIn('fetch origin "$QPK_RUNTIME_SHA"', script)
        self.assertIn('checkout --detach --quiet "$QPK_RUNTIME_SHA"', script)
        self.assertIn("checkout --detach --quiet origin/main", script)
        self.assertIn('[[ "$repo" == "QuantPlatformKit" ]]', script)
        self.assertNotIn("pull --ff-only", script)

    def test_health_check_sources_telegram_env_after_common_env(self) -> None:
        script = (ROOT / "scripts" / "health_check.sh").read_text(encoding="utf-8")

        self.assertIn('source "$ROOT/scripts/common_env.sh"', script)
        self.assertIn(
            'source "$ROOT/scripts/source_telegram_env.sh" 2>/dev/null || true',
            script,
        )
        self.assertLess(
            script.index('source "$ROOT/scripts/common_env.sh"'),
            script.index('source "$ROOT/scripts/source_telegram_env.sh"'),
        )

    def test_health_check_syncs_artifacts_before_monitoring(self) -> None:
        script = (ROOT / "scripts" / "health_check.sh").read_text(encoding="utf-8")

        self.assertLess(
            script.index("sync_lifecycle_artifacts.py"),
            script.index("health_cycle.py"),
        )
        self.assertLess(
            script.index("sync_binance_live_runs.py"),
            script.index("health_cycle.py"),
        )
        self.assertIn('binance_live_runs_status=$?', script)
        self.assertIn('[[ "$binance_live_runs_status" -ne 0 && "$binance_live_runs_status" -ne 2 ]]', script)

    def test_health_check_publishes_the_snapshot_before_returning_an_alert_status(self) -> None:
        script = (ROOT / "scripts" / "health_check.sh").read_text(encoding="utf-8")

        self.assertIn('health_cycle_status=$?', script)
        self.assertLess(
            script.index("health_cycle.py"),
            script.index("publish_strategy_health.sh"),
        )
        self.assertIn('exit "$health_cycle_status"', script)

    def test_deploy_script_normalizes_chat_id_and_requires_one_fixed_source(self) -> None:
        script = (ROOT / "scripts" / "deploy_to_vps.sh").read_text(encoding="utf-8")

        self.assertIn(
            "s/^Environment=(GLOBAL_TELEGRAM_CHAT_ID=)+//",
            script,
        )
        self.assertIn('[[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]', script)
        self.assertIn('"$REMOTE_MAIN_SHA" != "$SOURCE_SHA"', script)
        self.assertIn('checkout --detach "$SOURCE_SHA"', script)
        self.assertIn('bash "$REMOTE_MONITOR/scripts/setup_vps_runtime.sh" "$SOURCE_SHA" "$REMOTE_AAB"', script)
        self.assertIn('status --porcelain --untracked-files=all -- client scripts service ops/quant-monitor/scripts ops/quant-monitor/systemd', script)
        self.assertNotIn("rsync", script)

    def test_runtime_setup_requires_exact_checkout_and_never_resyncs_aab_main(self) -> None:
        script = (ROOT / "scripts" / "setup_vps_runtime.sh").read_text(encoding="utf-8")
        pin = (ROOT / "qpk-runtime.sha").read_text(encoding="utf-8").strip()
        aab_sync = script[script.index('if [[ ! -d "$AAB_ROOT/.git" ]]'):script.index('VENV=')]
        self.assertRegex(pin, r"^[0-9a-f]{40}$")
        self.assertIn('SOURCE_SHA="${1:-}"', script)
        self.assertIn('EXPECTED_AAB_ROOT="${2:-}"', script)
        self.assertIn('[[ "$AAB_ROOT" != "$EXPECTED_AAB_ROOT" ]]', script)
        self.assertIn('rev-parse HEAD', aab_sync)
        self.assertIn('"$ACTUAL_AAB_SHA" != "$SOURCE_SHA"', aab_sync)
        self.assertIn('status --porcelain --untracked-files=all -- "${DIRTY_PATHSPEC[@]}"', aab_sync)
        self.assertIn("ops/quant-monitor/qpk-runtime.sha", aab_sync)
        self.assertIn("ops/quant-monitor/requirements-linux-py312.lock", aab_sync)
        self.assertIn('QPK_RUNTIME_SHA="$(<"$ROOT/qpk-runtime.sha")"', script)
        self.assertIn("qpk_created=0", script)
        self.assertIn("qpk_created=1", script)
        self.assertIn('[[ "$qpk_created" -eq 0 ]]', script)
        self.assertIn('fetch origin "$QPK_RUNTIME_SHA"', script)
        self.assertIn('checkout --detach --quiet "$QPK_RUNTIME_SHA"', script)
        self.assertIn('"$ACTUAL_QPK_SHA" != "$QPK_RUNTIME_SHA"', script)
        self.assertIn('requirements-linux-py312.lock', script)
        self.assertIn('platform.python_implementation()', script)
        self.assertIn('unsupported Python implementation', script)
        self.assertIn("--require-hashes", script)
        self.assertIn("--only-binary=:all:", script)
        self.assertIn("--no-deps", script)
        self.assertIn("--no-build-isolation", script)
        self.assertIn("pip check", script)
        self.assertNotIn("install -U pip wheel", script)
        self.assertNotIn("install numpy pandas google-cloud-storage", script)
        self.assertLess(
            script.index("requirements-linux-py312.lock only covers"),
            script.index('python3 "${PYTHON_ISOLATION_ARGS[@]}" -m venv "$VENV"'),
        )
        self.assertLess(
            script.index("requirements-linux-py312.lock only covers"),
            script.index('cloning QuantPlatformKit'),
        )
        self.assertLess(
            script.index('[[ "$qpk_created" -eq 0 ]]'),
            script.index('checkout --detach --quiet "$QPK_RUNTIME_SHA"'),
        )
        self.assertNotIn('fetch origin main', script)
        self.assertNotIn('checkout --detach --quiet origin/main', script)
        self.assertNotIn('fetch origin main', aab_sync)
        self.assertNotIn('checkout main', aab_sync)
        self.assertNotIn('pull --ff-only', aab_sync)

    def test_deploy_script_only_accepts_monitor_alert_exit(self) -> None:
        script = (ROOT / "scripts" / "deploy_to_vps.sh").read_text(encoding="utf-8")

        self.assertNotIn("sudo systemctl start codex-quant.service || true", script)
        self.assertIn("ExecMainStatus", script)
        self.assertIn('if [[ "$monitor_status" != "2" ]]', script)

    def test_deploy_script_passes_fixed_sha_and_path_to_remote_without_local_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            capture = root / "capture"
            fake_bin.mkdir()
            capture.mkdir()
            ssh = fake_bin / "ssh"
            ssh.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "count=0\n"
                "[[ ! -f \"$SSH_CAPTURE_DIR/count\" ]] || count=$(<\"$SSH_CAPTURE_DIR/count\")\n"
                "count=$((count + 1))\n"
                "printf '%s' \"$count\" > \"$SSH_CAPTURE_DIR/count\"\n"
                "printf '%s\\n' \"$@\" > \"$SSH_CAPTURE_DIR/args.$count\"\n"
                "cat > \"$SSH_CAPTURE_DIR/stdin.$count\"\n"
                "if [[ \"$count\" -le 2 ]]; then "
                "mkdir -p \"$SSH_CAPTURE_DIR/repo/.git\"; shift 3; "
                "set -- \"$1\" \"$2\" \"$3\" \"$4\" \"$SSH_CAPTURE_DIR/repo\"; "
                "sed \"s#/home/ubuntu/quant-monitor-runtime/AIAuditBridge#$SSH_CAPTURE_DIR/repo#g\" \"$SSH_CAPTURE_DIR/stdin.$count\" > \"$SSH_CAPTURE_DIR/remote.$count\"; "
                "\"$@\" < \"$SSH_CAPTURE_DIR/remote.$count\"; fi\n",
                encoding="utf-8",
            )
            ssh.chmod(0o755)
            git = fake_bin / "git"
            git.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$*\" >> \"$GIT_CAPTURE_FILE\"\n"
                "while [[ \"${1:-}\" == -C ]]; do shift 2; done\n"
                "if [[ \"${FAIL_FETCH:-0}\" == 1 && \"$1\" == fetch ]]; then exit 42; fi\n"
                "if [[ \"${DIRTY_RUNTIME_SOURCE:-0}\" == 1 && \"$1\" == status ]]; then printf '%s\\n' ' M service/briefing_consumer.py'; exit 0; fi\n"
                "if [[ \"$1\" == rev-parse && \"$2\" == origin/main ]]; then printf '%s\\n' \"$REMOTE_MAIN_SHA\"; exit 0; fi\n"
                "case \"$1\" in status|fetch|clone|checkout) exit 0 ;; esac\n"
                "exit 90\n",
                encoding="utf-8",
            )
            git.chmod(0o755)
            mkdir = fake_bin / "mkdir"
            mkdir.write_text("#!/usr/bin/env bash\nexec /bin/mkdir \"$@\"\n", encoding="utf-8")
            mkdir.chmod(0o755)
            fake_bash = fake_bin / "bash"
            fake_bash.write_text(
                "#!/bin/bash\nset -euo pipefail\n"
                "if [[ \"${1:-}\" == -s ]]; then exec /bin/bash \"$@\"; fi\n"
                "if [[ \"${1:-}\" == */setup_vps_runtime.sh ]]; then "
                "printf '%s\\n%s\\n%s\\n' \"${AIAUDIT_BRIDGE_ROOT-}\" \"$2\" \"$3\" > \"$SETUP_CAPTURE_FILE\"; exit 0; fi\n"
                "exec /bin/bash \"$@\"\n",
                encoding="utf-8",
            )
            fake_bash.chmod(0o755)
            for name, body in {
                "sudo": "#!/bin/bash\nexit 0\n",
                "systemctl": "#!/bin/bash\nif [[ \"${1:-}\" == is-active ]]; then echo active; elif [[ \"${1:-}\" == show ]]; then echo /bin/true; fi\nexit 0\n",
                "cp": "#!/bin/bash\nexit 0\n",
            }.items():
                stub = fake_bin / name
                stub.write_text(body, encoding="utf-8")
                stub.chmod(0o755)
            source_sha = "a" * 40
            run_number = 0
            def run_with_remote_main(remote_main_sha: str, **overrides: str):
                nonlocal run_number
                run_number += 1
                env = os.environ.copy()
                env["PATH"] = f"{fake_bin}:{env.get('PATH', '/usr/bin:/bin')}"
                ssh_capture_dir = root / f"ssh-{run_number}"
                ssh_capture_dir.mkdir()
                env["SSH_CAPTURE_DIR"] = str(ssh_capture_dir)
                env["GIT_CAPTURE_FILE"] = str(capture / "git-args")
                env["SETUP_CAPTURE_FILE"] = str(ssh_capture_dir / "setup.args")
                env["AIAUDIT_BRIDGE_SOURCE_SHA"] = source_sha
                env["REMOTE_MAIN_SHA"] = remote_main_sha
                env.update(overrides)
                return subprocess.run(
                    ["bash", str(ROOT / "scripts" / "deploy_to_vps.sh")],
                    cwd=ROOT.parents[1], env=env, capture_output=True, text=True, check=False,
                ), ssh_capture_dir

            result, capture = run_with_remote_main(source_sha)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = (capture / "args.1").read_text(encoding="utf-8").splitlines()
            remote_script = (capture / "stdin.1").read_text(encoding="utf-8")
            self.assertEqual(args[-4:], ["-s", "--", source_sha, "/home/ubuntu/quant-monitor-runtime/AIAuditBridge"])
            self.assertIn('SOURCE_SHA="$1"', remote_script)
            self.assertIn('REMOTE_AAB="$2"', remote_script)
            self.assertNotIn(source_sha, remote_script)
            second_args = (capture / "args.2").read_text(encoding="utf-8").splitlines()
            second_script = (capture / "stdin.2").read_text(encoding="utf-8")
            self.assertEqual(second_args[-4:], ["-s", "--", source_sha, "/home/ubuntu/quant-monitor-runtime/AIAuditBridge"])
            self.assertIn('SOURCE_SHA="$1"', second_script)
            self.assertIn('REMOTE_AAB="$2"', second_script)
            self.assertIn('AIAUDIT_BRIDGE_ROOT="$REMOTE_AAB"', second_script)
            self.assertIn('QUANT_MONITOR_ROOT="$REMOTE_MONITOR"', second_script)
            self.assertEqual(
                (capture / "setup.args").read_text(encoding="utf-8").splitlines(),
                [str(capture / "repo"), source_sha, str(capture / "repo")],
            )
            git_calls = (root / "capture" / "git-args").read_text(encoding="utf-8")
            self.assertIn("rev-parse origin/main", git_calls)
            self.assertIn(f"checkout --detach {source_sha} --quiet", git_calls)
            self.assertEqual(git_calls.count("fetch origin main"), 1, git_calls)

            mismatch, mismatch_capture = run_with_remote_main("b" * 40)
            self.assertEqual(mismatch.returncode, 1)
            self.assertIn("requested SHA does not match fetched main", mismatch.stderr)
            self.assertFalse((mismatch_capture / "args.2").exists())

            failed_fetch, failed_capture = run_with_remote_main(source_sha, FAIL_FETCH="1")
            self.assertEqual(failed_fetch.returncode, 42)
            self.assertFalse((failed_capture / "args.2").exists())

            dirty_source, dirty_capture = run_with_remote_main(source_sha, DIRTY_RUNTIME_SOURCE="1")
            self.assertEqual(dirty_source.returncode, 1)
            self.assertIn("refusing dirty production runtime source", dirty_source.stderr)
            self.assertFalse((dirty_capture / "args.2").exists())

    def test_deployment_uses_a_dedicated_runtime_checkout(self) -> None:
        script = (ROOT / "scripts" / "deploy_to_vps.sh").read_text(encoding="utf-8")
        service = (ROOT / "systemd" / "codex-quant.service.example").read_text(
            encoding="utf-8"
        )

        runtime_root = "/home/ubuntu/quant-monitor-runtime/AIAuditBridge"
        self.assertIn(f'REMOTE_AAB="{runtime_root}"', script)
        self.assertIn(f"WorkingDirectory={runtime_root}/ops/quant-monitor", service)
        self.assertNotIn("/home/ubuntu/Projects/AIAuditBridge", service)

    def test_strategy_health_sync_has_a_separate_root_owned_drop_in_example(self) -> None:
        drop_in = (
            ROOT / "systemd" / "codex-quant.service.d" / "strategy-health-sync.conf.example"
        ).read_text(encoding="utf-8")

        self.assertIn("STRATEGY_HEALTH_PUBLISH=1", drop_in)
        self.assertIn("/api/internal/sync-strategy-health", drop_in)
        self.assertIn(
            "LoadCredential=strategy-health-sync.token:/etc/codex-quant/strategy-health.sync.token",
            drop_in,
        )
        self.assertIn("STRATEGY_HEALTH_SYNC_TOKEN_FILE=%d/strategy-health-sync.token", drop_in)
        self.assertNotIn("STRATEGY_HEALTH_SYNC_TOKEN=", drop_in)

    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            capture_output=True,
            check=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        return result.stdout.strip()

    def test_setup_fresh_qpk_clone_checks_out_pin_and_existing_dirty_mirror_still_refuses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream = root / "upstream"
            upstream.mkdir()
            self._git(upstream, "init", "--initial-branch=main")
            self._git(upstream, "config", "user.name", "Test")
            self._git(upstream, "config", "user.email", "test@example.invalid")
            (upstream / "module.py").write_text("VERSION = 1\n", encoding="utf-8")
            self._git(upstream, "add", "module.py")
            self._git(upstream, "commit", "-m", "base")
            pin = self._git(upstream, "rev-parse", "HEAD")
            (upstream / "module.py").write_text("VERSION = 2\n", encoding="utf-8")
            self._git(upstream, "commit", "-am", "advance main")
            bare = root / "QuantPlatformKit.git"
            self._git(root, "clone", "--bare", str(upstream), str(bare))

            aab = root / "AIAuditBridge"
            self._git(root, "clone", str(upstream), str(aab))
            self._git(aab, "config", "user.name", "Test")
            self._git(aab, "config", "user.email", "test@example.invalid")
            aab_sha = self._git(aab, "rev-parse", "HEAD^")
            self._git(aab, "reset", "--hard", aab_sha)
            monitor = aab / "ops" / "quant-monitor"
            (monitor / "scripts").mkdir(parents=True)
            shutil.copyfile(ROOT / "scripts" / "common_env.sh", monitor / "scripts" / "common_env.sh")
            (monitor / "qpk-runtime.sha").write_text(pin + "\n", encoding="utf-8")
            shutil.copyfile(
                ROOT / "requirements-linux-py312.lock",
                monitor / "requirements-linux-py312.lock",
            )
            self._git(
                aab,
                "add",
                "ops/quant-monitor/qpk-runtime.sha",
                "ops/quant-monitor/requirements-linux-py312.lock",
                "ops/quant-monitor/scripts/common_env.sh",
            )
            self._git(aab, "commit", "-m", "pin qpk and lock")
            aab_sha = self._git(aab, "rev-parse", "HEAD")

            qpk_parent = root / "mirrors"
            qpk_parent.mkdir()
            qpk = qpk_parent / "QuantPlatformKit"
            lifecycle_root = root / "lifecycle-store"
            lifecycle_root.mkdir()
            qpk_alias = root / "mirror-root-alias"
            qpk_alias.symlink_to(qpk_parent, target_is_directory=True)
            lifecycle_alias = root / "lifecycle-root-alias"
            lifecycle_alias.symlink_to(lifecycle_root, target_is_directory=True)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            git_stub = bin_dir / "git"
            git_stub.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                f"real={real_git!r}\n"
                f"bare={bare.as_uri()!r}\n"
                "args=sys.argv[1:]\n"
                "for i, arg in enumerate(args):\n"
                "    if 'QuantStrategyLab/QuantPlatformKit.git' in arg:\n"
                "        args[i]=bare\n"
                "os.execv(real, ['git', *args])\n",
                encoding="utf-8",
            )
            git_stub.chmod(0o755)
            pip_log = root / "pip.log"
            pip_code = (
                f"#!{sys.executable}\n"
                "import os\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"log = Path({str(pip_log)!r})\n"
                "prev = log.read_text() if log.exists() else ''\n"
                "log.write_text(prev + ' '.join(sys.argv[1:]) + '\\n')\n"
                "args = sys.argv[1:]\n"
                "if os.environ.get('SETUP_FAIL_PIP') == '1' and 'install' in args:\n"
                "    raise SystemExit(17)\n"
                "if '-e' in args:\n"
                "    raise SystemExit(9)\n"
                "if 'install' in args and '-U' in args:\n"
                "    raise SystemExit(8)\n"
                "if args[:1] == ['check']:\n"
                "    raise SystemExit(0)\n"
                "if 'install' in args and '--require-hashes' in args and '-r' in args:\n"
                "    raise SystemExit(0)\n"
                "if 'install' in args and '--no-deps' in args and '--no-build-isolation' in args:\n"
                "    source = Path(args[-1])\n"
                "    if source.is_dir():\n"
                "        (source / 'installed-from-archive').write_text('ok')\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(f'unexpected pip invocation: {args!r}')\n"
            )
            venv_python_code = (
                f"#!{sys.executable}\n"
                "import subprocess\n"
                "import sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "if args[:1] == ['-I']:\n"
                "    import os\n"
                "    assert 'PYTHONPATH' not in os.environ\n"
                "    args = args[1:]\n"
                "if args[:2] == ['-m', 'pip']:\n"
                "    raise SystemExit(subprocess.call([str(Path(__file__).with_name('pip')), *args[2:]]))\n"
                "raise SystemExit(f'unexpected venv python: {args!r}')\n"
            )
            python_stub = bin_dir / "python3"
            python_stub.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if args[:1] == ['-I']:\n"
                "    import os\n"
                "    assert 'PYTHONPATH' not in os.environ\n"
                "    args = args[1:]\n"
                "if args == ['-']:\n"
                "    code = sys.stdin.read()\n"
                "    if 'glibc' in code and 'need 3.12' in code:\n"
                "        import os, platform\n"
                "        sys.version_info = (3, 12, 0, 'final', 0)\n"
                "        platform.system = lambda: 'Linux'\n"
                "        platform.machine = lambda: 'x86_64'\n"
                "        platform.python_implementation = lambda: 'CPython'\n"
                "        os.confstr = lambda key: 'glibc 2.35'\n"
                "        exec(compile(code, '<runtime-preflight>', 'exec'), {'__name__': '__main__'})\n"
                "        raise SystemExit(0)\n"
                "    raise SystemExit(f'unexpected python stdin: {code[:80]!r}')\n"
                "if len(args) >= 3 and args[0] == '-m' and args[1] == 'venv':\n"
                "    bindir = Path(args[-1]) / 'bin'\n"
                "    bindir.mkdir(parents=True, exist_ok=True)\n"
                f"    (bindir / 'pip').write_text({pip_code!r})\n"
                "    (bindir / 'pip').chmod(0o755)\n"
                f"    (bindir / 'python').write_text({venv_python_code!r})\n"
                "    (bindir / 'python').chmod(0o755)\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(f'unexpected python3 invocation: {args!r}')\n",
                encoding="utf-8",
            )
            python_stub.chmod(0o755)
            for name in ("gh", "gcloud"):
                stub = bin_dir / name
                stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                stub.chmod(0o755)

            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "TMPDIR": str(root),
                "QUANT_MONITOR_ROOT": str(monitor),
                "QUANT_PLATFORM_KIT_ROOT": str(qpk),
                "AIAUDIT_BRIDGE_ROOT": str(aab),
                "QUANT_PROJECTS_ROOT": str(qpk_parent),
                "LIFECYCLE_LOCAL_ROOT": str(lifecycle_root),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            env.pop("QUANT_MONITOR_VENV", None)
            self.assertFalse(qpk.exists())
            fresh = subprocess.run(
                ["bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), aab_sha, str(aab)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertEqual(self._git(qpk, "rev-parse", "HEAD"), pin)
            self.assertEqual((qpk / "module.py").read_text(encoding="utf-8"), "VERSION = 1\n")
            self.assertIn(f"sha={pin}", fresh.stdout)
            self.assertNotIn("tracked changes require mirror synchronization", fresh.stderr)
            pip_calls = pip_log.read_text(encoding="utf-8")
            self.assertIn("--require-hashes", pip_calls)
            self.assertIn("--only-binary=:all:", pip_calls)
            self.assertIn("--no-deps", pip_calls)
            self.assertIn("--no-build-isolation", pip_calls)
            self.assertIn("check", pip_calls)
            self.assertNotIn(" -U ", f" {pip_calls} ")

            live_venv_marker = monitor / ".venv" / "keep-existing-env.txt"
            live_venv_marker.write_text("preserve\n", encoding="utf-8")
            stage_parent = root / "isolated-stage"
            stage_parent.mkdir()
            staged_venv = stage_parent / ".venv"
            staged_qpk = Path(f"{staged_venv}.qpk")
            qpk_status_before_stage = self._git(qpk, "status", "--porcelain", "--untracked-files=all")
            staged = subprocess.run(
                ["bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), aab_sha, str(aab)],
                text=True,
                capture_output=True,
                check=False,
                env={**env, "QUANT_MONITOR_VENV": str(staged_venv)},
            )
            self.assertEqual(staged.returncode, 0, staged.stderr)
            self.assertTrue((staged_venv / "bin" / "python").is_file())
            self.assertFalse(staged_qpk.exists())
            self.assertEqual(self._git(qpk, "rev-parse", "HEAD"), pin)
            self.assertEqual(
                self._git(qpk, "status", "--porcelain", "--untracked-files=all"),
                qpk_status_before_stage,
            )
            self.assertEqual((qpk / "module.py").read_text(encoding="utf-8"), "VERSION = 1\n")
            self.assertEqual(live_venv_marker.read_text(encoding="utf-8"), "preserve\n")

            alias_parent = root / "live-venv-alias"
            alias_parent.symlink_to(monitor / ".venv", target_is_directory=True)
            invalid_stages = (
                monitor / ".venv",
                aab / "new-stage",
                alias_parent / "staged",
                qpk_parent / "new-stage",
                lifecycle_root / "new-stage",
                qpk_alias / "new-stage",
                lifecycle_alias / "new-stage",
                root / "missing-parent" / ".venv",
                stage_parent / ".." / "escaped-stage",
            )
            for invalid_stage in invalid_stages:
                refused = subprocess.run(
                    ["bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), aab_sha, str(aab)],
                    text=True,
                    capture_output=True,
                    check=False,
                    env={**env, "QUANT_MONITOR_VENV": str(invalid_stage)},
                )
                self.assertNotEqual(refused.returncode, 0, str(invalid_stage))
                self.assertEqual(self._git(qpk, "rev-parse", "HEAD"), pin)
                self.assertEqual((qpk / "module.py").read_text(encoding="utf-8"), "VERSION = 1\n")
                self.assertEqual(live_venv_marker.read_text(encoding="utf-8"), "preserve\n")

            relative_protected_root = subprocess.run(
                ["bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), aab_sha, str(aab)],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
                env={
                    **env,
                    "QUANT_MONITOR_VENV": str(stage_parent / "relative-root-stage"),
                    "LIFECYCLE_LOCAL_ROOT": "missing-lifecycle-root",
                },
            )
            self.assertNotEqual(relative_protected_root.returncode, 0)
            self.assertIn("unable to resolve protected runtime path", relative_protected_root.stderr)
            self.assertFalse((stage_parent / "relative-root-stage").exists())

            failed_stage = root / "failed-stage" / ".venv"
            failed_stage.parent.mkdir()
            failed = subprocess.run(
                ["bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), aab_sha, str(aab)],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **env,
                    "QUANT_MONITOR_VENV": str(failed_stage),
                    "SETUP_FAIL_PIP": "1",
                },
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("locked dependency install failed", failed.stderr)
            self.assertTrue(live_venv_marker.is_file())
            self.assertEqual(live_venv_marker.read_text(encoding="utf-8"), "preserve\n")
            self.assertEqual(self._git(qpk, "rev-parse", "HEAD"), pin)
            self.assertEqual((qpk / "module.py").read_text(encoding="utf-8"), "VERSION = 1\n")

            (qpk / "module.py").write_text("unknown local edit\n", encoding="utf-8")
            blocked = subprocess.run(
                ["bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), aab_sha, str(aab)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("QPK tracked changes require mirror synchronization/review", blocked.stderr)
            self.assertEqual((qpk / "module.py").read_text(encoding="utf-8"), "unknown local edit\n")
            self.assertEqual(self._git(qpk, "rev-parse", "HEAD"), pin)

            (qpk / "module.py").write_text("VERSION = 1\n", encoding="utf-8")
            qpk_before = self._git(qpk, "rev-parse", "HEAD")
            (monitor / "requirements-linux-py312.lock").unlink()
            missing_lock = subprocess.run(
                ["bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), aab_sha, str(aab)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertNotEqual(missing_lock.returncode, 0)
            self.assertIn("missing requirements-linux-py312.lock", missing_lock.stderr)
            self.assertEqual(self._git(qpk, "rev-parse", "HEAD"), qpk_before)

            shutil.copyfile(
                ROOT / "requirements-linux-py312.lock",
                monitor / "requirements-linux-py312.lock",
            )
            (monitor / "requirements-linux-py312.lock").write_text(
                (monitor / "requirements-linux-py312.lock").read_text(encoding="utf-8")
                + "# tampered\n",
                encoding="utf-8",
            )
            tampered_lock = subprocess.run(
                ["bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), aab_sha, str(aab)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertNotEqual(tampered_lock.returncode, 0)
            self.assertIn("refusing dirty AIAuditBridge runtime source", tampered_lock.stderr)

    def test_setup_rejects_pypy_before_mirror_or_venv_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream = root / "upstream"
            upstream.mkdir()
            self._git(upstream, "init", "--initial-branch=main")
            self._git(upstream, "config", "user.name", "Test")
            self._git(upstream, "config", "user.email", "test@example.invalid")
            (upstream / "module.py").write_text("VERSION = 1\n", encoding="utf-8")
            self._git(upstream, "add", "module.py")
            self._git(upstream, "commit", "-m", "base")
            pin = self._git(upstream, "rev-parse", "HEAD")

            aab = root / "AIAuditBridge"
            self._git(root, "clone", str(upstream), str(aab))
            self._git(aab, "config", "user.name", "Test")
            self._git(aab, "config", "user.email", "test@example.invalid")
            monitor = aab / "ops" / "quant-monitor"
            (monitor / "scripts").mkdir(parents=True)
            shutil.copyfile(ROOT / "scripts" / "common_env.sh", monitor / "scripts" / "common_env.sh")
            (monitor / "qpk-runtime.sha").write_text(pin + "\n", encoding="utf-8")
            shutil.copyfile(
                ROOT / "requirements-linux-py312.lock",
                monitor / "requirements-linux-py312.lock",
            )
            self._git(
                aab,
                "add",
                "ops/quant-monitor/qpk-runtime.sha",
                "ops/quant-monitor/requirements-linux-py312.lock",
                "ops/quant-monitor/scripts/common_env.sh",
            )
            self._git(aab, "commit", "-m", "pin")
            aab_sha = self._git(aab, "rev-parse", "HEAD")

            qpk = root / "mirrors" / "QuantPlatformKit"
            qpk.parent.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            python_stub = bin_dir / "python3"
            python_stub.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "args = sys.argv[1:]\n"
                "if args[:1] == ['-I']:\n"
                "    assert 'PYTHONPATH' not in os.environ\n"
                "    args = args[1:]\n"
                "if args == ['-']:\n"
                "    code = sys.stdin.read()\n"
                "    import platform\n"
                "    from collections import namedtuple\n"
                "    minor = 11 if os.environ.get('SETUP_WRONG_VERSION') == '1' else 12\n"
                "    sys.version_info = namedtuple('Version', 'major minor micro releaselevel serial')(3, minor, 0, 'final', 0)\n"
                "    platform.system = lambda: 'Linux'\n"
                "    platform.machine = lambda: 'x86_64'\n"
                "    platform.python_implementation = lambda: 'PyPy'\n"
                "    os.confstr = lambda key: 'glibc 2.35'\n"
                "    exec(compile(code, '<runtime-preflight>', 'exec'), {'__name__': '__main__'})\n"
                "    raise SystemExit(0)\n"
                "if len(args) >= 3 and args[:2] == ['-m', 'venv']:\n"
                "    print('MUTATION: venv', file=sys.stderr)\n"
                "    raise SystemExit(70)\n"
                "raise SystemExit(f'unexpected python3 invocation: {args!r}')\n",
                encoding="utf-8",
            )
            python_stub.chmod(0o755)
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            git_stub = bin_dir / "git"
            git_stub.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                f"real = {real_git!r}\n"
                "args = sys.argv[1:]\n"
                "if 'https://github.com/QuantStrategyLab/QuantPlatformKit.git' in args:\n"
                "    print('MUTATION: qpk clone', file=sys.stderr)\n"
                "    raise SystemExit(71)\n"
                "if args[:3] == ['-C', os.environ['QUANT_PLATFORM_KIT_ROOT'], 'fetch']:\n"
                "    print('MUTATION: qpk fetch', file=sys.stderr)\n"
                "    raise SystemExit(72)\n"
                "if args[:3] == ['-C', os.environ['QUANT_PLATFORM_KIT_ROOT'], 'checkout']:\n"
                "    print('MUTATION: qpk checkout', file=sys.stderr)\n"
                "    raise SystemExit(73)\n"
                "os.execv(real, ['git', *args])\n",
                encoding="utf-8",
            )
            git_stub.chmod(0o755)
            for name in ("gh", "gcloud"):
                stub = bin_dir / name
                stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                stub.chmod(0o755)

            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "TMPDIR": str(root),
                "QUANT_MONITOR_ROOT": str(monitor),
                "QUANT_PLATFORM_KIT_ROOT": str(qpk),
                "AIAUDIT_BRIDGE_ROOT": str(aab),
                "QUANT_PROJECTS_ROOT": str(qpk.parent),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), aab_sha, str(aab)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("only covers", result.stderr)
            self.assertIn("unsupported Python implementation 'PyPy'", result.stderr)
            self.assertNotIn("MUTATION:", result.stderr)
            self.assertFalse(qpk.exists())
            self.assertFalse((monitor / ".venv").exists())
            for extra, expected_error in (({}, "unsupported Python implementation 'PyPy'"),
                                          ({"SETUP_WRONG_VERSION": "1"}, "unsupported Python 3.11")):
                with self.subTest(staging_platform=expected_error):
                    staged = root / "new-staged-venv"
                    refused = subprocess.run(
                        ["bash", str(ROOT / "scripts/setup_vps_runtime.sh"), aab_sha, str(aab)],
                        env={**env, "QUANT_MONITOR_VENV": str(staged), **extra},
                        capture_output=True, text=True, timeout=5, check=False,
                    )
                    self.assertNotEqual(refused.returncode, 0)
                    self.assertIn(expected_error, refused.stderr)
                    self.assertNotIn("MUTATION:", refused.stderr)
                    self.assertFalse(staged.exists())
                    self.assertFalse(qpk.exists())

    def test_setup_reports_failure_when_locked_pip_install_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream = root / "upstream"
            upstream.mkdir()
            self._git(upstream, "init", "--initial-branch=main")
            self._git(upstream, "config", "user.name", "Test")
            self._git(upstream, "config", "user.email", "test@example.invalid")
            (upstream / "module.py").write_text("VERSION = 1\n", encoding="utf-8")
            self._git(upstream, "add", "module.py")
            self._git(upstream, "commit", "-m", "base")
            pin = self._git(upstream, "rev-parse", "HEAD")
            bare = root / "QuantPlatformKit.git"
            self._git(root, "clone", "--bare", str(upstream), str(bare))

            aab = root / "AIAuditBridge"
            self._git(root, "clone", str(upstream), str(aab))
            self._git(aab, "config", "user.name", "Test")
            self._git(aab, "config", "user.email", "test@example.invalid")
            monitor = aab / "ops" / "quant-monitor"
            (monitor / "scripts").mkdir(parents=True)
            shutil.copyfile(ROOT / "scripts" / "common_env.sh", monitor / "scripts" / "common_env.sh")
            (monitor / "qpk-runtime.sha").write_text(pin + "\n", encoding="utf-8")
            shutil.copyfile(
                ROOT / "requirements-linux-py312.lock",
                monitor / "requirements-linux-py312.lock",
            )
            self._git(
                aab,
                "add",
                "ops/quant-monitor/qpk-runtime.sha",
                "ops/quant-monitor/requirements-linux-py312.lock",
                "ops/quant-monitor/scripts/common_env.sh",
            )
            self._git(aab, "commit", "-m", "pin")
            aab_sha = self._git(aab, "rev-parse", "HEAD")

            qpk = root / "mirrors" / "QuantPlatformKit"
            qpk.parent.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            git_stub = bin_dir / "git"
            git_stub.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                f"real = {real_git!r}\n"
                f"bare = {bare.as_uri()!r}\n"
                "args = sys.argv[1:]\n"
                "for i, arg in enumerate(args):\n"
                "    if 'QuantStrategyLab/QuantPlatformKit.git' in arg:\n"
                "        args[i] = bare\n"
                "os.execv(real, ['git', *args])\n",
                encoding="utf-8",
            )
            git_stub.chmod(0o755)
            pip_code = (
                f"#!{sys.executable}\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if 'install' in args and '--require-hashes' in args:\n"
                "    raise SystemExit(17)\n"
                "raise SystemExit(0)\n"
            )
            venv_python_code = (
                f"#!{sys.executable}\n"
                "import subprocess, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "if args[:2] == ['-m', 'pip']:\n"
                "    raise SystemExit(subprocess.call([str(Path(__file__).with_name('pip')), *args[2:]]))\n"
                "raise SystemExit(0)\n"
            )
            python_stub = bin_dir / "python3"
            python_stub.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if args == ['-']:\n"
                "    code = sys.stdin.read()\n"
                "    if 'glibc' in code and 'need 3.12' in code:\n"
                "        raise SystemExit(0)\n"
                "    raise SystemExit(f'unexpected python stdin: {code[:80]!r}')\n"
                "if len(args) >= 3 and args[0] == '-m' and args[1] == 'venv':\n"
                "    bindir = Path(args[-1]) / 'bin'\n"
                "    bindir.mkdir(parents=True, exist_ok=True)\n"
                f"    (bindir / 'pip').write_text({pip_code!r})\n"
                "    (bindir / 'pip').chmod(0o755)\n"
                f"    (bindir / 'python').write_text({venv_python_code!r})\n"
                "    (bindir / 'python').chmod(0o755)\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(f'unexpected python3 invocation: {args!r}')\n",
                encoding="utf-8",
            )
            python_stub.chmod(0o755)
            for name in ("gh", "gcloud"):
                stub = bin_dir / name
                stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                stub.chmod(0o755)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "setup_vps_runtime.sh"), aab_sha, str(aab)],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMPDIR": str(root),
                    "QUANT_MONITOR_ROOT": str(monitor),
                    "QUANT_PLATFORM_KIT_ROOT": str(qpk),
                    "AIAUDIT_BRIDGE_ROOT": str(aab),
                    "QUANT_PROJECTS_ROOT": str(qpk.parent),
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("locked dependency install failed", result.stderr)
            self.assertNotIn("[setup] ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
