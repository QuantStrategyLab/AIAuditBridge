#!/usr/bin/env python3
"""VPS health cycle — roadmap task 7 (scores, drift, issues, Telegram)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DOMAINS = ("cn_equity", "hk_equity", "us_equity", "crypto")
SCORE_ALERT = 60.0
DRIFT_REVIEW = 0.50
DRIFT_CRITICAL = 0.75
_ALERT_STATE_RELATIVE_PATH = Path("data/alert-state/health_cycle.json")


def _collect_drift_results(run_drift_detection, *, domains=DOMAINS):
    results: dict[str, list[Any]] = {}
    errors: list[dict[str, str]] = []
    for domain in domains:
        try:
            results[domain] = list(run_drift_detection(domain))
        except Exception as exc:
            errors.append(
                {
                    "domain": domain,
                    "code": "drift_data_unavailable",
                    "error_type": type(exc).__name__,
                }
            )
    return results, errors


def _alert_fingerprint(lines: list[str]) -> str:
    payload = "\n".join(sorted(str(line) for line in lines))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strategy_health_alert(row: dict[str, Any]) -> tuple[str, str] | None:
    try:
        score = float(row.get("overall_score"))
    except (TypeError, ValueError):
        return None
    if score >= SCORE_ALERT:
        return None
    profile = str(row.get("strategy_profile") or "?")
    domain = str(row.get("domain") or "?")
    return (
        f"[{domain}] {profile}: health_score={score:.1f}",
        f"strategy_health_below_{SCORE_ALERT:g}:{domain}:{profile}",
    )


def _alert_state_path(root: Path) -> Path:
    return root / _ALERT_STATE_RELATIVE_PATH


def _is_duplicate_alert(root: Path, fingerprint: str) -> bool:
    try:
        payload = json.loads(_alert_state_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return str(payload.get("fingerprint") or "") == fingerprint


def _record_alert(root: Path, fingerprint: str) -> None:
    path = _alert_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(
            {
                "schema_version": "quant_monitor_alert_state.v1",
                "fingerprint": fingerprint,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _clear_alert(root: Path) -> None:
    try:
        _alert_state_path(root).unlink()
    except FileNotFoundError:
        pass


def _create_issues_for_available_domains(
    drift_results: dict[str, list[Any]],
    create_issues_for_domain,
    *,
    domains=DOMAINS,
) -> list[dict[str, Any]]:
    issue_results: list[dict[str, Any]] = []
    for domain in domains:
        if domain in drift_results:
            issue_results.extend(create_issues_for_domain(domain))
    return issue_results


def _send_telegram(text: str) -> bool:
    token = (os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TG_TOKEN") or "").strip()
    chat = (os.environ.get("GLOBAL_TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return False
    try:
        from quant_platform_kit.notifications.telegram import send_telegram_message

        return bool(send_telegram_message(bot_token=token, chat_ids=chat, text=text))
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

    drift_results, drift_errors = _collect_drift_results(run_drift_detection)
    build_dashboard(output_dir=str(dash_dir), output_format="json")

    strategies: list[dict[str, Any]] = []
    json_path = dash_dir / "strategy_health_dashboard.json"
    collector_payload_invalid = False
    from build_dashboard_snapshot import build_payload

    normalized_path = out_dir / "strategy_health_dashboard.v1.json"
    review_dir = Path(os.environ.get("QUANT_REVIEW_DIR") or root / "data" / "strategy-reviews")
    normalized_payload = build_payload(
        health_file=json_path,
        review_dir=review_dir,
    )
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=normalized_path.parent,
            prefix=f".{normalized_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            json.dump(normalized_payload, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        Path(temp_name).replace(normalized_path)
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass
    collector_payload_invalid = (
        normalized_payload.get("data_status") != "ready"
        or bool(normalized_payload.get("errors"))
    )
    if not collector_payload_invalid and json_path.is_file():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
            collector_payload_invalid = True
        if isinstance(payload.get("strategies"), list):
            strategies = [row for row in payload["strategies"] if isinstance(row, dict)]
    elif not json_path.is_file():
        collector_payload_invalid = True

    telegram_lines: list[str] = []
    critical_lines: list[str] = []
    alert_identities: list[str] = []

    for row in strategies:
        alert = _strategy_health_alert(row)
        if alert:
            line, identity = alert
            telegram_lines.append(line)
            alert_identities.append(identity)

    for domain in DOMAINS:
        drifts = drift_results.get(domain, [])
        for drift in drifts:
            score = float(drift.drift_score or 0.0)
            label = f"[{domain}] {drift.strategy_profile}: drift_score={score:.2f}"
            if score >= DRIFT_CRITICAL:
                critical_lines.append(label)
                alert_identities.append(
                    f"critical_drift:{domain}:{drift.strategy_profile}"
                )
            elif score >= DRIFT_REVIEW:
                pass  # tracked via create_issues_for_domain below

    issue_results = _create_issues_for_available_domains(
        drift_results,
        create_issues_for_domain,
    )

    for line in critical_lines:
        _create_owner_issue(
            title=f"[monitor] critical drift — {line}",
            body=f"Quant-monitor detected critical drift.\n\n- {line}",
        )

    data_error_lines: list[str] = []
    for error in drift_errors:
        data_error_lines.append(
            f"[{error['domain']}] {error['code']} ({error['error_type']})"
        )
        alert_identities.append(
            f"data_error:{error['domain']}:{error['code']}:{error['error_type']}"
        )
    if collector_payload_invalid:
        data_error_lines.append("[collector] dashboard_data_unavailable")
        alert_identities.append("data_error:collector:dashboard_data_unavailable")
    notify_lines = telegram_lines + critical_lines + data_error_lines
    telegram_sent = False
    duplicate_alert_suppressed = False
    if notify_lines:
        body = "🚨 quant-monitor health_cycle\n" + "\n".join(f"• {line}" for line in notify_lines)
        fingerprint = _alert_fingerprint(alert_identities)
        duplicate_alert_suppressed = _is_duplicate_alert(root, fingerprint)
        if not duplicate_alert_suppressed:
            telegram_sent = _send_telegram(body)
            if telegram_sent:
                _record_alert(root, fingerprint)
    else:
        _clear_alert(root)

    summary = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "domains": list(DOMAINS),
        "strategy_count": len(strategies),
        "telegram_alerts": notify_lines,
        "telegram_sent": telegram_sent,
        "duplicate_alert_suppressed": duplicate_alert_suppressed,
        "data_errors": drift_errors,
        "issues_created": len([r for r in issue_results if r.get("issue_url")]),
        "ok": not notify_lines and not collector_payload_invalid,
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
