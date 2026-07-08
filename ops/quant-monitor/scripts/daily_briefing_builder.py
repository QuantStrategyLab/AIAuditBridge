#!/usr/bin/env python3
"""Build per-domain daily briefing JSON for AIAuditBridge consume (task 10)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DOMAINS = ("cn_equity", "hk_equity", "us_equity", "crypto")


def _status_counts(strategies: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"healthy": 0, "watch": 0, "review": 0, "critical": 0}
    for row in strategies:
        status = str(row.get("status") or "healthy").lower()
        if status in counts:
            counts[status] += 1
    return counts


def main() -> int:
    root = Path(os.environ.get("QUANT_MONITOR_ROOT") or Path(__file__).resolve().parents[1])
    day = os.environ.get("DAY") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = root / "data" / "daily-reports" / day
    out_dir.mkdir(parents=True, exist_ok=True)

    from quant_platform_kit.strategy_lifecycle.drift_detector import run_drift_detection
    from quant_platform_kit.strategy_lifecycle.health_dashboard import build_dashboard

    drift_by_key: dict[tuple[str, str], float] = {}
    for domain in DOMAINS:
        for drift in run_drift_detection(domain):
            drift_by_key[(domain, drift.strategy_profile)] = float(drift.drift_score or 0.0)

    with tempfile.TemporaryDirectory() as tmp:
        build_dashboard(output_dir=tmp, output_format="json")
        dash_path = Path(tmp) / "strategy_health_dashboard.json"
        strategies_raw: list[dict[str, Any]] = []
        if dash_path.is_file():
            payload = json.loads(dash_path.read_text(encoding="utf-8"))
            if isinstance(payload.get("strategies"), list):
                strategies_raw = [row for row in payload["strategies"] if isinstance(row, dict)]

    by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in strategies_raw:
        domain = str(row.get("domain") or "")
        profile = str(row.get("strategy_profile") or "")
        enriched = dict(row)
        enriched["drift_score"] = drift_by_key.get((domain, profile), 0.0)
        by_domain[domain].append(enriched)

    for domain in DOMAINS:
        strategies = by_domain.get(domain, [])
        summary = _status_counts(strategies)
        report = {
            "domain": domain,
            "ok": True,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "strategies": strategies,
            "summary": summary,
        }
        path = out_dir / f"{domain}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[briefing] wrote {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
