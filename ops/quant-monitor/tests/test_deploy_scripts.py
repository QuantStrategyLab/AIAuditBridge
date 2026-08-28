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

    def test_health_check_syncs_artifacts_before_monitoring(self) -> None:
        script = (ROOT / "scripts" / "health_check.sh").read_text(encoding="utf-8")

        self.assertLess(
            script.index("sync_lifecycle_artifacts.py"),
            script.index("health_cycle.py"),
        )

    def test_health_check_publishes_the_snapshot_before_returning_an_alert_status(self) -> None:
        script = (ROOT / "scripts" / "health_check.sh").read_text(encoding="utf-8")

        self.assertIn('health_cycle_status=$?', script)
        self.assertLess(
            script.index("health_cycle.py"),
            script.index("publish_strategy_health.sh"),
        )
        self.assertIn('exit "$health_cycle_status"', script)

    def test_deploy_script_normalizes_chat_id_and_excludes_bytecode(self) -> None:
        script = (ROOT / "scripts" / "deploy_to_vps.sh").read_text(encoding="utf-8")

        self.assertIn(
            "s/^Environment=(GLOBAL_TELEGRAM_CHAT_ID=)+//",
            script,
        )
        self.assertIn("--exclude '__pycache__/'", script)
        self.assertIn("--exclude '*.py[co]'", script)

    def test_deploy_script_only_accepts_monitor_alert_exit(self) -> None:
        script = (ROOT / "scripts" / "deploy_to_vps.sh").read_text(encoding="utf-8")

        self.assertNotIn("sudo systemctl start codex-quant.service || true", script)
        self.assertIn("ExecMainStatus", script)
        self.assertIn('if [[ "$monitor_status" != "2" ]]', script)

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
        self.assertIn("STRATEGY_HEALTH_SYNC_TOKEN_FILE=/etc/codex-quant/strategy-health.sync.token", drop_in)
        self.assertNotIn("STRATEGY_HEALTH_SYNC_TOKEN=", drop_in)


if __name__ == "__main__":
    unittest.main()
