import os
import subprocess
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

        self.assertIn('MIRROR_ROOT="${QUANT_PROJECTS_ROOT:-$ROOT/data/lifecycle-projects}"', script)
        self.assertIn('dir="$MIRROR_ROOT/$repo"', script)
        self.assertIn("checkout --detach --quiet origin/main", script)
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
        aab_sync = script[script.index('if [[ ! -d "$AAB_ROOT/.git" ]]'):script.index('VENV=')]
        self.assertIn('SOURCE_SHA="${1:-}"', script)
        self.assertIn('EXPECTED_AAB_ROOT="${2:-}"', script)
        self.assertIn('[[ "$AAB_ROOT" != "$EXPECTED_AAB_ROOT" ]]', script)
        self.assertIn('rev-parse HEAD', aab_sync)
        self.assertIn('"$ACTUAL_AAB_SHA" != "$SOURCE_SHA"', aab_sync)
        self.assertIn('status --porcelain --untracked-files=all -- client scripts service ops/quant-monitor/scripts ops/quant-monitor/systemd', aab_sync)
        self.assertLess(script.index('rev-parse HEAD'), script.index('git -C "$QPK_ROOT" fetch origin main'))
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


if __name__ == "__main__":
    unittest.main()
