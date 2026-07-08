#!/usr/bin/env python3
"""Run roadmap task-11 dual-review orchestration from JSON payloads."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from service.dual_review_briefing import collect_dual_review_payloads, summarize_dual_review_runs
from service.dual_review_dispatch import dispatch_dual_review_result
from service.dual_review_orchestrator import orchestrate_from_payload


def _load_json_arg(value: str) -> dict[str, Any]:
    path = Path(value)
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
    else:
        loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("expected JSON object")
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run dual-review orchestration for critical decisions.")
    parser.add_argument("--payload", help="Inline JSON payload or path to JSON file")
    parser.add_argument(
        "--report-dir",
        help="Scan quant-monitor briefing directory for dual-review candidates",
    )
    parser.add_argument(
        "--secondary-review",
        help="Optional secondary review JSON (file path or inline). "
        "Use {\"gpt\":{...},\"claude\":{...}} for plan-B injection.",
    )
    parser.add_argument(
        "--secondary-mode",
        choices=("dual_api", "stub"),
        default=os.environ.get("DUAL_REVIEW_SECONDARY_MODE", "dual_api"),
        help="Secondary reviewer backend (default: dual_api = GPT+Claude parallel)",
    )
    parser.add_argument("--dispatch", action="store_true", help="Open GitHub issue on disagreement")
    parser.add_argument("--dry-run", action="store_true", help="Do not create GitHub issues")
    args = parser.parse_args(argv)
    os.environ["DUAL_REVIEW_SECONDARY_MODE"] = args.secondary_mode

    payloads: list[dict[str, Any]] = []
    if args.payload:
        payloads.append(_load_json_arg(args.payload))
    if args.report_dir:
        payloads.extend(collect_dual_review_payloads(Path(args.report_dir)))

    if not payloads:
        print(json.dumps({"ok": False, "error": "no_dual_review_payloads"}))
        return 1

    secondary_review = None
    if args.secondary_review:
        secondary_review = _load_json_arg(args.secondary_review)

    results: list[dict[str, Any]] = []
    for payload in payloads:
        outcome = orchestrate_from_payload(
            payload,
            secondary_review=secondary_review,
        )
        if outcome is None:
            results.append({"ok": False, "error": "invalid_payload", "payload": payload})
            continue
        item = outcome.to_dict()
        if args.dispatch:
            item["dispatch"] = dispatch_dual_review_result(outcome, dry_run=args.dry_run)
        results.append(item)

    summary = summarize_dual_review_runs(results)
    summary["ok"] = True
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    has_disagreement = any(item.get("outcome") == "disagreement" for item in results)
    return 2 if has_disagreement else 0


if __name__ == "__main__":
    raise SystemExit(main())
