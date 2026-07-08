#!/usr/bin/env python3
"""CLI entrypoint for quant-monitor daily briefing consumption."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from service.briefing_consumer import consume_briefing_dir
from service.briefing_dispatch import dispatch_briefing_result
from service.dual_review_briefing import collect_dual_review_payloads, summarize_dual_review_runs
from service.dual_review_dispatch import dispatch_dual_review_result
from service.dual_review_orchestrator import orchestrate_from_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consume quant-monitor daily briefing reports.")
    parser.add_argument(
        "--report-dir",
        required=True,
        help="Directory containing domain JSON files (e.g. data/daily-reports/2026-07-08)",
    )
    parser.add_argument("--day", default="", help="Report day label (defaults to directory name)")
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="Send Telegram / GitHub notifications per briefing severity",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print dispatch actions without sending")
    parser.add_argument(
        "--dual-review",
        action="store_true",
        help="Run dual-review orchestration for briefing strategies with primary_review metadata",
    )
    args = parser.parse_args(argv)

    report_dir = Path(args.report_dir)
    if not report_dir.is_dir():
        print(json.dumps({"ok": False, "error": f"report_dir_not_found: {report_dir}"}))
        return 1

    result = consume_briefing_dir(report_dir, day=args.day)
    payload: dict = result.to_dict()
    if args.dispatch:
        payload["dispatch"] = dispatch_briefing_result(result, dry_run=args.dry_run)
    if args.dual_review:
        dual_results = []
        for item in collect_dual_review_payloads(report_dir):
            outcome = orchestrate_from_payload(item)
            if outcome is None:
                continue
            entry = outcome.to_dict()
            if args.dispatch:
                entry["dispatch"] = dispatch_dual_review_result(outcome, dry_run=args.dry_run)
            dual_results.append(entry)
        payload["dual_review"] = summarize_dual_review_runs(dual_results)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    exit_code = 0 if result.action.value == "quiet" else 2
    if args.dual_review and payload.get("dual_review", {}).get("disagreements"):
        exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
