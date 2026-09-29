import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployScriptTests(unittest.TestCase):
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
            script.index('python3 -m venv "$VENV"'),
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
                "import sys\n"
                "from pathlib import Path\n"
                f"log = Path({str(pip_log)!r})\n"
                "prev = log.read_text() if log.exists() else ''\n"
                "log.write_text(prev + ' '.join(sys.argv[1:]) + '\\n')\n"
                "args = sys.argv[1:]\n"
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
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
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
                "if args == ['-']:\n"
                "    code = sys.stdin.read()\n"
                "    import platform\n"
                "    sys.version_info = (3, 12, 0, 'final', 0)\n"
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
