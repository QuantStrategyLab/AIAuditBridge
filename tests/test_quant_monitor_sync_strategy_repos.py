from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


class SyncStrategyReposTests(unittest.TestCase):
    def test_exits_nonzero_when_a_repo_pull_fails(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "ops" / "quant-monitor" / "scripts" / "sync_strategy_repos.sh"

        with tempfile.TemporaryDirectory() as tmp:
            projects_root = Path(tmp) / "Projects"
            bin_dir = Path(tmp) / "bin"
            projects_root.mkdir()
            bin_dir.mkdir()

            for name in [
                "QuantPlatformKit",
                "CnEquityStrategies",
                "HkEquityStrategies",
                "UsEquityStrategies",
                "CryptoStrategies",
            ]:
                (projects_root / name / ".git").mkdir(parents=True)

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
                    if repo == "QuantPlatformKit" and command == "pull":
                        print("error: simulated pull failure", file=sys.stderr)
                        raise SystemExit(1)
                    raise SystemExit(0)
                    """
                ),
                encoding="utf-8",
            )
            git_stub.chmod(0o755)

            env = {
                **os.environ,
                "PROJECTS_ROOT": str(projects_root),
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
        self.assertIn("[sync] QuantPlatformKit pull failed", completed.stderr)
        self.assertIn("[sync] CnEquityStrategies ok", completed.stdout)
        self.assertNotIn("[sync] QuantPlatformKit ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
