"""Synthetic checks for the optional LongBridge runtime digest pipeline."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_briefing_pipeline.sh"
ZONE = "Pacific/Kiritimati"


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "monitor"
    aab = tmp_path / "aab"
    (root / "scripts").mkdir(parents=True)
    (aab / "scripts").mkdir(parents=True)
    (root / "scripts" / "source_telegram_env.sh").write_text("# stub\n", encoding="utf-8")
    (root / "scripts" / "daily_briefing.sh").write_text(
        "#!/bin/bash\nexit \"${DOMAIN_BUILD_EXIT:-0}\"\n",
        encoding="utf-8",
    )
    (aab / "scripts" / "consume_daily_briefing.py").write_text(
        "\n".join([
            "import os, sys",
            "with open(os.environ['CONSUME_LOG'], 'a', encoding='utf-8') as handle:",
            "    handle.write('\\t'.join(sys.argv[1:]) + '\\n')",
            "if '--runtime-projection-gcs' in sys.argv:",
            "    raise SystemExit(int(os.environ.get('RUNTIME_CONSUME_EXIT', '0')))",
            "raise SystemExit(int(os.environ.get('DOMAIN_CONSUME_EXIT', '0')))",
            "",
        ]),
        encoding="utf-8",
    )
    return root, aab, tmp_path / "consume.log"


def _run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    root, aab, log = _layout(tmp_path)
    child_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "QUANT_MONITOR_ROOT": str(root),
        "AIAUDIT_BRIDGE_ROOT": str(aab),
        "CONSUME_LOG": str(log),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    child_env.update(env)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=str(root),
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
    )
    result.log = log.read_text(encoding="utf-8") if log.exists() else ""
    return result


def _calls(result: subprocess.CompletedProcess[str]) -> list[list[str]]:
    if not result.log:
        return []
    return [line.split("\t") for line in result.log.splitlines()]


def test_runtime_switch_off_does_not_call_gcs(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="false",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj/runtime_daily",
        QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|paper",
    )
    assert result.returncode == 0
    calls = _calls(result)
    assert len(calls) == 1
    assert "--runtime-projection-gcs" not in calls[0]
    assert "--report-dir" in calls[0]
    assert "gs://" not in result.stdout + result.stderr


def test_enabled_pipeline_uses_timezone_day_and_fixed_paper_object(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj-bucket/runtime_daily",
        QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|paper",
    )
    assert result.returncode == 0, result.stderr
    zoned = datetime.now(ZoneInfo(ZONE)).date().isoformat()
    utc = datetime.now(timezone.utc).date().isoformat()
    calls = _calls(result)
    assert len(calls) == 2
    domain, runtime = calls
    assert "--runtime-projection-gcs" not in domain
    assert domain[domain.index("--day") + 1] == utc
    object_uri = runtime[runtime.index("--runtime-projection-gcs") + 1]
    assert object_uri == f"gs://proj-bucket/runtime_daily/longbridge/paper/{zoned}.json"
    assert runtime[runtime.index("--day") + 1] == zoned
    assert runtime[runtime.index("--expected-target-key") + 1] == "lb-paper|rot|paper"
    assert "--dispatch" in runtime
    assert "gs://" not in result.stdout + result.stderr
    assert "lb-paper|rot|paper" not in result.stdout + result.stderr
    if utc != zoned:
        assert domain[domain.index("--day") + 1] != runtime[runtime.index("--day") + 1]


def test_domain_failure_still_runs_runtime_and_returns_nonzero(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj-bucket/runtime_daily/",
        QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|paper",
        DOMAIN_BUILD_EXIT="4",
    )
    assert result.returncode == 1
    assert len(_calls(result)) == 2
    assert "--runtime-projection-gcs" in _calls(result)[1]
    assert "domain_exit=4 runtime_exit=0" in result.stderr
    assert "gs://" not in result.stderr


def test_runtime_failure_does_not_skip_domain(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj-bucket/team/runtime_daily",
        QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|paper",
        RUNTIME_CONSUME_EXIT="5",
    )
    assert result.returncode == 1
    calls = _calls(result)
    assert "--report-dir" in calls[0]
    assert "--runtime-projection-gcs" in calls[1]
    assert "domain_exit=0 runtime_exit=5" in result.stderr


def test_invalid_runtime_config_skips_gcs_and_stays_nonzero(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj-bucket/other",
        QUANT_MONITOR_RUNTIME_TIMEZONE="Not/AZone",
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|live",
    )
    assert result.returncode == 1
    calls = _calls(result)
    assert len(calls) == 1
    assert "--runtime-projection-gcs" not in calls[0]
    assert "runtime_projection_config_invalid" in result.stderr
    assert "gs://" not in result.stderr


def test_disabled_domain_failure_does_not_start_runtime(tmp_path: Path) -> None:
    result = _run(tmp_path, DOMAIN_BUILD_EXIT="4")
    assert result.returncode == 4
    assert _calls(result) == []
    assert "runtime-projection-gcs" not in result.stdout + result.stderr
