#!/usr/bin/env python3
"""Run the fixed LongBridge platform-bugfix fixture through the real bridge gate."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from scripts.run_monthly_codex_audit import BridgeError, apply_service_changes


SOURCE_REPO = "https://github.com/QuantStrategyLab/LongBridgePlatform.git"
BASELINE_SHA = "26844fa9b909e98e867acf4c50120b90acc6c792"
FIXED_SHA = "e6f08e8f09c4f05ed42feaf6479086b5dcd58d09"
ALLOWED = ("application/rebalance_service.py", "tests/test_rebalance_service.py")


def run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, timeout=300)


def checkout_fixture(repo: Path, worktree: Path, revision: str) -> None:
    run("git", "worktree", "add", "--detach", str(worktree), revision, cwd=repo)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aab-longbridge-history-") as raw:
        root = Path(raw)
        repo = root / "repo"
        run("git", "init", str(repo))
        run("git", "remote", "add", "origin", SOURCE_REPO, cwd=repo)
        run("git", "fetch", "--no-tags", "--depth=1", "origin", BASELINE_SHA, FIXED_SHA, cwd=repo)
        positive = root / "positive"
        negative = root / "negative"
        fixed = root / "fixed"
        checkout_fixture(repo, positive, BASELINE_SHA)
        checkout_fixture(repo, negative, BASELINE_SHA)
        checkout_fixture(repo, fixed, FIXED_SHA)
        changes = [{"path": path, "content": (fixed / path).read_text(encoding="utf-8")} for path in ALLOWED]

        apply_service_changes(positive, changes, task="platform_bugfix")

        old_implementation = (negative / ALLOWED[0]).read_text(encoding="utf-8")
        fixed_tests = (fixed / ALLOWED[1]).read_text(encoding="utf-8")
        try:
            apply_service_changes(
                negative,
                [
                    {"path": ALLOWED[0], "content": old_implementation},
                    {"path": ALLOWED[1], "content": fixed_tests},
                ],
                task="platform_bugfix",
            )
        except BridgeError as exc:
            if "bounded test failed" not in str(exc):
                raise RuntimeError(f"negative fixture failed before bounded test: {exc}") from exc
        else:
            raise RuntimeError("negative fixture unexpectedly passed the bounded regression")
        finally:
            for worktree in (positive, negative, fixed):
                if worktree.exists():
                    subprocess.run(
                        ("git", "worktree", "remove", "--force", str(worktree)),
                        cwd=repo,
                        check=False,
                        timeout=60,
                    )
        print("LongBridge historical platform-bugfix fixtures passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
