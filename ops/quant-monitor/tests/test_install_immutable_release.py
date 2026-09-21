"""Contract and functional tests for install-only immutable release."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_immutable_release.sh"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _init_monitor_repo(repo: Path) -> str:
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    monitor = repo / "ops" / "quant-monitor"
    scripts = monitor / "scripts"
    scripts.mkdir(parents=True)
    (monitor / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (monitor / "README.md").write_text("readme\n", encoding="utf-8")
    (scripts / "common_env.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "health_check.sh").write_text("#!/usr/bin/env bash\necho health\n", encoding="utf-8")
    (scripts / "daily_briefing_pipeline.sh").write_text(
        "#!/usr/bin/env bash\necho briefing\n", encoding="utf-8"
    )
    (scripts / "tracked_payload.txt").write_text("committed-payload\n", encoding="utf-8")
    (monitor / ".gitignore").write_text("data/\n.venv/\n", encoding="utf-8")
    _git(repo, "add", "ops/quant-monitor")
    _git(repo, "commit", "-m", "seed monitor tree")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _run_install(
    *,
    sha: str,
    repo: Path,
    release_root: Path,
    runtime_data: Path,
    runtime_venv: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in (
        "QUANT_MONITOR_RELEASE_SHA",
        "QUANT_MONITOR_REPO_ROOT",
        "AIAUDIT_BRIDGE_ROOT",
        "QUANT_MONITOR_RELEASE_ROOT",
        "QUANT_MONITOR_RUNTIME_DATA",
        "QUANT_MONITOR_RUNTIME_VENV",
    ):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--sha",
            sha,
            "--repo",
            str(repo),
            "--release-root",
            str(release_root),
            "--runtime-data",
            str(runtime_data),
            "--runtime-venv",
            str(runtime_venv),
        ],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def fixture_paths(tmp_path: Path) -> dict[str, Path | str]:
    repo = tmp_path / "repo"
    sha = _init_monitor_repo(repo)
    release_root = tmp_path / "releases"
    release_root.mkdir()
    runtime_data = tmp_path / "runtime" / "data"
    runtime_venv = tmp_path / "runtime" / ".venv"
    runtime_data.mkdir(parents=True)
    runtime_venv.mkdir(parents=True)
    (runtime_data / "keep.txt").write_text("runtime-data\n", encoding="utf-8")
    (runtime_venv / "keep.txt").write_text("runtime-venv\n", encoding="utf-8")
    return {
        "repo": repo,
        "sha": sha,
        "release_root": release_root,
        "runtime_data": runtime_data,
        "runtime_venv": runtime_venv,
    }


class TestInstallImmutableRelease:
    def test_script_contract_forbids_remote_and_service_actions(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        assert "git archive" in source
        assert "systemctl" not in source
        assert "daemon-reload" not in source
        assert "git fetch" not in source
        assert "git pull" not in source
        assert "pull --ff-only" not in source
        assert "rsync" not in source
        assert "workflow" not in source
        assert "telegram" not in source.lower()
        assert 'rm -rf -- "${staged}"' in source
        assert "not performed" in source
        assert "chmod -R" not in source

    def test_rejects_invalid_and_missing_sha(self, fixture_paths: dict[str, Path | str]) -> None:
        repo = fixture_paths["repo"]
        release_root = fixture_paths["release_root"]
        runtime_data = fixture_paths["runtime_data"]
        runtime_venv = fixture_paths["runtime_venv"]

        invalid = _run_install(
            sha="main",
            repo=repo,  # type: ignore[arg-type]
            release_root=release_root,  # type: ignore[arg-type]
            runtime_data=runtime_data,  # type: ignore[arg-type]
            runtime_venv=runtime_venv,  # type: ignore[arg-type]
        )
        assert invalid.returncode != 0
        assert "40-hex" in invalid.stderr

        missing = _run_install(
            sha="a" * 40,
            repo=repo,  # type: ignore[arg-type]
            release_root=release_root,  # type: ignore[arg-type]
            runtime_data=runtime_data,  # type: ignore[arg-type]
            runtime_venv=runtime_venv,  # type: ignore[arg-type]
        )
        assert missing.returncode != 0
        assert "commit not found" in missing.stderr
        assert list(Path(release_root).iterdir()) == []

    def test_installs_exact_archive_and_links_runtime_paths(
        self, fixture_paths: dict[str, Path | str]
    ) -> None:
        repo = Path(fixture_paths["repo"])
        sha = str(fixture_paths["sha"])
        release_root = Path(fixture_paths["release_root"])
        runtime_data = Path(fixture_paths["runtime_data"])
        runtime_venv = Path(fixture_paths["runtime_venv"])

        dirty = repo / "ops" / "quant-monitor" / "scripts" / "tracked_payload.txt"
        dirty.write_text("dirty-uncommitted\n", encoding="utf-8")
        (repo / "untracked-secret").write_text("secret\n", encoding="utf-8")

        result = _run_install(
            sha=sha,
            repo=repo,
            release_root=release_root,
            runtime_data=runtime_data,
            runtime_venv=runtime_venv,
        )
        assert result.returncode == 0, result.stderr
        release = release_root / sha
        payload = release / "ops" / "quant-monitor" / "scripts" / "tracked_payload.txt"
        assert payload.read_text(encoding="utf-8") == "committed-payload\n"
        assert release.stat().st_mode & 0o555 == 0o555
        assert not (release / "untracked-secret").exists()
        data_link = release / "ops" / "quant-monitor" / "data"
        venv_link = release / "ops" / "quant-monitor" / ".venv"
        assert data_link.is_symlink()
        assert venv_link.is_symlink()
        assert data_link.resolve() == runtime_data.resolve()
        assert venv_link.resolve() == runtime_venv.resolve()
        assert (runtime_data / "keep.txt").read_text(encoding="utf-8") == "runtime-data\n"
        assert (runtime_venv / "keep.txt").read_text(encoding="utf-8") == "runtime-venv\n"
        assert dirty.read_text(encoding="utf-8") == "dirty-uncommitted\n"
        assert "secret" not in result.stdout + result.stderr

    def test_idempotent_with_matching_content_refuses_tampered_release(
        self, fixture_paths: dict[str, Path | str]
    ) -> None:
        repo = Path(fixture_paths["repo"])
        sha = str(fixture_paths["sha"])
        release_root = Path(fixture_paths["release_root"])
        runtime_data = Path(fixture_paths["runtime_data"])
        runtime_venv = Path(fixture_paths["runtime_venv"])

        first = _run_install(
            sha=sha,
            repo=repo,
            release_root=release_root,
            runtime_data=runtime_data,
            runtime_venv=runtime_venv,
        )
        assert first.returncode == 0, first.stderr
        release = release_root / sha
        probe = release / "ops" / "quant-monitor" / "scripts" / "tracked_payload.txt"
        second = _run_install(
            sha=sha,
            repo=repo,
            release_root=release_root,
            runtime_data=runtime_data,
            runtime_venv=runtime_venv,
        )
        assert second.returncode == 0, second.stderr
        assert "release_reused=" in second.stdout

        probe.write_text("local-edit-after-install\n", encoding="utf-8")

        tampered = _run_install(
            sha=sha,
            repo=repo,
            release_root=release_root,
            runtime_data=runtime_data,
            runtime_venv=runtime_venv,
        )
        assert tampered.returncode != 0
        assert "refusing overwrite" in tampered.stderr
        assert probe.read_text(encoding="utf-8") == "local-edit-after-install\n"

    def test_refuses_preexisting_target_without_installing_over_it(
        self, fixture_paths: dict[str, Path | str]
    ) -> None:
        repo = Path(fixture_paths["repo"])
        sha = str(fixture_paths["sha"])
        release_root = Path(fixture_paths["release_root"])
        runtime_data = Path(fixture_paths["runtime_data"])
        runtime_venv = Path(fixture_paths["runtime_venv"])
        release = release_root / sha
        release.mkdir()
        sentinel = release / "preexisting.txt"
        sentinel.write_text("keep-me\n", encoding="utf-8")

        result = _run_install(
            sha=sha,
            repo=repo,
            release_root=release_root,
            runtime_data=runtime_data,
            runtime_venv=runtime_venv,
        )
        assert result.returncode != 0
        assert "refusing overwrite" in result.stderr
        assert sentinel.read_text(encoding="utf-8") == "keep-me\n"
        assert not (release / "ops").exists()

    def test_rejects_missing_runtime_paths(self, fixture_paths: dict[str, Path | str]) -> None:
        repo = Path(fixture_paths["repo"])
        sha = str(fixture_paths["sha"])
        release_root = Path(fixture_paths["release_root"])
        runtime_venv = Path(fixture_paths["runtime_venv"])
        missing_data = Path(fixture_paths["release_root"]).parent / "missing-data"

        result = _run_install(
            sha=sha,
            repo=repo,
            release_root=release_root,
            runtime_data=missing_data,
            runtime_venv=runtime_venv,
        )
        assert result.returncode != 0
        assert "runtime data" in result.stderr
        assert list(release_root.iterdir()) == []
