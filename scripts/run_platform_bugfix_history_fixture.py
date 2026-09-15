#!/usr/bin/env python3
"""Run the fixed LongBridge platform-bugfix fixture through the real bridge gate."""

from __future__ import annotations

import difflib
import hashlib
import subprocess
import tempfile
from pathlib import Path

from scripts.run_monthly_codex_audit import BridgeError, apply_service_changes


SOURCE_REPO = "https://github.com/QuantStrategyLab/LongBridgePlatform.git"
BASELINE_SHA = "26844fa9b909e98e867acf4c50120b90acc6c792"
FIXED_SHA = "e6f08e8f09c4f05ed42feaf6479086b5dcd58d09"
ALLOWED = ("application/rebalance_service.py", "tests/test_rebalance_service.py")


def targeted_changes(path: str, original: str, replacement: str) -> dict[str, object]:
    old_lines = original.splitlines(keepends=True)
    new_lines = replacement.splitlines(keepends=True)
    opcodes = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False).get_opcodes()
    changed = [opcode for opcode in opcodes if opcode[0] != "equal"]
    if not changed:
        raise RuntimeError(f"historical fixture has no change for {path}")
    groups: list[list[int]] = []
    for _tag, i1, i2, j1, j2 in changed:
        if groups and i1 - groups[-1][1] <= 6:
            groups[-1][1] = max(groups[-1][1], i2)
            groups[-1][3] = max(groups[-1][3], j2)
        else:
            groups.append([i1, i2, j1, j2])
    edits: list[dict[str, str]] = []
    for start, end, new_start, new_end in groups:
        old_start = max(0, start - 3)
        old_end = min(len(old_lines), end + 3)
        new_start = max(0, new_start - 3)
        new_end = min(len(new_lines), new_end + 3)
        old_text = "".join(old_lines[old_start:old_end])
        new_text = "".join(new_lines[new_start:new_end])
        while original.count(old_text) != 1:
            if old_start > 0:
                old_start -= 1
                new_start = max(0, new_start - 1)
                old_text = "".join(old_lines[old_start:old_end])
                new_text = "".join(new_lines[new_start:new_end])
            elif old_end < len(old_lines):
                old_end += 1
                new_end = min(len(new_lines), new_end + 1)
                old_text = "".join(old_lines[old_start:old_end])
                new_text = "".join(new_lines[new_start:new_end])
            else:
                raise RuntimeError(f"historical fixture edit context is not unique for {path}")
        edits.append({"old": old_text, "new": new_text})
    return {
        "path": path,
        "base_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "edits": edits,
    }


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
        changes = [
            targeted_changes(
                path,
                (positive / path).read_text(encoding="utf-8"),
                (fixed / path).read_text(encoding="utf-8"),
            )
            for path in ALLOWED
        ]

        apply_service_changes(positive, changes, task="platform_bugfix")

        old_implementation = (negative / ALLOWED[0]).read_text(encoding="utf-8")
        fixed_tests = (fixed / ALLOWED[1]).read_text(encoding="utf-8")
        try:
            apply_service_changes(
                negative,
                [
                    # Keep the old implementation's behavior while making a
                    # real, bounded edit so the targeted contract is exercised.
                    targeted_changes(ALLOWED[0], old_implementation, old_implementation + "\n"),
                    targeted_changes(
                        ALLOWED[1],
                        (negative / ALLOWED[1]).read_text(encoding="utf-8"),
                        fixed_tests,
                    ),
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
