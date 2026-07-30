#!/usr/bin/env python3
"""VPS health cycle — roadmap task 7 (scores, drift, issues, Telegram)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DOMAINS = ("cn_equity", "hk_equity", "us_equity", "crypto")
SCORE_ALERT = 60.0
DRIFT_REVIEW = 0.50
DRIFT_CRITICAL = 0.75
_ALERT_STATE_RELATIVE_PATH = Path("data/alert-state/health_cycle.json")
_ARTIFACT_STATUS_RELATIVE_PATH = Path("data/lifecycle-artifacts/status.json")
_ARTIFACT_STATUS_SCHEMA = "quant_monitor_lifecycle_artifact_status.v1"
_SAFE_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")


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


def _refresh_and_collect_drift(run_monitor, run_drift_detection, *, domains=DOMAINS):
    snapshots: dict[str, list[Any]] = {}
    results: dict[str, list[Any]] = {}
    errors: list[dict[str, str]] = []
    for domain in domains:
        try:
            domain_snapshots = list(run_monitor(domain))
            if not domain_snapshots:
                raise RuntimeError("monitor produced no snapshots")
            snapshots[domain] = domain_snapshots
        except Exception as exc:
            errors.append(
                {
                    "domain": domain,
                    "code": "monitor_data_unavailable",
                    "error_type": type(exc).__name__,
                }
            )
            continue
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
    return snapshots, results, errors


def _artifact_status_error(
    domain: str,
    *,
    code: str = "artifact_sync_status_unavailable",
    error_type: str = "RuntimeError",
) -> dict[str, str]:
    safe_code = code if _SAFE_TOKEN.fullmatch(code) else "artifact_sync_status_unavailable"
    safe_error_type = error_type if _SAFE_TOKEN.fullmatch(error_type) else "RuntimeError"
    return {"domain": domain, "code": safe_code, "error_type": safe_error_type}


def _load_lifecycle_artifact_status(
    root: Path,
    *,
    domains=DOMAINS,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=2),
) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    path = root / _ARTIFACT_STATUS_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        as_of = datetime.fromisoformat(str(payload["as_of"]).replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            raise ValueError("status timestamp has no timezone")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        age = current - as_of.astimezone(timezone.utc)
        domain_statuses = payload["domains"]
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _ARTIFACT_STATUS_SCHEMA
            or not isinstance(domain_statuses, dict)
            or age > max_age
            or age < -timedelta(minutes=5)
        ):
            raise ValueError("artifact status is invalid or stale")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return (), [
            _artifact_status_error(domain, error_type=type(exc).__name__)
            for domain in domains
        ]

    ready: list[str] = []
    errors: list[dict[str, str]] = []
    for domain in domains:
        status = domain_statuses.get(domain)
        if not isinstance(status, dict):
            errors.append(_artifact_status_error(domain))
            continue
        profiles = status.get("profiles")
        valid_ready = (
            status.get("status") == "ready"
            and isinstance(status.get("artifact_id"), int)
            and status["artifact_id"] > 0
            and isinstance(status.get("run_id"), int)
            and status["run_id"] > 0
            and re.fullmatch(r"[0-9a-f]{40}", str(status.get("head_sha") or ""))
            and isinstance(profiles, list)
            and bool(profiles)
            and all(isinstance(profile, str) and profile for profile in profiles)
        )
        if valid_ready:
            ready.append(domain)
            continue
        errors.append(
            _artifact_status_error(
                domain,
                code=str(status.get("code") or "artifact_sync_status_unavailable"),
                error_type=str(status.get("error_type") or "RuntimeError"),
            )
        )
    return tuple(ready), errors


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
    from quant_platform_kit.strategy_lifecycle.performance_monitor import run_monitor

    ready_domains, artifact_errors = _load_lifecycle_artifact_status(root)
    snapshot_results, drift_results, lifecycle_errors = _refresh_and_collect_drift(
        run_monitor,
        run_drift_detection,
        domains=ready_domains,
    )
    data_errors = artifact_errors + lifecycle_errors
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
    for error in data_errors:
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
        "data_errors": data_errors,
        "snapshot_count": sum(len(rows) for rows in snapshot_results.values()),
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
