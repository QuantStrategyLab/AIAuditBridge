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

    def _offline_consumer_scripts(self):
        workflow = (ROOT.parents[1] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        self.assertIn("\n  check-quant-consumers-offline:\n", workflow)
        job = workflow.split("\n  check-quant-consumers-offline:\n", 1)[1].split("\n  install-quant-release-inactive:\n", 1)[0]
        scripts = [textwrap.dedent(block) for block in re.findall(r"(?m)^        run: \|\n((?:^          .*\n|^\n)*)", job)]
        return workflow, job, scripts

    def test_offline_consumers_have_an_independent_fixed_mode(self):
        workflow, job, scripts = self._offline_consumer_scripts()
        self.assertEqual(len(scripts), 3)
        self.assertIn("inputs.mode != 'check-quant-consumers-offline'", workflow)
        self.assertIn("!inputs.acknowledge_interruption", job)
        self.assertIn("inputs.ssh_unban_ip == ''", job)
        self.assertIn("ref: 35ac71176127f07e00fe04dbc793777f3c595bc0", job)
        self.assertNotRegex(job, r"systemctl|sudo|daemon-reload|install_immutable_release|setup_vps_runtime|sync_strategy_repos|FileFinder")
        self.assertIn("/home/ubuntu/quant-monitor-data03-286757-py312-v1", scripts[2])
        self.assertIn(" -I -B -", scripts[2])

    def test_offline_snapshot_rejects_unsafe_archives_and_existing_paths(self):
        import ast
        import contextlib
        import hashlib
        import io
        import tarfile
        from types import SimpleNamespace

        _, _, scripts = self._offline_consumer_scripts()
        code = scripts[1].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        files = next(ast.literal_eval(n.value) for n in ast.parse(code).body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "SOURCE_FILES" for t in n.targets))
        self.assertEqual(len(files), 40)
        self.assertEqual(sum(name.endswith(".py") for name in files), 38)
        self.assertFalse(any({".git", ".venv", "data"} & set(Path(name).parts) for name in files))
        contents = {name: ("# synthetic " + name + "\n").encode() for name in files}
        contents["ops/quant-monitor/qpk-runtime.sha"] = b"28675796cabbe137a1fa3970b70d1aa98e952c88\n"
        directories = {str(parent) for name in files for parent in Path(name).parents if str(parent) != "."}
        size = sum(map(len, contents.values()))
        tree = b"".join(("100644 blob " + hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest() + f" {len(content)}\t{name}\0").encode()
                        for name, content in contents.items())
        cases = ("valid", "existing", "temporary_overlap", "temporary_alias", "blocked_parent", "bad_run_id",
                 "manifest_missing", "manifest_link", "archive_timeout", "extra_private", "git_private",
                 "absolute", "dotdot", "duplicate", "missing", "symlink", "hardlink", "device", "fifo", "sparse",
                 "wrong_hash", "wrong_size", "wrong_mode", "special_mode", "pax_escape", "write_failure")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo, temporary = root / "checkout", root / "runner-temp"
                repo.mkdir()
                temporary.mkdir()
                snapshot = temporary / "quant-monitor-consumer-imports-35ac-12345"
                if case == "existing":
                    snapshot.mkdir()
                    (snapshot / "preserved").write_text("prior")
                if case == "temporary_overlap":
                    temporary = repo
                if case == "temporary_alias":
                    alias = root / "temp-alias"
                    alias.symlink_to(temporary, target_is_directory=True)
                    temporary = alias
                if case == "blocked_parent":
                    temporary = Path("/opt/quant-monitor/fixture-never-read")
                run_id = "bad-attempt" if case == "bad_run_id" else "12345"
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w") as bundle:
                    for name in sorted(directories):
                        member = tarfile.TarInfo(name)
                        member.type, member.mode = tarfile.DIRTYPE, 0o775
                        bundle.addfile(member)
                    for index, (name, content) in enumerate(contents.items()):
                        if index == 0 and case == "missing":
                            continue
                        member = tarfile.TarInfo(name)
                        member.size, member.mode = len(content), 0o664
                        if index == 0:
                            if case == "absolute":
                                member.name = "/private/token_DO_NOT_EMIT"
                            if case == "dotdot":
                                member.name = "../outside"
                            if case == "pax_escape":
                                member.pax_headers = {"path": "../outside"}
                            type_cases = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                                          "device": tarfile.CHRTYPE, "fifo": tarfile.FIFOTYPE, "sparse": tarfile.GNUTYPE_SPARSE}
                            if case in type_cases:
                                member.type = type_cases[case]
                                member.linkname = "/private/token_DO_NOT_EMIT"
                                member.size = 0
                            if case == "wrong_hash":
                                content = b"x" * len(content)
                            if case == "wrong_size":
                                content += b"x"
                                member.size += 1
                            if case == "wrong_mode":
                                member.mode = 0o775
                            if case == "special_mode":
                                member.mode = 0o4664
                        bundle.addfile(member, io.BytesIO(content) if member.isreg() else None)
                        if index == 0 and case == "duplicate":
                            bundle.addfile(member, io.BytesIO(content))
                    if case in {"extra_private", "git_private"}:
                        name = "ops/quant-monitor/data/private.json" if case == "extra_private" else ".git/config"
                        member = tarfile.TarInfo(name)
                        member.size = 17
                        bundle.addfile(member, io.BytesIO(b"token_DO_NOT_EMIT!"))
                calls = []
                def git(argv, **kwargs):
                    calls.append((argv, kwargs))
                    if argv[3] == "ls-tree":
                        value = tree
                        if case == "manifest_missing":
                            value = b"\0".join(tree.split(b"\0")[1:])
                        if case == "manifest_link":
                            value = tree.replace(b"100644", b"120000", 1)
                        return SimpleNamespace(stdout=value)
                    if case == "archive_timeout":
                        raise subprocess.TimeoutExpired(argv, 30, stderr="token_DO_NOT_EMIT")
                    return SimpleNamespace(stdout=archive.getvalue())
                fixture = code.replace("EXPECTED_SOURCE_BYTES = 480370", f"EXPECTED_SOURCE_BYTES = {size}")
                stdout = io.StringIO()
                real_fdopen = os.fdopen
                @contextlib.contextmanager
                def fail_write(descriptor, mode):
                    with real_fdopen(descriptor, mode) as stream:
                        yield SimpleNamespace(fileno=stream.fileno,
                                              write=mock.Mock(side_effect=OSError("token_DO_NOT_EMIT")))
                patch_writer = mock.patch.object(os, "fdopen", fail_write) if case == "write_failure" else contextlib.nullcontext()
                with mock.patch.object(sys, "argv", ["snapshot", str(repo), str(temporary), run_id]), \
                     mock.patch.object(subprocess, "run", side_effect=git), patch_writer, contextlib.redirect_stdout(stdout):
                    if case == "valid":
                        exec(compile(fixture, "<offline-snapshot>", "exec"), {})
                    else:
                        with self.assertRaises(SystemExit) as caught:
                            exec(compile(fixture, "<offline-snapshot>", "exec"), {})
                        self.assertEqual(caught.exception.code, 1)
                early = case in {"existing", "temporary_overlap", "temporary_alias", "blocked_parent", "bad_run_id"}
                self.assertEqual(len(calls), 0 if early else 1 if case in {"manifest_missing", "manifest_link"} else 2)
                for number, (argv, kwargs) in enumerate(calls):
                    subcommand = ["ls-tree", "-rlz", "--full-tree"] if number == 0 else ["archive", "--format=tar"]
                    self.assertEqual(argv, ["/usr/bin/git", "-C", str(repo), *subcommand,
                                            "35ac71176127f07e00fe04dbc793777f3c595bc0", "--", *files])
                    self.assertEqual(kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")
                    self.assertNotIn("GH_TOKEN", kwargs["env"])
                    self.assertNotIn("HOME", kwargs["env"])
                    self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
                self.assertNotIn("token_DO_NOT_EMIT", stdout.getvalue())
                self.assertFalse((root / "outside").exists())
                if case == "valid":
                    receipt = json.loads(stdout.getvalue())
                    self.assertEqual(receipt["source_files_checked"], 40)
                    self.assertFalse(receipt["immutable_release_proof"])
                    self.assertFalse(receipt["application_import_proof"])
                    self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
                    self.assertEqual({str(p.relative_to(snapshot)) for p in snapshot.rglob("*") if p.is_file()}, set(files))
                    self.assertFalse(any(p.is_symlink() for p in snapshot.rglob("*")))
                else:
                    self.assertNotIn("_verified", stdout.getvalue())
                    self.assertRegex(stdout.getvalue(), r"^\[offline-consumers\] failure=[a-z_]+; no reuse or cleanup\n$")
                    if case == "existing":
                        self.assertEqual((snapshot / "preserved").read_text(), "prior")
                    elif case == "write_failure":
                        self.assertTrue(snapshot.is_dir(), "own partial snapshot must not be silently reused or deleted")
                    else:
                        self.assertFalse(snapshot.exists())

    def test_offline_snapshot_accepts_real_local_git_archive_without_installing(self):
        import ast

        _, _, scripts = self._offline_consumer_scripts()
        code = scripts[1].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        files = next(ast.literal_eval(n.value) for n in ast.parse(code).body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "SOURCE_FILES" for t in n.targets))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, temporary = root / "checkout", root / "runner-temp"
            repo.mkdir()
            temporary.mkdir()
            for name in files:
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# synthetic local Git source only\n")
                if name.endswith("build_dashboard_snapshot.py"):
                    path.chmod(0o755)
            (repo / "ops/quant-monitor/qpk-runtime.sha").write_text("28675796cabbe137a1fa3970b70d1aa98e952c88\n")
            self._git(repo, "init", "-q")
            self._git(repo, "config", "user.email", "fixture@example.invalid")
            self._git(repo, "config", "user.name", "Fixture")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "-qm", "synthetic consumer source")
            sha = self._git(repo, "rev-parse", "HEAD")
            total = sum((repo / name).stat().st_size for name in files)
            fixture = code.replace("35ac71176127f07e00fe04dbc793777f3c595bc0", sha).replace("EXPECTED_SOURCE_BYTES = 480370", f"EXPECTED_SOURCE_BYTES = {total}")
            runner = root / "snapshot_prepare_fixture.py"
            runner.write_text(fixture)
            result = subprocess.run([sys.executable, "-I", "-B", str(runner), str(repo), str(temporary), "12345"],
                                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}, umask=0o077,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stderr, "")
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["source_files_checked"], 40)
            self.assertFalse(receipt["runtime_adoption_proof"])
            snapshot = temporary / "quant-monitor-consumer-imports-35ac-12345"
            for name in files:
                self.assertEqual((snapshot / name).read_bytes(), (repo / name).read_bytes())
            self.assertEqual((snapshot / "ops/quant-monitor/scripts/build_dashboard_snapshot.py").stat().st_mode & 0o777, 0o755)
            self.assertFalse((snapshot / ".git").exists())
            self.assertFalse((snapshot / "ops/quant-monitor/data").exists())
            self.assertFalse((snapshot / "ops/quant-monitor/.venv").exists())

    def test_offline_consumers_gate_binds_the_same_fixed_source(self) -> None:
        _, _, scripts = self._offline_consumer_scripts()
        sha = "a" * 40
        env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C", "LC_ALL": "C",
               "GITHUB_WORKSPACE": str(ROOT.parents[1]), "RUN_EVENT_NAME": "workflow_dispatch",
               "RUN_MODE": "check-quant-consumers-offline", "RUN_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
               "RUN_REF": "refs/heads/main", "RUN_ACK": "false", "RUN_UNBAN_IP": "",
               "RUN_SHA": sha, "RUN_WORKFLOW_SHA": sha,
               "RUN_WORKFLOW_REF": "QuantStrategyLab/AIAuditBridge/.github/workflows/vps_codex_service_ops.yml@refs/heads/main",
               "FIXTURE_HEAD": "35ac71176127f07e00fe04dbc793777f3c595bc0", "FIXTURE_MAIN": sha, "FIXTURE_DIRTY": ""}
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

    def _inactive_release_scripts(self):
        workflow = (ROOT.parents[1] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        self.assertIn("\n  install-quant-release-inactive:\n", workflow)
        job = workflow.split("\n  install-quant-release-inactive:\n", 1)[1].split("\n  stage-quant-runtime-inactive:\n", 1)[0]
        scripts = [textwrap.dedent(block) for block in re.findall(r"(?m)^        run: \|\n((?:^          .*\n|^\n)*)", job)]
        return workflow, job, scripts

    def test_inactive_release_has_fixed_separate_entry_without_activation(self):
        workflow, job, scripts = self._inactive_release_scripts()
        self.assertEqual(len(scripts), 3)
        self.assertIn("inputs.mode != 'install-quant-release-inactive'", workflow)
        self.assertIn("!inputs.acknowledge_interruption", job)
        self.assertIn("inputs.ssh_unban_ip == ''", job)
        self.assertIn("ref: 35ac71176127f07e00fe04dbc793777f3c595bc0", job)
        self.assertIn("persist-credentials: false", job)
        self.assertNotRegex(job, r"systemctl|sudo|daemon-reload|setup_vps_runtime|sync_strategy_repos|load_telegram_env")
        self.assertIn(" -I -B -", scripts[2])
        self.assertIn('env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C', scripts[2])

    def test_inactive_release_gate_binds_controller_and_application_separately(self) -> None:
        _, _, scripts = self._inactive_release_scripts()
        sha = "a" * 40
        env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C", "LC_ALL": "C",
               "GITHUB_WORKSPACE": str(ROOT.parents[1]), "RUN_EVENT_NAME": "workflow_dispatch",
               "RUN_MODE": "install-quant-release-inactive", "RUN_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
               "RUN_REF": "refs/heads/main", "RUN_ACK": "false", "RUN_UNBAN_IP": "",
               "RUN_SHA": sha, "RUN_WORKFLOW_SHA": sha,
               "RUN_WORKFLOW_REF": "QuantStrategyLab/AIAuditBridge/.github/workflows/vps_codex_service_ops.yml@refs/heads/main",
               "FIXTURE_HEAD": "35ac71176127f07e00fe04dbc793777f3c595bc0", "FIXTURE_MAIN": sha, "FIXTURE_DIRTY": ""}
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

    def test_inactive_release_installer_rejects_reuse_and_verifies_the_final_tree(self):
        import contextlib
        import hashlib
        import io
        from types import SimpleNamespace

        _, _, scripts = self._inactive_release_scripts()
        code = scripts[1].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        source_sha = "35ac71176127f07e00fe04dbc793777f3c595bc0"
        cases = ("valid", "existing", "existing_link", "installer_failure", "installer_secret", "reuse",
                 "missing", "extra", "mode", "wrong_data", "wrong_venv", "data_directory", "nested_mv", "timeout")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo, releases, candidate, data = (root / x for x in ("repo", "releases", "candidate", "data"))
                for directory in (repo, releases, candidate, data):
                    directory.mkdir()
                release = releases / source_sha
                files = {"ops/quant-monitor/scripts/check.py": (b"# synthetic source\n", 0o644),
                         "bin/tool.sh": (b"#!/bin/sh\nexit 0\n", 0o755),
                         "docs/data/nested.txt": (b"must not be excluded\n", 0o644)}
                manifest = b"".join((f"{mode | 0o100000:o} blob " + hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest() + "\t" + name).encode() + b"\0"
                                    for name, (content, mode) in files.items())
                if case == "existing":
                    release.mkdir()
                elif case == "existing_link":
                    release.symlink_to(root / "missing")
                def run(argv, **kwargs):
                    self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")
                    self.assertNotIn("HOME", kwargs["env"])
                    self.assertNotIn("GH_TOKEN", kwargs["env"])
                    if argv[0] == "/usr/bin/git":
                        self.assertEqual(argv, ["/usr/bin/git", "-C", str(repo), "ls-tree", "-rz", "--full-tree", source_sha])
                        return SimpleNamespace(stdout=manifest, returncode=0)
                    self.assertEqual(argv, ["/bin/bash", "--noprofile", "--norc", str(repo / "ops/quant-monitor/scripts/install_immutable_release.sh"),
                                            "--sha", source_sha, "--repo", str(repo), "--release-root", str(releases),
                                            "--runtime-data", str(data), "--runtime-venv", str(candidate)])
                    if case == "timeout":
                        raise subprocess.TimeoutExpired(argv, 120)
                    release.mkdir()
                    if case == "installer_failure":
                        return SimpleNamespace(stdout=b"private token_DO_NOT_EMIT", returncode=7)
                    destination = release / ".stage.nested" if case == "nested_mv" else release
                    for name, (content, mode) in files.items():
                        path = destination / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content)
                        path.chmod(mode)
                    (destination / "ops/quant-monitor/data").symlink_to(data if case != "wrong_data" else candidate)
                    (destination / "ops/quant-monitor/.venv").symlink_to(candidate if case != "wrong_venv" else data)
                    if case == "data_directory":
                        (destination / "ops/quant-monitor/data").unlink()
                        (destination / "ops/quant-monitor/data").mkdir()
                    if case == "missing":
                        (destination / "docs/data/nested.txt").unlink()
                    if case == "extra":
                        (destination / "unknown").write_text("preserve")
                    if case == "mode":
                        (destination / "bin/tool.sh").chmod(0o644)
                    output = "release_reused=" if case == "reuse" else "release_installed="
                    if case == "installer_secret":
                        return SimpleNamespace(stdout=b"token_DO_NOT_EMIT /private/path\n", returncode=0)
                    return SimpleNamespace(stdout=(output + str(release) + "\n").encode(), returncode=0)
                fixture = code.replace('/opt/quant-monitor/releases', str(releases)).replace('/home/ubuntu/quant-monitor-data03-286757-py312-v1', str(candidate)).replace('/home/ubuntu/quant-monitor-runtime/AIAuditBridge/ops/quant-monitor/data', str(data))
                stdout = io.StringIO()
                with mock.patch.object(sys, "argv", ["check", str(repo)]), mock.patch.object(subprocess, "run", side_effect=run) as calls, contextlib.redirect_stdout(stdout):
                    if case == "valid":
                        exec(compile(fixture, "<inactive-release-install>", "exec"), {})
                    else:
                        with self.assertRaises(SystemExit) as caught:
                            exec(compile(fixture, "<inactive-release-install>", "exec"), {})
                        self.assertNotEqual(caught.exception.code, 0)
                if case in {"existing", "existing_link"}:
                    calls.assert_not_called()
                if case == "valid":
                    receipt = json.loads(stdout.getvalue())
                    self.assertEqual(receipt["source_files_checked"], 3)
                    self.assertEqual(receipt["runtime_links_checked"], 2)
                    self.assertFalse(receipt["application_import_proof"])
                else:
                    self.assertNotIn("_verified", stdout.getvalue())
                    self.assertRegex(stdout.getvalue(), r"^\[inactive-release\] failure=[a-z_]+; residue preserved\n$")
                self.assertNotIn("token_DO_NOT_EMIT", stdout.getvalue())
                if case not in {"timeout"}:
                    self.assertTrue(os.path.lexists(release), "unknown target must not be deleted")

    def test_inactive_release_original_installer_uses_child_umask_only(self):
        _, _, scripts = self._inactive_release_scripts()
        code = scripts[1].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, releases, candidate, data = (root / x for x in ("repo", "releases", "candidate", "data"))
            for directory in (repo, releases, candidate, data):
                directory.mkdir()
            monitor = repo / "ops/quant-monitor"
            (monitor / "scripts").mkdir(parents=True)
            for name in ("common_env.sh", "health_check.sh", "daily_briefing_pipeline.sh"):
                (monitor / "scripts" / name).write_text("#!/bin/sh\n# fixture never executed\n")
            (monitor / "AGENTS.md").write_text("Synthetic installer fixture only\n")
            installer = monitor / "scripts/install_immutable_release.sh"
            shutil.copyfile(ROOT / "scripts/install_immutable_release.sh", installer)
            installer.chmod(0o755)
            self._git(repo, "init", "-q")
            self._git(repo, "config", "user.email", "fixture@example.invalid")
            self._git(repo, "config", "user.name", "Fixture")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "-qm", "synthetic archive")
            sha = self._git(repo, "rev-parse", "HEAD")
            fixture = code.replace("35ac71176127f07e00fe04dbc793777f3c595bc0", sha).replace('/opt/quant-monitor/releases', str(releases)).replace('/home/ubuntu/quant-monitor-data03-286757-py312-v1', str(candidate)).replace('/home/ubuntu/quant-monitor-runtime/AIAuditBridge/ops/quant-monitor/data', str(data))
            runner = root / "offline_install_fixture.py"
            # The wrapper starts restrictive; only the real installer child may
            # receive the reviewed 022. No parent/system umask mutation occurs.
            runner.write_text(fixture + "\nprobe = Path(__file__).with_name('wrapper-mode-probe')\nprobe.touch()\nprint('wrapper_mode=' + oct(probe.stat().st_mode & 0o777))\n")
            runner.chmod(0o600)
            result = subprocess.run([sys.executable, "-I", "-B", str(runner), str(repo)],
                                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}, umask=0o077,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            self.assertEqual(result.stderr, "")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('"stage": "inactive_release_tree_verified"', result.stdout)
            self.assertIn("wrapper_mode=0o600", result.stdout)
            release = releases / sha
            self.assertEqual((release / "ops").stat().st_mode & 0o777, 0o755)
            self.assertEqual((release / "ops/quant-monitor/AGENTS.md").stat().st_mode & 0o777, 0o644)
            self.assertEqual((release / "ops/quant-monitor/scripts/install_immutable_release.sh").stat().st_mode & 0o777, 0o755)
            self.assertEqual(os.readlink(release / "ops/quant-monitor/data"), str(data))
            self.assertEqual(os.readlink(release / "ops/quant-monitor/.venv"), str(candidate))

    def test_inactive_release_consumer_import_guards_and_all_origins_in_fresh_process(self):
        self._assert_consumer_import_guards(self._inactive_release_scripts()[2])

    def test_offline_consumer_imports_preserve_the_full_guard_matrix(self):
        self._assert_consumer_import_guards(self._offline_consumer_scripts()[2], temporary_snapshot=True)

    def _assert_consumer_import_guards(self, scripts, *, temporary_snapshot=False):
        import ast
        import hashlib

        code = scripts[2].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        parsed = ast.parse(code)
        assignments = {n.targets[0].id: n for n in parsed.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
        ops = ast.literal_eval(assignments["OPS_IMPORTS"].value)
        # The fixed daily names are read as literals, never supplied to production.
        daily = set(ast.literal_eval(assignments["EXPECTED_DAILY"].value.args[0].func.value).split())
        fixed_hashes = ast.literal_eval(assignments["HASHES"].value)
        source_files = ast.literal_eval(assignments["SOURCE_FILES"].value) if temporary_snapshot else ()
        cases = ("valid", "missing_dependency", "missing_daily", "network", "process", "write", "private_read",
                 "swallowed_network", "aab_origin", "qpk_origin", "package_path", "sys_path", "record_hash", "fixed_hash", "numpy_version", "pyc_mismatch", "pyc_link", "qpk_external", "private_listdir", "private_scandir", "secret_exception")
        if temporary_snapshot:
            cases += ("source_git_read", "source_data_read", "source_ignored_read", "checkout_git_read", "source_hardlink",
                      "tempdir_constant", "tempdir_mkstemp", "tempdir_mkdir", "tempdir_private_read")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                candidate = root / "candidate"
                site = candidate / "lib/python3.12/site-packages"
                release = root / ("quant-monitor-consumer-imports-35ac-12345" if temporary_snapshot else "release")
                site.mkdir(parents=True)
                release.mkdir(mode=0o700 if temporary_snapshot else 0o755)
                for relative in ops.values():
                    path = release / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("# actual synthetic module, no module substitution\n")
                names = daily | {"client", "client.config", "client.gateway_client", "client.errors", "service.provider_scenarios"}
                for name in sorted(names):
                    package = name in {"scripts", "service", "service.adapters", "client"}
                    path = release / (name.replace(".", "/") + ("/__init__.py" if package else ".py"))
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("# synthetic consumer definition\n")
                cli = release / "scripts/consume_daily_briefing.py"
                imported_names = sorted(names - {"scripts.consume_daily_briefing"} - ({"service.autonomy"} if case == "missing_daily" else set()))
                cli.write_text("\n".join("import " + name for name in imported_names) + "\n")
                private_dir = root / "private-fixture"
                private_dir.mkdir()
                (private_dir / "token_DO_NOT_EMIT").write_text("synthetic")
                extras = {
                    "missing_dependency": "import absent_fixture_dependency\n",
                    "secret_exception": "raise ValueError('token_DO_NOT_EMIT /private/path')\n",
                    "network": "import socket\nsocket.socket()\n",
                    "process": "import subprocess\nsubprocess.run(['/bin/false'])\n",
                    "write": f"open({str(root / 'forbidden-write')!r}, 'w')\n",
                    "private_read": "open('/private/token_DO_NOT_EMIT')\n",
                    "source_git_read": f"open({str(release / '.git/config')!r})\n",
                    "source_data_read": f"open({str(release / 'ops/quant-monitor/data/private')!r})\n",
                    "source_ignored_read": f"open({str(release / 'ignored.env')!r})\n",
                    "checkout_git_read": f"open({str(root / 'checkout/.git/config')!r})\n",
                    "private_listdir": f"import os\nos.listdir({str(private_dir)!r})\n",
                    "private_scandir": f"import os\nlist(os.scandir({str(private_dir)!r}))\n",
                    "swallowed_network": "import socket\ntry:\n socket.socket()\nexcept RuntimeError:\n pass\n",
                    "aab_origin": "__file__='/private/token_DO_NOT_EMIT.py'\n",
                    "package_path": "import service\nservice.__path__.append('/private/token_DO_NOT_EMIT')\n",
                    "sys_path": "import sys\nsys.path.append('/synthetic/old-shared-mirror')\n",
                }
                cli.write_text(cli.read_text() + "print('token_DO_NOT_EMIT /private/path')\nimport os\nos.write(1, b'token_DO_NOT_EMIT')\nos.write(2, b'token_DO_NOT_EMIT')\n" + extras.get(case, ""))
                qpk_root = site / "quant_platform_kit"
                (qpk_root / "strategy_lifecycle").mkdir(parents=True)
                (qpk_root / "__init__.py").write_text("import numpy, pandas\n")
                (qpk_root / "strategy_lifecycle/__init__.py").write_text("")
                for name in fixed_hashes:
                    (qpk_root / "strategy_lifecycle" / name).write_text("VALUE = 1\n")
                if case.startswith("tempdir_"):
                    initialization = (
                        "import tempfile\nfrom pathlib import Path\n"
                        f"assert Path(tempfile.gettempdir()) == Path({str(release)!r})\n"
                        "CACHE_ROOT = Path(tempfile.gettempdir()) / 'synthetic_cache'\n"
                    )
                    initialization += {
                        "tempdir_mkstemp": "tempfile.mkstemp()\n",
                        "tempdir_mkdir": "CACHE_ROOT.mkdir()\n",
                        "tempdir_private_read": "(Path(tempfile.gettempdir()) / 'private_token_DO_NOT_EMIT').read_text()\n",
                    }.get(case, "")
                    (qpk_root / "strategy_lifecycle/drift_detector.py").write_text(initialization)
                if case == "qpk_origin":
                    (qpk_root / "strategy_lifecycle/return_collector.py").write_text("__file__='/private/token_DO_NOT_EMIT.py'\n")
                if case in {"pyc_mismatch", "pyc_link"}:
                    import py_compile
                    module_path = qpk_root / "strategy_lifecycle/return_collector.py"
                    module_path.write_text("VALUE = 9\n")
                    timestamp = module_path.stat().st_mtime_ns
                    cache = Path(py_compile.compile(str(module_path), doraise=True))
                    if case == "pyc_link":
                        moved_cache = candidate / "existing-cache"
                        cache.rename(moved_cache)
                        cache.symlink_to(moved_cache)
                    module_path.write_text("VALUE = 1\n")
                    os.utime(module_path, ns=(timestamp, timestamp))
                    cli.write_text(cli.read_text() + "from quant_platform_kit.strategy_lifecycle import return_collector\nassert return_collector.VALUE == 1\n")
                if case == "qpk_external":
                    external_package = release / "external-qpk"
                    qpk_root.rename(external_package)
                    qpk_root.symlink_to(external_package, target_is_directory=True)
                for name, version in (("numpy", "2.5.2"), ("pandas", "3.0.5")):
                    (site / name).mkdir()
                    (site / name / "__init__.py").write_text(f"__version__ = {('0.0.0' if case == 'numpy_version' and name == 'numpy' else version)!r}\n")
                monitor = release / "ops/quant-monitor"
                shutil.copyfile(ROOT / "requirements-linux-py312.lock", monitor / "requirements-linux-py312.lock")
                if temporary_snapshot:
                    (monitor / "qpk-runtime.sha").write_text("28675796cabbe137a1fa3970b70d1aa98e952c88\n")
                    if case == "source_hardlink":
                        os.link(release / "client/config.py", root / "external-source-copy")
                manifest = b""
                source_bytes = 0
                for path in sorted(release.rglob("*")):
                    if path.is_file() and (not temporary_snapshot or str(path.relative_to(release)) in source_files):
                        content = path.read_bytes()
                        source_bytes += len(content)
                        oid = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
                        size_field = f" {len(content)}" if temporary_snapshot else ""
                        manifest += f"100644 blob {oid}{size_field}\t{path.relative_to(release)}\0".encode()
                fixture = code.replace('Path("/opt/quant-monitor/releases") / SOURCE_SHA', f'Path({str(release)!r})').replace('/home/ubuntu/quant-monitor-data03-286757-py312-v1', str(candidate))
                if temporary_snapshot:
                    fixture = fixture.replace("EXPECTED_SOURCE_BYTES = 480370", f"EXPECTED_SOURCE_BYTES = {source_bytes}")
                hashes = {name: hashlib.sha256((qpk_root / "strategy_lifecycle" / name).read_bytes()).hexdigest() for name in fixed_hashes}
                if case == "fixed_hash":
                    hashes["return_collector.py"] = "0" * 64
                hash_node = assignments["HASHES"]
                original_hash_source = "\n".join(code.splitlines()[hash_node.lineno - 1:hash_node.end_lineno])
                fixture = fixture.replace(original_hash_source, "HASHES = " + repr(hashes))
                # Simulate only installation metadata. Consumer/QPK modules are real
                # fixture files, imported in a new isolated process with production guards.
                prefix = f'''
import base64, hashlib, importlib.metadata, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
site = Path({str(site)!r})
sys.prefix = {str(candidate)!r}
sys.path[:] = [p for p in sys.path if 'site-packages' not in p]
sys.path.append(str(site))
fixture_versions = dict(__import__('re').findall(r'(?m)^([A-Za-z0-9_-]+)==([^ ]+)', Path({str(monitor / 'requirements-linux-py312.lock')!r}).read_text()))
fixture_versions['quant-platform-kit'] = '1.0.0'
files = []
for path in (site / 'quant_platform_kit').rglob('*.py'):
    p = importlib.metadata.PackagePath(str(path.relative_to(site)))
    value = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b'=').decode()
    if {case!r} == 'record_hash' and path.name == '__init__.py': value = 'incorrect'
    p.hash = SimpleNamespace(mode='sha256', value=value)
    files.append(p)
importlib.metadata.distribution = lambda name: SimpleNamespace(version=fixture_versions[name], files=files, locate_file=lambda p: site / p)
real_run = subprocess.run
fixture_source_files = {source_files!r}
def source_tree(argv, **kwargs):
    expected = ['/usr/bin/git', '-C', 'synthetic-checkout', 'ls-tree', '-rlz' if {temporary_snapshot!r} else '-rz', '--full-tree', '35ac71176127f07e00fe04dbc793777f3c595bc0']
    if {temporary_snapshot!r}:
        expected += ['--', *fixture_source_files]
    assert argv == expected
    subprocess.run = real_run
    return SimpleNamespace(stdout={manifest!r}, returncode=0)
subprocess.run = source_tree
sys.argv = ['offline-fixture', 'synthetic-checkout'] + ([{str(root)!r}, '12345'] if {temporary_snapshot!r} else [])
'''
                runner = root / "offline_import_fixture.py"
                if case.startswith("tempdir_"):
                    prefix += "\nimport tempfile\ntempfile.tempdir = None\n"
                    snapshot_before = {str(p.relative_to(release)): p.read_bytes()
                                       for p in release.rglob("*") if p.is_file()}
                runner.write_text(textwrap.dedent(prefix) + "\n" + fixture)
                result = subprocess.run([sys.executable, "-I", "-B", str(runner)],
                                        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
                self.assertEqual(result.stderr, "")
                self.assertNotIn("token_DO_NOT_EMIT", result.stdout)
                self.assertFalse((root / "forbidden-write").exists())
                if case.startswith("tempdir_"):
                    self.assertEqual({str(p.relative_to(release)): p.read_bytes()
                                      for p in release.rglob("*") if p.is_file()}, snapshot_before)
                    self.assertFalse((release / "synthetic_cache").exists())
                if case not in {"pyc_mismatch", "pyc_link"}:
                    self.assertFalse(list(root.rglob("__pycache__")))
                else:
                    self.assertEqual(len(list(root.rglob("*.pyc"))), 1, "existing cache is preserved")
                if case in {"valid", "pyc_mismatch", "pyc_link", "tempdir_constant"}:
                    self.assertEqual(result.returncode, 0, result.stdout)
                    receipt = json.loads(result.stdout)
                    self.assertEqual(receipt["daily_required_modules_checked"], 30)
                    self.assertGreaterEqual(receipt["aab_modules_checked"], 38)
                    self.assertEqual(receipt["guard_attempts"], dict(network=0, process=0, read=0, write=0))
                    self.assertTrue(receipt["application_import_proof"])
                    self.assertFalse(receipt["shell_selection_proof"])
                    self.assertFalse(receipt["runtime_adoption_proof"])
                    if temporary_snapshot:
                        self.assertEqual(receipt["stage"], "offline_consumer_imports_verified")
                        self.assertEqual(receipt["source_kind"], "task_temporary_snapshot")
                        self.assertEqual(receipt["source_files_checked"], 40)
                        self.assertTrue(receipt["source_snapshot_reverified"])
                        self.assertFalse(receipt["immutable_release_proof"])
                        self.assertFalse(receipt["health_recovery_proof"])
                else:
                    self.assertNotEqual(result.returncode, 0)
                    receipt = json.loads(result.stdout)
                    expected_fields = {"stage", "failure", "import_target", "guard_attempts",
                                       "application_import_proof", "runtime_adoption_proof", "residue"}
                    if temporary_snapshot:
                        expected_fields |= {"source_kind", "immutable_release_proof", "shell_selection_proof", "health_recovery_proof"}
                        self.assertEqual(receipt["source_kind"], "task_temporary_snapshot")
                        self.assertFalse(receipt["immutable_release_proof"])
                        self.assertFalse(receipt["shell_selection_proof"])
                        self.assertFalse(receipt["health_recovery_proof"])
                    self.assertEqual(set(receipt), expected_fields)
                    self.assertEqual(receipt["stage"], "offline_consumer_import_failed" if temporary_snapshot else "inactive_release_consumer_import_failed")
                    self.assertFalse(receipt["application_import_proof"])
                    self.assertFalse(receipt["runtime_adoption_proof"])
                    self.assertEqual(receipt["residue"], "preserved")
                    if case in {"network", "process", "write", "private_read", "private_listdir", "private_scandir", "source_git_read", "source_data_read", "source_ignored_read", "checkout_git_read"}:
                        kind = "read" if case.startswith(("private_", "source_", "checkout_")) else case
                        expected_counts = dict(network=0, process=0, write=0, read=0)
                        expected_counts[kind] = 1
                        self.assertEqual(receipt["guard_attempts"], expected_counts)
                        self.assertEqual(receipt["import_target"], "daily_consumer")
                    if case in {"tempdir_mkstemp", "tempdir_mkdir", "tempdir_private_read"}:
                        expected_counts = dict(network=0, process=0, write=0, read=0)
                        expected_counts["read" if case == "tempdir_private_read" else "write"] = 1
                        self.assertEqual(receipt["guard_attempts"], expected_counts)
                        self.assertEqual(receipt["import_target"], "qpk_drift")
                    if case in {"missing_dependency", "secret_exception"}:
                        self.assertEqual(receipt["import_target"], "daily_consumer")
                        self.assertEqual(receipt["guard_attempts"], dict(network=0, process=0, write=0, read=0))
                    self.assertIn(receipt["failure"], {"candidate_metadata", "source_manifest", "consumer_import", "module_origin"})
                    expected_targets = {"not_started", "daily_consumer", "daily_module_presence", "origin_validation"}
                    if case.startswith("tempdir_"):
                        expected_targets = {"qpk_drift"}
                    self.assertIn(receipt["import_target"], expected_targets)
                    self.assertNotIn("_verified", result.stdout)

    def _inactive_stage_scripts(self) -> tuple[str, list[str]]:
        workflow = (ROOT.parents[1] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        job = workflow.split("\n  stage-quant-runtime-inactive:\n", 1)[1].split("\n  release-gateway-failure-repairs:\n", 1)[0]
        scripts = [textwrap.dedent(block) for block in re.findall(r"(?m)^        run: \|\n((?:^          .*\n|^\n)*)", job)]
        self.assertEqual(len(scripts), 3)
        return workflow, scripts

    def _public_source_fixture(self, root: Path, script: str, behavior: str = "ok"):
        """Intercept every new source Git command before executing a workflow fixture."""
        root.mkdir(parents=True, exist_ok=True)
        command_bin = root / "command-bin"
        command_bin.mkdir()
        runner_temp = root / "runner-temp"
        runner_temp.mkdir()
        events = root / "git-events.jsonl"
        git = command_bin / "git"
        git.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\nfrom pathlib import Path\n"
            f"events = Path({str(events)!r})\n"
            f"runner_temp = Path({str(runner_temp)!r})\n"
            f"behavior = {behavior!r}\n"
            "args = sys.argv[1:]\n"
            "record = {'argv': args, 'gh_token_present': 'GH_TOKEN' in os.environ,\n"
            "          'env': {k: v for k, v in os.environ.items() if k.startswith('GIT_') or k in ('HOME', 'HTTPS_PROXY', 'REQUESTS_CA_BUNDLE')}}\n"
            "with events.open('a') as stream: stream.write(json.dumps(record) + '\\n')\n"
            "if args[:2] == ['init', '--template='] and len(args) == 3:\n"
            "    target = Path(args[2])\n"
            "    assert target.parent == runner_temp\n"
            "    if behavior == 'init_failure': raise SystemExit(23)\n"
            "    (target / '.git').mkdir()\n"
            "    raise SystemExit(0)\n"
            "assert len(args) >= 4 and args[0] == '-C' and Path(args[1]).parent == runner_temp\n"
            "pin = '28675796cabbe137a1fa3970b70d1aa98e952c88'\n"
            "if args[2] == 'fetch':\n"
            "    assert args[3:] == ['--depth=1', '--no-tags', '--no-recurse-submodules',\n"
            "                         'https://github.com/QuantStrategyLab/QuantPlatformKit.git', pin]\n"
            "    if behavior in {'http_403', 'timeout'}:\n"
            "        print('synthetic HTTP 403 token_DO_NOT_EMIT /private/fixture', file=sys.stderr)\n"
            "        raise SystemExit(124 if behavior == 'timeout' else 128)\n"
            "    raise SystemExit(0)\n"
            "assert args[2:] == ['cat-file', '-t', pin]\n"
            "if behavior == 'missing_object': raise SystemExit(128)\n"
            "print('tree' if behavior == 'wrong_object' else 'commit')\n",
            encoding="utf-8",
        )
        git.chmod(0o755)
        return (script.replace("PATH=/usr/bin:/bin", f"PATH={command_bin}:/usr/bin:/bin"),
                {"RUNNER_TEMP": str(runner_temp), "GITHUB_RUN_ID": "12345"}, events)

    def test_inactive_stage_public_source_is_anonymous_fixed_and_fails_closed(self) -> None:
        _, scripts = self._inactive_stage_scripts()
        for behavior in ("ok", "init_failure", "http_403", "timeout", "missing_object", "wrong_object",
                         "source_exists", "candidate_exists", "inside_checkout", "invalid_run_id"):
            with self.subTest(behavior=behavior), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "checkout"
                setup = workspace / "ops/quant-monitor/scripts/setup_vps_runtime.sh"
                setup.parent.mkdir(parents=True)
                receipt = root / "setup-source"
                setup.write_text("#!/bin/bash\nset -euo pipefail\n"
                                 f'printf %s "$QUANT_PLATFORM_KIT_ROOT" > {shlex.quote(str(receipt))}\n')
                candidate = root / "candidate"
                stage = scripts[1].replace("/home/ubuntu/quant-monitor-data03-286757-py312-v1", str(candidate))
                stage = stage.replace("HOME=/home/ubuntu", f"HOME={root}")
                stage, fixture_env, events = self._public_source_fixture(root / "commands", stage, behavior)
                if behavior == "inside_checkout":
                    fixture_env["RUNNER_TEMP"] = str(workspace)
                if behavior == "invalid_run_id":
                    fixture_env["GITHUB_RUN_ID"] = "not-a-run-id"
                source = Path(fixture_env["RUNNER_TEMP"]) / "quant-monitor-data03-qpk-286757-12345"
                shared = root / "shared-mirror"
                shared.mkdir()
                (shared / "HEAD").write_text("preserve-original")
                (root / ".netrc").write_text("machine github.com login synthetic password DO_NOT_USE\n")
                if behavior == "source_exists":
                    source.mkdir()
                    (source / "residue").write_text("preserve-residue")
                if behavior == "candidate_exists":
                    candidate.mkdir()
                    (candidate / "residue").write_text("preserve-residue")
                poisoned = "export PYTHONPATH=/synthetic/old\nexport GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.https://unreviewed.invalid/.insteadOf GIT_CONFIG_VALUE_0=https://github.com/\n"
                result = subprocess.run(
                    ["/bin/bash", "-c", poisoned + stage],
                    env={"PATH": "/usr/bin:/bin", "HOME": str(root), "GH_TOKEN": "synthetic-token",
                         "GITHUB_WORKSPACE": str(workspace), "RUN_SHA": "a" * 40,
                         "HTTPS_PROXY": "https://proxy.invalid", "REQUESTS_CA_BUNDLE": "/synthetic/ca.pem", **fixture_env},
                    capture_output=True, text=True, timeout=5, check=False,
                )
                records = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
                calls = [r["argv"][0] if r["argv"][0] == "init" else r["argv"][2] for r in records]
                expected_calls = (["init", "fetch", "cat-file"] if behavior in {"ok", "missing_object", "wrong_object"}
                                  else ["init", "fetch"] if behavior in {"http_403", "timeout"}
                                  else ["init"] if behavior == "init_failure" else [])
                self.assertEqual(calls, expected_calls, "no fallback, retry, checkout or later phase after failure")
                self.assertEqual(result.returncode == 0, behavior == "ok", result.stderr)
                self.assertEqual(receipt.exists(), behavior == "ok")
                self.assertEqual((shared / "HEAD").read_text(), "preserve-original")
                self.assertNotIn("token_DO_NOT_EMIT", result.stdout + result.stderr)
                self.assertNotIn("/private/fixture", result.stdout + result.stderr)
                self.assertNotIn("inactive_candidate_metadata_verified", result.stdout + result.stderr)
                expected_failure = {"init_failure": ("git_init", 23), "http_403": ("git_fetch", 128),
                                    "timeout": ("timeout", 124), "missing_object": ("git_object", 128),
                                    "wrong_object": ("git_object", 1), "source_exists": ("source_directory", 1)}
                if behavior in expected_failure:
                    failure_enum, status = expected_failure[behavior]
                    self.assertEqual(result.returncode, status)
                    self.assertEqual(result.stderr,
                                     f"[inactive-stage] qpk_source_failure={failure_enum}; candidate untouched\n")
                    self.assertTrue(source.exists(), "new source residue is not automatically removed")
                if behavior == "ok":
                    self.assertEqual(receipt.read_text(), str(source))
                    self.assertFalse((source / "src").exists(), "object preparation never checks out files")
                if behavior in {"inside_checkout", "invalid_run_id"}:
                    self.assertEqual(result.stderr, "[inactive-stage] qpk_source_failure=source_directory; candidate untouched\n")
                    self.assertFalse((workspace / "quant-monitor-data03-qpk-286757-12345").exists())
                if behavior in {"source_exists", "candidate_exists"}:
                    self.assertEqual(records, [])
                    existing = source if behavior == "source_exists" else candidate
                    self.assertEqual((existing / "residue").read_text(), "preserve-residue")
                if behavior == "timeout":
                    self.assertEqual(result.returncode, 124)
                for record in records:
                    self.assertEqual(record["argv"][-1] if record["argv"][0] == "init" else record["argv"][1], str(source))
                    self.assertFalse(record["gh_token_present"])
                    git_env = record["env"]
                    self.assertEqual(git_env["HOME"], str(source), "public Git cannot consume the caller's .netrc")
                    self.assertFalse((source / ".netrc").exists())
                    self.assertEqual(git_env["GIT_CONFIG_GLOBAL"], "/dev/null")
                    self.assertEqual(git_env["GIT_CONFIG_NOSYSTEM"], "1")
                    self.assertEqual(git_env["GIT_ALLOW_PROTOCOL"], "https")
                    self.assertEqual(git_env["GIT_TERMINAL_PROMPT"], "0")
                    self.assertEqual(git_env["HTTPS_PROXY"], "https://proxy.invalid")
                    self.assertEqual(git_env["REQUESTS_CA_BUNDLE"], "/synthetic/ca.pem")
                    policy = {git_env[f"GIT_CONFIG_KEY_{i}"]: git_env[f"GIT_CONFIG_VALUE_{i}"]
                              for i in range(int(git_env["GIT_CONFIG_COUNT"]))}
                    self.assertEqual(policy, {"core.hooksPath": "/dev/null", "core.fsmonitor": "false",
                                              "credential.helper": "", "http.sslVerify": "true", "http.followRedirects": "false"})

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
        commands = "\n".join(line for line in scripts[1].splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(commands, r"\brm\b|\bchmod\b|\bchown\b|\bcheckout\b")
        self.assertNotIn("/data/lifecycle-projects", scripts[1])
        self.assertIn("GIT_ALLOW_PROTOCOL=https", scripts[1])
        self.assertIn("--kill-after=5s 60s git", scripts[1])
        self.assertIn("qpk_source_sha=" + (ROOT / "qpk-runtime.sha").read_text().strip(), scripts[1])
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
            stage, source_env, _ = self._public_source_fixture(root / "commands", stage)
            captured_names = ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "BASH_ENV", "UNRELATED_SECRET",
                              "PYTHONNOUSERSITE", "PIP_CONFIG_FILE", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL",
                              "PIP_TRUSTED_HOST", "HTTPS_PROXY", "REQUESTS_CA_BUNDLE", "GH_TOKEN")
            capture = "\n".join(f'printf "%s=%s\\n" {name} "${{{name}-unset}}"' for name in captured_names)
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "GITHUB_WORKSPACE": str(workspace),
                   "RUN_SHA": "a" * 40, "GH_TOKEN": "synthetic-token",
                   "HTTPS_PROXY": "https://proxy.invalid", "REQUESTS_CA_BUNDLE": "/synthetic/ca.pem", **source_env}
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
            env["GITHUB_RUN_ID"] = "12346"
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
                    stage, source_env, _ = self._public_source_fixture(root / f"commands-{number}", stage)
                    # Real setup is not executed: only fixture output and residue.
                    payload = secret_text + "\n" + message + "\n"
                    setup.write_text("#!/bin/bash\nset -euo pipefail\n"
                                     'mkdir "$QUANT_MONITOR_VENV"\n'
                                     'printf original > "$QUANT_MONITOR_VENV/residue"\n'
                                     f"printf %s {shlex.quote(payload)} >&2\nexit {exit_code}\n")
                    result = subprocess.run(
                        ["/bin/bash", "-c", "export PYTHONPATH=/synthetic/old\n" + stage],
                        env={"PATH": "/usr/bin:/bin", "HOME": str(root), "GH_TOKEN": "synthetic-token",
                             "GITHUB_WORKSPACE": str(workspace), "RUN_SHA": "a" * 40, **source_env},
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
