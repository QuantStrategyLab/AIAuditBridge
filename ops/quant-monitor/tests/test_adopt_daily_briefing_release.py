"""Synthetic-only checks for the daily-only immutable release adopter."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADOPTER = ROOT / "scripts" / "adopt_daily_briefing_release.sh"
INSTALLER = ROOT / "scripts" / "install_immutable_release.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _command(root: str, script: str) -> str:
    return (
        "{ path=/bin/bash ; argv[]=/bin/bash "
        f"{root}/ops/quant-monitor/scripts/{script}"
        " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


def _env_command(release_root: Path, sha: str) -> str:
    root = release_root / sha
    monitor = root / "ops/quant-monitor"
    return (
        "{ path=/usr/bin/env ; argv[]=/usr/bin/env "
        f"QUANT_MONITOR_ROOT={monitor} AIAUDIT_BRIDGE_ROOT={root} "
        f"/bin/bash {monitor}/scripts/daily_briefing_pipeline.sh"
        " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


@pytest.fixture
def adoption_case(tmp_path: Path) -> dict[str, Path | str]:
    if os.geteuid() == 0:
        pytest.skip("synthetic test mode is intentionally unavailable as root")

    repo = tmp_path / "repo"
    monitor = repo / "ops/quant-monitor"
    scripts = monitor / "scripts"
    scripts.mkdir(parents=True)
    (monitor / "AGENTS.md").write_text("synthetic\n", encoding="utf-8")
    (monitor / ".gitignore").write_text("data/\n.venv/\n", encoding="utf-8")
    (scripts / "common_env.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "health_check.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "daily_briefing_pipeline.sh").write_text(
        "#!/usr/bin/env bash\n", encoding="utf-8"
    )
    (scripts / "daily_briefing_builder.py").write_text("# offline qualification target\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts/consume_daily_briefing.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops/quant-monitor'))\n"
        "from quant_monitor_domain import runtime_digest, briefing_dispatch\n",
        encoding="utf-8",
    )
    domain = monitor / "quant_monitor_domain"
    domain.mkdir()
    (domain / "__init__.py").write_text("\n", encoding="utf-8")
    for module in ("runtime_digest", "briefing_dispatch"):
        (domain / f"{module}.py").write_text("\n", encoding="utf-8")
    shutil.copy2(ADOPTER, scripts / ADOPTER.name)
    shutil.copy2(INSTALLER, scripts / INSTALLER.name)
    (scripts / INSTALLER.name).chmod(0o755)
    (scripts / ADOPTER.name).chmod(0o755)

    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "synthetic daily adopter")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)

    old_sha = "a" * 40
    release_root = tmp_path / "releases"
    old_monitor = release_root / old_sha / "ops/quant-monitor"
    old_monitor.mkdir(parents=True)
    runtime_data = tmp_path / "runtime/data"
    runtime_venv = tmp_path / "runtime/.venv"
    runtime_data.mkdir(parents=True)
    venv.EnvBuilder(with_pip=False).create(runtime_venv)
    site_packages = runtime_venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    qpk = site_packages / "quant_platform_kit/strategy_lifecycle"
    qpk.mkdir(parents=True)
    (site_packages / "quant_platform_kit/__init__.py").write_text("\n", encoding="utf-8")
    (qpk.parent / "__init__.py").write_text("\n", encoding="utf-8")
    (qpk / "__init__.py").write_text("\n", encoding="utf-8")
    (qpk / "drift_detector.py").write_text(
        "def run_drift_detection(domain): return []\n", encoding="utf-8"
    )
    (qpk / "health_dashboard.py").write_text(
        "def build_dashboard(*, output_dir, output_format, domains=()): return None\n",
        encoding="utf-8",
    )
    # Deliberately models the installed older QPK API: qualification checks
    # required imports/basic signatures, not the newer crypto-window kwargs.
    (qpk / "performance_monitor.py").write_text(
        "def run_monitor(domain, *, strategy_profile=None, live_stream_id=None, source_revision=None): return []\n",
        encoding="utf-8",
    )
    (qpk / "strategy_health_score.py").write_text(
        "def compute_health_score(snapshot, *, drift=None): return None\n", encoding="utf-8"
    )
    (old_monitor / "data").symlink_to(runtime_data)
    (old_monitor / ".venv").symlink_to(runtime_venv)

    unit_root = tmp_path / "systemd"
    unit_root.mkdir()
    base_unit = unit_root / "codex-daily-briefing.service"
    base_unit.write_text("[Service]\nExecStartPre=/bin/true\n", encoding="utf-8")
    (unit_root / "codex-daily-briefing.timer").write_text("[Timer]\n", encoding="utf-8")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$TEST_SYSTEMCTL_LOG"
if [[ "${1:-}" == daemon-reload ]]; then touch "$TEST_RELOADED"; exit 0; fi
[[ "${1:-}" == --no-pager && "${2:-}" == show ]] || exit 9
unit="$3"
case "$unit" in
  codex-daily-briefing.service)
    root="$TEST_RELEASE_ROOT/$TEST_OLD_SHA"
    if [[ -e "$TEST_RELOADED" ]]; then
      if [[ "${TEST_FAIL_READBACK:-false}" == true ]]; then root="$TEST_RELEASE_ROOT/$TEST_OLD_SHA"; fi
      wd="$TEST_RELEASE_ROOT/$TEST_NEW_SHA/ops/quant-monitor"
      if [[ ! -e "$TEST_RELOADED" || "${TEST_FAIL_READBACK:-false}" == true ]]; then wd="$root/ops/quant-monitor"; fi
      if [[ "${TEST_FAIL_READBACK:-false}" == true ]]; then execstart="$TEST_OLD_EXECSTART"; else
        execstart="{ path=/usr/bin/env ; argv[]=/usr/bin/env QUANT_MONITOR_ROOT=$wd AIAUDIT_BRIDGE_ROOT=$TEST_RELEASE_ROOT/$TEST_NEW_SHA /bin/bash $wd/scripts/daily_briefing_pipeline.sh ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
      fi
      dropin=""
    else
      wd="$root/ops/quant-monitor"
      execstart="$TEST_OLD_EXECSTART"
      dropin=""
    fi
    dropin_dir="$TEST_UNIT_ROOT/codex-daily-briefing.service.d"
    for candidate in "$dropin_dir"/*.conf; do
      [[ -e "$candidate" ]] || continue
      dropin="${dropin}${dropin:+ }$candidate"
    done
    printf 'WorkingDirectory=%s\\nExecStart=%s\\nExecStartPre=%s\\nFragmentPath=%s\\nDropInPaths=%s\\nActiveState=inactive\\nSubState=dead\\nUnitFileState=static\\n' \\
      "$wd" "$execstart" "$TEST_EXECSTARTPRE" "$TEST_UNIT_ROOT/codex-daily-briefing.service" "$dropin"
    ;;
  codex-daily-briefing.timer)
    printf 'FragmentPath=%s\\nDropInPaths=\\nUnit=codex-daily-briefing.service\\nActiveState=active\\nSubState=waiting\\nUnitFileState=enabled\\n' "$TEST_UNIT_ROOT/codex-daily-briefing.timer"
    ;;
  codex-quant.service)
    printf 'WorkingDirectory=/health\\nExecStart={ path=/bin/bash ; argv[]=/bin/bash /health/health_check.sh ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }\\nExecStartPre=/bin/true\\nFragmentPath=/etc/systemd/system/codex-quant.service\\nDropInPaths=\\nActiveState=failed\\nSubState=failed\\nUnitFileState=static\\n'
    ;;
  *) exit 9 ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)

    old_root = str(release_root / old_sha)
    old_exec = _command(old_root, "daily_briefing_pipeline.sh")
    return {
        "repo": repo,
        "sha": sha,
        "old_sha": old_sha,
        "release_root": release_root,
        "runtime_data": runtime_data,
        "runtime_venv": runtime_venv,
        "site_packages": site_packages,
        "path_prefix": bin_dir,
        "unit_root": unit_root,
        "systemctl": systemctl,
        "log": tmp_path / "systemctl.log",
        "reloaded": tmp_path / "reloaded",
        "old_exec": old_exec,
        "execstartpre": "/bin/bash /old/scripts/load_telegram_env.sh",
    }


def _run(case: dict[str, Path | str], *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("QUANT_MONITOR_V2_RUNTIME_READY", None)
    env.update(
        {
            "AAB_DAILY_ADOPTION_TEST_MODE": "true",
            "AAB_DAILY_ADOPTION_TEST_UNIT_ROOT": str(case["unit_root"]),
            "AAB_DAILY_ADOPTION_TEST_SYSTEMCTL": str(case["systemctl"]),
            "TEST_SYSTEMCTL_LOG": str(case["log"]),
            "TEST_RELOADED": str(case["reloaded"]),
            "TEST_RELEASE_ROOT": str(case["release_root"]),
            "TEST_OLD_SHA": str(case["old_sha"]),
            "TEST_NEW_SHA": str(case["sha"]),
            "TEST_OLD_EXECSTART": str(case["old_exec"]),
            "TEST_EXECSTARTPRE": str(case["execstartpre"]),
            "TEST_UNIT_ROOT": str(case["unit_root"]),
            "PATH": str(case["path_prefix"]) + os.pathsep + os.environ.get("PATH", ""),
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            "bash",
            str(ADOPTER),
            "--sha",
            str(case["sha"]),
            "--expected-current-sha",
            str(case["old_sha"]),
            "--repo",
            str(case["repo"]),
            "--release-root",
            str(case["release_root"]),
            "--runtime-data",
            str(case["runtime_data"]),
            "--runtime-venv",
            str(case["runtime_venv"]),
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def test_adopts_only_daily_unit_and_preserves_data_venv_and_prestart(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    dropin_dir = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d"
    dropin_dir.mkdir()
    preserved = dropin_dir / "10-site-policy.conf"
    preserved.write_text("[Service]\nEnvironment=EXAMPLE=unchanged\n", encoding="utf-8")
    result = _run(case)
    assert result.returncode == 0, result.stderr
    assert "status=prepared" in result.stdout

    new_monitor = Path(str(case["release_root"])) / str(case["sha"]) / "ops/quant-monitor"
    assert new_monitor.is_dir()
    assert not list(new_monitor.parent.parent.rglob("__pycache__"))
    assert new_monitor.joinpath("data").resolve() == Path(str(case["runtime_data"]))
    assert new_monitor.joinpath(".venv").resolve() == Path(str(case["runtime_venv"]))
    dropin = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d/zzzzzzzzzz-aab-daily-release.conf"
    content = dropin.read_text(encoding="utf-8")
    assert f"WorkingDirectory={new_monitor}" in content
    assert f"QUANT_MONITOR_ROOT={new_monitor}" in content
    assert f"AIAUDIT_BRIDGE_ROOT={Path(str(case['release_root'])) / str(case['sha'])}" in content
    assert "ExecStartPre=" not in content
    assert preserved.read_text(encoding="utf-8") == "[Service]\nEnvironment=EXAMPLE=unchanged\n"

    invocations = Path(str(case["log"])).read_text(encoding="utf-8").splitlines()
    assert invocations.count("daemon-reload") == 1
    assert all(" start " not in f" {line} " and " restart " not in f" {line} " for line in invocations)
    assert not any("codex-quant.timer" in line or "codex-quant.service" in line and "show" not in line for line in invocations)
    assert any("show codex-daily-briefing.timer" in line for line in invocations)


def test_foreign_file_colliding_with_owned_dropin_is_rejected(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    dropin_dir = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d"
    dropin_dir.mkdir()
    dropin = dropin_dir / "zzzzzzzzzz-aab-daily-release.conf"
    dropin.write_text("[Service]\nEnvironment=FOREIGN=true\n", encoding="utf-8")
    result = _run(case)
    assert result.returncode == 2
    assert "daily_dropin_unowned_or_unexpected" in result.stderr
    assert not (Path(str(case["release_root"])) / str(case["sha"])).exists()


def test_daily_runtime_qualification_failure_does_not_switch_unit(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    (Path(str(case["site_packages"])) / "quant_platform_kit/strategy_lifecycle/health_dashboard.py").unlink()
    result = _run(case)
    assert result.returncode == 2
    assert "daily_runtime_qualification_failed" in result.stderr
    assert Path(str(case["reloaded"])).exists() is False
    invocations = Path(str(case["log"])).read_text(encoding="utf-8").splitlines()
    assert not any("daemon-reload" in line for line in invocations)
    assert not (Path(str(case["unit_root"])) / "codex-daily-briefing.service.d/zzzzzzzzzz-aab-daily-release.conf").exists()


def test_exact_prior_helper_dropin_is_snapshotted_and_replaced(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    dropin_dir = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d"
    dropin_dir.mkdir()
    dropin = dropin_dir / "zzzzzzzzzz-aab-daily-release.conf"
    old_monitor = Path(str(case["release_root"])) / str(case["old_sha"]) / "ops/quant-monitor"
    previous_sha = "b" * 40
    prior_contents = (
        "# managed-by: AIAuditBridge daily-release-adoption.v1\n"
        f"# release-sha: {case['old_sha']}\n"
        f"# prior-release-sha: {previous_sha}\n"
        "[Service]\n"
        f"WorkingDirectory={old_monitor}\n"
        "ExecStart=\n"
        f"ExecStart=/usr/bin/env QUANT_MONITOR_ROOT={old_monitor} AIAUDIT_BRIDGE_ROOT={Path(str(case['release_root'])) / str(case['old_sha'])} /bin/bash {old_monitor}/scripts/daily_briefing_pipeline.sh\n"
    )
    dropin.write_text(prior_contents, encoding="utf-8")
    result = _run(case)
    assert result.returncode == 0, result.stderr
    snapshot = dropin_dir / f"zzzzzzzzzz-aab-daily-release.rollback.{case['old_sha']}.{case['sha']}"
    assert snapshot.read_text(encoding="utf-8") == prior_contents
    assert f"# release-sha: {case['sha']}" in dropin.read_text(encoding="utf-8")


def test_two_consecutive_adoptions_accept_exact_helper_execstart(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    first = _run(case)
    assert first.returncode == 0, first.stderr
    first_sha = str(case["sha"])

    repo = Path(str(case["repo"]))
    (repo / "ops/quant-monitor/README.md").write_text("second synthetic release\n", encoding="utf-8")
    _git(repo, "add", "ops/quant-monitor/README.md")
    _git(repo, "commit", "-m", "synthetic second release")
    second_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", second_sha)
    Path(str(case["reloaded"])).unlink()
    case["old_sha"] = first_sha
    case["sha"] = second_sha
    case["old_exec"] = _env_command(Path(str(case["release_root"])), first_sha)

    second = _run(case)
    assert second.returncode == 0, second.stderr
    dropin_dir = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d"
    dropin = dropin_dir / "zzzzzzzzzz-aab-daily-release.conf"
    snapshot = dropin_dir / f"zzzzzzzzzz-aab-daily-release.rollback.{first_sha}.{second_sha}"
    assert f"# release-sha: {first_sha}\n" in snapshot.read_text(encoding="utf-8")
    assert f"# release-sha: {second_sha}" in dropin.read_text(encoding="utf-8")


def test_failed_readback_keeps_recovery_snapshot_and_does_not_retry(adoption_case: dict[str, Path | str]) -> None:
    case = adoption_case
    result = _run(case, extra_env={"TEST_FAIL_READBACK": "true"})
    assert result.returncode == 3
    assert "status=unknown" in result.stderr
    assert "manual_readback_no_retry" in result.stderr
    dropin_dir = Path(str(case["unit_root"])) / "codex-daily-briefing.service.d"
    snapshots = list(dropin_dir.glob("*.rollback.*"))
    assert len(snapshots) == 1
    assert snapshots[0].read_text(encoding="utf-8") == "previous_dropin=absent\n"
    invocations = Path(str(case["log"])).read_text(encoding="utf-8").splitlines()
    assert invocations.count("daemon-reload") == 1
