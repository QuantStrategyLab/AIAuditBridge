from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from scripts.consume_daily_briefing import main


def _write_critical_report(report_dir: Path) -> None:
    (report_dir / "us_equity.json").write_text(
        json.dumps(
            {
                "domain": "us_equity",
                "ok": True,
                "strategies": [
                    {
                        "strategy_profile": "global_etf_rotation",
                        "status": "critical",
                        "overall_score": 14.2,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_successful_optimization_record_exits_zero() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        _write_critical_report(report_dir)
        dispatch_summary = {
            "action": "github_issue",
            "optimization_watch": {"status": "ok", "errors": 0},
            "errors": [],
        }

        with patch(
            "scripts.consume_daily_briefing.dispatch_briefing_result",
            return_value=dispatch_summary,
        ):
            assert main(["--report-dir", str(report_dir), "--dispatch"]) == 0


def test_optimization_record_failure_exits_nonzero() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        _write_critical_report(report_dir)
        dispatch_summary = {
            "action": "github_issue",
            "optimization_watch": {"status": "partial_error", "errors": 1},
            "errors": ["optimization_record_failed"],
        }

        with patch(
            "scripts.consume_daily_briefing.dispatch_briefing_result",
            return_value=dispatch_summary,
        ):
            assert main(["--report-dir", str(report_dir), "--dispatch"]) == 2


def test_undispatched_optimization_finding_exits_nonzero() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        _write_critical_report(report_dir)

        assert main(["--report-dir", str(report_dir)]) == 2
