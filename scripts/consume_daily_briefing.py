#!/usr/bin/env python3
"""CLI entrypoint for quant-monitor daily briefing consumption."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from service.briefing_consumer import consume_briefing_dir
from service.briefing_dispatch import dispatch_briefing_result


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
    args = parser.parse_args(argv)

    report_dir = Path(args.report_dir)
    if not report_dir.is_dir():
        print(json.dumps({"ok": False, "error": f"report_dir_not_found: {report_dir}"}))
        return 1

    result = consume_briefing_dir(report_dir, day=args.day)
    payload: dict = result.to_dict()
    if args.dispatch:
        payload["dispatch"] = dispatch_briefing_result(result, dry_run=args.dry_run)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.action.value == "quiet" else 2


if __name__ == "__main__":
    raise SystemExit(main())
