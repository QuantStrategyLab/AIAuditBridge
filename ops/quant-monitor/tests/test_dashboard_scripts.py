import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DashboardScriptTests(unittest.TestCase):
    def test_refresh_supports_legacy_cli_without_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            lifecycle = fake_bin / "quant-lifecycle"
            lifecycle.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$*\" == *\"--help\"* ]]; then echo 'dashboard --format'; exit 0; fi\n"
                "mkdir -p dashboard_output\n"
                "printf '%s' '{\"computed_at\":\"2026-07-11T00:00:00+00:00\",\"strategies\":[]}' > dashboard_output/strategy_health_dashboard.json\n",
                encoding="utf-8",
            )
            lifecycle.chmod(0o755)
            env = os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "QUANT_MONITOR_ROOT": str(root),
            }
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/refresh_strategy_health.sh")],
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads((root / "data/health/strategy_health_dashboard.v1.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], "strategy_health_dashboard.v1")
            self.assertFalse((root / "data/health/dashboard/.legacy-dashboard-output").exists())

    def test_publish_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/publish_strategy_health.sh")],
                env=os.environ | {"QUANT_MONITOR_ROOT": str(root)},
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0)
        self.assertIn("disabled", result.stderr)

    def test_publish_keeps_sync_token_out_of_curl_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data" / "health" / "strategy_health_dashboard.v1.json"
            data.parent.mkdir(parents=True)
            data.write_text(json.dumps({"schema_version": "strategy_health_dashboard.v1"}), encoding="utf-8")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s' \"$*\" > \"$CURL_ARGV\"\n"
                "cat > \"$CURL_CONFIG\"\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            argv_path = root / "curl.argv"
            config_path = root / "curl.config"
            token = " ".join(("dedicated", "sync", "value"))
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/publish_strategy_health.sh")],
                env=os.environ | {
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "QUANT_MONITOR_ROOT": str(root),
                    "STRATEGY_HEALTH_PUBLISH": "1",
                    "STRATEGY_HEALTH_SYNC_URL": "https://example.invalid/sync",
                    "STRATEGY_HEALTH_SYNC_TOKEN": token,
                    "CURL_ARGV": str(argv_path),
                    "CURL_CONFIG": str(config_path),
                },
                capture_output=True,
                text=True,
            )
            curl_argv = argv_path.read_text(encoding="utf-8")
            curl_config = config_path.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, curl_argv)
        self.assertIn(token, curl_config)
        self.assertIn(f'header = "Authorization: Bearer {token}"', curl_config)


if __name__ == "__main__":
    unittest.main()
