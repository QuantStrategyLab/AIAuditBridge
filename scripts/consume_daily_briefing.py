#!/usr/bin/env python3
"""CLI entrypoint for quant-monitor daily briefing consumption."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from service.briefing_consumer import consume_briefing_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consume quant-monitor daily briefing reports.")
    parser.add_argument(
        "--report-dir",
        required=True,
        help="Directory containing domain JSON files (e.g. data/daily-reports/2026-07-08)",
    )
    parser.add_argument("--day", default="", help="Report day label (defaults to directory name)")
    args = parser.parse_args(argv)

    report_dir = Path(args.report_dir)
    if not report_dir.is_dir():
        print(json.dumps({"ok": False, "error": f"report_dir_not_found: {report_dir}"}))
        return 1

    result = consume_briefing_dir(report_dir, day=args.day)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.action.value == "quiet" else 2


if __name__ == "__main__":
    raise SystemExit(main())
