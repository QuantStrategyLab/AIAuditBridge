#!/usr/bin/env python3
"""Dry-run CLI for trusted structured dependency-notification intake.

Reads local JSON (file or stdin) only. Does not call GitHub notifications,
models, Telegram, or GitHub issue APIs. Preview uses dispatch_briefing_result
with dry_run=True.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from service.dependency_notification_triage import run_trusted_intake_dry_run


def _load_payload(path: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if path == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None, "input_unreadable"
    if not str(raw or "").strip():
        return None, "input_missing"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "schema_invalid"
    return payload, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run trusted structured dependency notification intake "
            "(local JSON only; no GitHub notifications / model / send)."
        )
    )
    parser.add_argument(
        "--input",
        "-i",
        default="-",
        help="Path to JSON file, or '-' for stdin (default: stdin)",
    )
    args = parser.parse_args(argv)

    payload, parse_error = _load_payload(args.input)
    if parse_error:
        result = run_trusted_intake_dry_run(None, parse_error=parse_error)
    else:
        result = run_trusted_intake_dry_run(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
