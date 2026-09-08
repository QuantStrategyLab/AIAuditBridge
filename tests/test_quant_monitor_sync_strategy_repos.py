from __future__ import annotations

import os
import subprocess
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path


class SyncStrategyReposTests(unittest.TestCase):
    def test_exits_nonzero_when_a_repo_fetch_fails(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "ops" / "quant-monitor" / "scripts" / "sync_strategy_repos.sh"

        with tempfile.TemporaryDirectory() as tmp:
            mirror_root = Path(tmp) / "lifecycle-projects"
            bin_dir = Path(tmp) / "bin"
            mirror_root.mkdir()
            bin_dir.mkdir()

            for name in [
                "QuantPlatformKit",
                "CnEquityStrategies",
                "HkEquityStrategies",
                "UsEquityStrategies",
                "CryptoStrategies",
            ]:
                (mirror_root / name / ".git").mkdir(parents=True)

            git_stub = bin_dir / "git"
            git_stub.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    from __future__ import annotations

                    import os
                    import sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    repo = None
                    command = None
                    if len(args) >= 3 and args[0] == "-C":
                        repo = Path(args[1]).name
                        command = args[2]
                    if repo == "QuantPlatformKit" and command == "fetch":
                        print("error: simulated fetch failure", file=sys.stderr)
                        raise SystemExit(1)
                    raise SystemExit(0)
                    """
                ),
                encoding="utf-8",
            )
            git_stub.chmod(0o755)

            env = {
                **os.environ,
                "QUANT_PROJECTS_ROOT": str(mirror_root),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            }
            completed = subprocess.run(
                ["bash", str(script)],
                cwd=repo_root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("[sync] QuantPlatformKit fetch failed", completed.stderr)
        self.assertIn("[sync] CnEquityStrategies ok", completed.stdout)
        self.assertNotIn("[sync] QuantPlatformKit ok", completed.stdout)


    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], text=True, capture_output=True, check=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        return result.stdout.strip()

    def _local_mirrors(self, root: Path) -> tuple[Path, Path, str]:
        upstream = root / "upstream"
        upstream.mkdir()
        self._git(upstream, "init", "--initial-branch=main")
        self._git(upstream, "config", "user.name", "Test")
        self._git(upstream, "config", "user.email", "test@example.invalid")
        metadata = upstream / "src/quant_platform_kit.egg-info"
        metadata.mkdir(parents=True)
        (metadata / "PKG-INFO").write_text("old metadata\n")
        (metadata / "SOURCES.txt").write_text("old sources\n")
        (upstream / "module.py").write_text("VERSION = 1\n")
        (upstream / "README.md").write_text("original\n")
        self._git(upstream, "add", ".")
        self._git(upstream, "commit", "-m", "base")
        mirrors = root / "mirrors"
        mirrors.mkdir()
        for repo in ("QuantPlatformKit", "CnEquityStrategies", "HkEquityStrategies", "UsEquityStrategies", "CryptoStrategies"):
            self._git(root, "clone", str(upstream), str(mirrors / repo))
        (metadata / "PKG-INFO").write_text("new metadata\n")
        (metadata / "SOURCES.txt").write_text("new sources\n")
        (upstream / "module.py").write_text("VERSION = 2\n")
        self._git(upstream, "commit", "-am", "updated")
        return mirrors, upstream, self._git(upstream, "rev-parse", "HEAD")

    def _sync(self, mirrors: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        script = Path(__file__).resolve().parents[1] / "ops/quant-monitor/scripts/sync_strategy_repos.sh"
        return subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, check=False,
            env={**os.environ, "QUANT_PROJECTS_ROOT": str(mirrors), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **(extra_env or {})},
        )

    def test_packaging_metadata_is_preserved_while_clean_mirror_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mirrors, _, latest = self._local_mirrors(Path(tmp))
            qpk = mirrors / "QuantPlatformKit"
            relative = "src/quant_platform_kit.egg-info/PKG-INFO"
            (qpk / relative).write_text("generated installation metadata\n")
            (qpk / "private-local-state").write_text("preserve me\n")
            before = self._git(qpk, "rev-parse", "HEAD")
            result = self._sync(mirrors)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._git(qpk, "rev-parse", "HEAD"), latest)
            self.assertEqual(self._git(qpk, "status", "--porcelain"), "")
            backup = mirrors / "QuantPlatformKit.preserved-before-metadata-refresh"
            self.assertEqual(self._git(backup, "rev-parse", "HEAD"), before)
            self.assertEqual((backup / relative).read_text(), "generated installation metadata\n")
            self.assertEqual((backup / "private-local-state").read_text(), "preserve me\n")
            again = self._sync(mirrors)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(len(list(mirrors.glob("*.preserved-before-metadata-refresh"))), 1)
            (qpk / relative).write_text("unexpected repeated dirty metadata\n")
            blocked = self._sync(mirrors)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertEqual((qpk / relative).read_text(), "unexpected repeated dirty metadata\n")
            self.assertEqual((backup / relative).read_text(), "generated installation metadata\n")

    def test_unknown_source_edits_are_not_carried_into_updated_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mirrors, _, _ = self._local_mirrors(Path(tmp))
            qpk = mirrors / "QuantPlatformKit"
            # This file does not conflict with the incoming commit. Plain checkout
            # carries it across and falsely declares a clean monitor source.
            (qpk / "README.md").write_text("unknown local change\n")
            before = self._git(qpk, "rev-parse", "HEAD")
            result = self._sync(mirrors)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self._git(qpk, "rev-parse", "HEAD"), before)
            self.assertEqual((qpk / "README.md").read_text(), "unknown local change\n")
            self.assertFalse((mirrors / "QuantPlatformKit.preserved-before-metadata-refresh").exists())

    def test_replacement_failure_restores_original_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirrors, _, _ = self._local_mirrors(root)
            qpk = mirrors / "QuantPlatformKit"
            metadata = qpk / "src/quant_platform_kit.egg-info/PKG-INFO"
            metadata.write_text("local metadata\n")
            before = self._git(qpk, "rev-parse", "HEAD")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "mv"
            stub.write_text(
                "#!/usr/bin/env python3\nimport os,sys\n"
                "if '.QuantPlatformKit.refresh.' in sys.argv[2]: raise SystemExit(1)\n"
                f"os.execv({shutil.which('mv')!r}, ['mv', *sys.argv[1:]])\n"
            )
            stub.chmod(0o755)
            result = self._sync(mirrors, {"PATH": f"{bin_dir}:{os.environ['PATH']}"})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self._git(qpk, "rev-parse", "HEAD"), before)
            self.assertEqual(metadata.read_text(), "local metadata\n")
            self.assertFalse((mirrors / "QuantPlatformKit.preserved-before-metadata-refresh").exists())
            self.assertEqual(list(mirrors.glob(".QuantPlatformKit.refresh.*")), [])

    def test_bootstrap_build_metadata_is_written_outside_source_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mirrors, upstream, _ = self._local_mirrors(root)
            qpk = mirrors / "QuantPlatformKit"
            aab = root / "AIAuditBridge"
            self._git(root, "clone", str(upstream), str(aab))
            monitor = root / "monitor"
            (monitor / "scripts").mkdir(parents=True)
            scripts = Path(__file__).resolve().parents[1] / "ops/quant-monitor/scripts"
            shutil.copyfile(scripts / "common_env.sh", monitor / "scripts/common_env.sh")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            python_stub = bin_dir / "python3"
            pip_code = (
                "#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n"
                "if '-e' in sys.argv: raise SystemExit(9)\n"
                "source=Path(sys.argv[-1])\n"
                "if source.is_dir():\n"
                "    (source/'src/quant_platform_kit.egg-info/PKG-INFO').write_text('generated')\n"
            )
            # Use the actual interpreter in generated scripts, bypassing this venv stub.
            import sys
            pip_code = pip_code.replace("#!/usr/bin/env python3", f"#!{sys.executable}")
            python_stub.write_text(
                f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
                "venv=Path(sys.argv[-1]); (venv/'bin').mkdir(parents=True,exist_ok=True)\n"
                f"(venv/'bin/pip').write_text({pip_code!r})\n"
                "(venv/'bin/pip').chmod(0o755)\n"
            )
            python_stub.chmod(0o755)
            for name in ("gh", "gcloud"):
                (bin_dir / name).write_text("#!/bin/sh\nexit 0\n")
                (bin_dir / name).chmod(0o755)
            result = subprocess.run(
                ["bash", str(scripts / "setup_vps_runtime.sh")], text=True, capture_output=True,
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                     "QUANT_MONITOR_ROOT": str(monitor), "QUANT_PLATFORM_KIT_ROOT": str(qpk),
                     "AIAUDIT_BRIDGE_ROOT": str(aab), "QUANT_PROJECTS_ROOT": str(mirrors),
                     "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._git(qpk, "status", "--porcelain"), "")
            self.assertEqual((qpk / "src/quant_platform_kit.egg-info/PKG-INFO").read_text(), "new metadata\n")


if __name__ == "__main__":
    unittest.main()
