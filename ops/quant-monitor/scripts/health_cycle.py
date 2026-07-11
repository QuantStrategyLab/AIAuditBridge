#!/usr/bin/env python3
"""VPS health cycle — roadmap task 7 (scores, drift, issues, Telegram)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DOMAINS = ("cn_equity", "hk_equity", "us_equity", "crypto")
SCORE_ALERT = 60.0
DRIFT_REVIEW = 0.50
DRIFT_CRITICAL = 0.75


def _send_telegram(text: str) -> bool:
    token = (os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TG_TOKEN") or "").strip()
    chat = (os.environ.get("GLOBAL_TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return False
    try:
        from quant_platform_kit.notifications.telegram import send_telegram_message

        send_telegram_message(bot_token=token, chat_ids=chat, text=text)
        return True
    except Exception:
        return False


def _create_owner_issue(*, title: str, body: str) -> str | None:
    repo = (os.environ.get("QSL_GITHUB_REPO") or "QuantStrategyLab/CnEquityStrategies").strip()
    owner = (os.environ.get("QSL_MONITOR_ISSUE_OWNER") or "Pigbibi").strip()
    full_body = f"{body}\n\ncc @{owner}"
    try:
        out = subprocess.check_output(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body",
                full_body,
                "--label",
                "monitoring",
                "--label",
                "drift-critical",
            ],
            text=True,
            stderr=subprocess.STDOUT,
            env={**os.environ, "GH_PROMPT": "disabled"},
        )
        return out.strip()
    except Exception:
        return None


def main() -> int:
    root = Path(os.environ.get("QUANT_MONITOR_ROOT") or Path(__file__).resolve().parents[1])
    out_dir = root / "data" / "health"
    dash_dir = out_dir / "dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    from quant_platform_kit.strategy_lifecycle.codex_integration import create_issues_for_domain
    from quant_platform_kit.strategy_lifecycle.drift_detector import run_drift_detection
    from quant_platform_kit.strategy_lifecycle.health_dashboard import build_dashboard

    build_dashboard(output_dir=str(dash_dir), output_format="json")

    strategies: list[dict[str, Any]] = []
    json_path = dash_dir / "strategy_health_dashboard.json"
    collector_payload_invalid = False
    from build_dashboard_snapshot import build_payload

    normalized_path = out_dir / "strategy_health_dashboard.v1.json"
    normalized_payload = build_payload(
        health_file=json_path,
        review_dir=root / "data" / "strategy-reviews",
    )
    normalized_path.write_text(
        json.dumps(normalized_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    collector_payload_invalid = (
        normalized_payload.get("data_status") != "ready"
        or bool(normalized_payload.get("errors"))
    )
    if json_path.is_file():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
            collector_payload_invalid = True
        if isinstance(payload.get("strategies"), list):
            strategies = [row for row in payload["strategies"] if isinstance(row, dict)]
    else:
        collector_payload_invalid = True

    telegram_lines: list[str] = []
    critical_lines: list[str] = []

    for row in strategies:
        try:
            score = float(row.get("overall_score"))
        except (TypeError, ValueError):
            continue
        if score >= SCORE_ALERT:
            continue
        profile = str(row.get("strategy_profile") or "?")
        domain = str(row.get("domain") or "?")
        telegram_lines.append(f"[{domain}] {profile}: health_score={score:.1f}")

    issue_results: list[dict[str, Any]] = []
    for domain in DOMAINS:
        drifts = run_drift_detection(domain)
        for drift in drifts:
            score = float(drift.drift_score or 0.0)
            label = f"[{domain}] {drift.strategy_profile}: drift_score={score:.2f}"
            if score >= DRIFT_CRITICAL:
                critical_lines.append(label)
            elif score >= DRIFT_REVIEW:
                pass  # tracked via create_issues_for_domain below

        issue_results.extend(create_issues_for_domain(domain))

    for line in critical_lines:
        _create_owner_issue(
            title=f"[monitor] critical drift — {line}",
            body=f"Quant-monitor detected critical drift.\n\n- {line}",
        )

    notify_lines = telegram_lines + critical_lines
    if notify_lines:
        body = "🚨 quant-monitor health_cycle\n" + "\n".join(f"• {line}" for line in notify_lines)
        _send_telegram(body)

    summary = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "domains": list(DOMAINS),
        "strategy_count": len(strategies),
        "telegram_alerts": notify_lines,
        "issues_created": len([r for r in issue_results if r.get("issue_url")]),
        "ok": not notify_lines,
        "collector_payload_valid": not collector_payload_invalid,
        "snapshot_data_status": normalized_payload.get("data_status"),
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"cycle_{ts}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
