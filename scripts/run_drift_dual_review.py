#!/usr/bin/env python3
"""Run dual-review pipeline for critical drift detections (task 11b)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _critical_drifts(domain: str) -> list[dict[str, Any]]:
    from quant_platform_kit.strategy_lifecycle.drift_detector import run_drift_detection

    results = run_drift_detection(domain)
    payloads: list[dict[str, Any]] = []
    for item in results:
        status = getattr(getattr(item, "status", None), "value", "")
        if status != "critical":
            continue
        payloads.append(
            {
                "trigger": "drift",
                "strategy_profile": item.strategy_profile,
                "domain": item.domain,
                "drift_score": item.drift_score,
                "context": {
                    "domain": item.domain,
                    "drift_score": item.drift_score,
                    "repository": os.environ.get("GITHUB_REPOSITORY", ""),
                },
            }
        )
    return payloads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dual-review for critical drift strategies.")
    parser.add_argument("--domain", default=os.environ.get("STRATEGY_DOMAIN", "").strip())
    parser.add_argument("--dispatch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--aab-root",
        default=os.environ.get("AIAUDIT_BRIDGE_ROOT", "external/AIAuditBridge"),
    )
    args = parser.parse_args(argv)

    domain = str(args.domain or "").strip()
    if not domain:
        print(json.dumps({"ok": False, "error": "domain_required"}))
        return 1

    if str(os.environ.get("DUAL_REVIEW_GATE_SKIP", "")).strip().lower() in {"1", "true", "yes"}:
        print(json.dumps({"ok": True, "skipped": ["dual_review_gate_disabled"], "count": 0}))
        return 0

    critical = _critical_drifts(domain)
    if not critical:
        print(json.dumps({"ok": True, "count": 0, "results": []}))
        return 0

    aab_root = Path(args.aab_root).resolve()
    pipeline = aab_root / "scripts" / "run_dual_review_pipeline.py"
    if not pipeline.is_file():
        print(json.dumps({"ok": False, "error": f"pipeline_not_found: {pipeline}"}))
        return 1

    results: list[dict[str, Any]] = []
    worst = 0
    for item in critical:
        cmd = [
            sys.executable,
            str(pipeline),
            "--trigger",
            "drift",
            "--strategy-profile",
            item["strategy_profile"],
            "--context-json",
            json.dumps(item["context"]),
        ]
        if args.dispatch:
            cmd.append("--dispatch")
        if args.dry_run:
            cmd.append("--dry-run")
        proc = subprocess.run(
            cmd,
            cwd=str(aab_root),
            env={**os.environ, "PYTHONPATH": str(aab_root)},
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            body = json.loads(proc.stdout) if proc.stdout.strip() else {"ok": False, "error": proc.stderr}
        except json.JSONDecodeError:
            body = {"ok": False, "error": proc.stdout or proc.stderr}
        body["exit_code"] = proc.returncode
        results.append(body)
        worst = max(worst, proc.returncode)

    summary = {"ok": True, "domain": domain, "count": len(results), "results": results}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
