#!/usr/bin/env python3
"""VPS health cycle — route strategy evidence to issue-only optimization monitoring."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
_OPERATIONAL_ERROR_CODES = frozenset({
    "artifact_sync_status_unavailable",
    "artifact_sync_unexpected",
    "consumer_path_conflict",
    "drift_data_unavailable",
    "github_api_invalid",
    "github_api_unavailable",
    "monitor_data_unavailable",
    "trusted_artifact_unavailable",
})
_OPERATIONAL_ERROR_TYPES = frozenset({
    "FileNotFoundError",
    "JSONDecodeError",
    "KeyError",
    "LifecycleArtifactError",
    "OSError",
    "RuntimeError",
    "TypeError",
    "ValueError",
})
_OPERATIONAL_DIAGNOSIS_SOURCE_REPOSITORY = "QuantStrategyLab/AIAuditBridge"
_HISTORICAL_DIAGNOSIS_REHEARSAL_TASK = "historical_operational_diagnosis_rehearsal"
_HISTORICAL_DIAGNOSIS_REHEARSAL_CASE = "static_token_write_guard_source_audit_v1"
_NON_OIDC_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "CODEX_API_KEY",
    "CODEX_AUDIT_SERVICE_TOKEN",
    "CURSOR_API_KEY",
    "OPENAI_API_KEY",
)


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


def _build_alert_body(lines: list[str]) -> str:
    return (
        "🚨 quant-monitor operational\n"
        "• scope: data/evidence or optimization-record delivery failure\n"
        + "\n".join(f"• {line}" for line in lines)
    )


def _alert_state_path(root: Path) -> Path:
    return root / _ALERT_STATE_RELATIVE_PATH


def _load_alert_state(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_alert_state_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _write_alert_state(root: Path, payload: dict[str, Any]) -> None:
    path = _alert_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _is_duplicate_alert(root: Path, fingerprint: str) -> bool:
    return str(_load_alert_state(root).get("fingerprint") or "") == fingerprint


def _record_alert(root: Path, fingerprint: str) -> None:
    try:
        payload = _load_operational_diagnosis_state(root)
    except OSError:
        return
    payload.update(
        schema_version="quant_monitor_alert_state.v1",
        fingerprint=fingerprint,
    )
    _write_alert_state(root, payload)


def _clear_alert(root: Path) -> None:
    try:
        payload = _load_operational_diagnosis_state(root)
    except OSError:
        return
    payload.pop("fingerprint", None)
    if payload.get("operational_diagnosis_attempts"):
        payload["schema_version"] = "quant_monitor_alert_state.v1"
        _write_alert_state(root, payload)
        return
    try:
        _alert_state_path(root).unlink()
    except FileNotFoundError:
        pass


def _operational_diagnosis_attempted(root: Path, fingerprint: str) -> bool:
    attempts = _load_operational_diagnosis_state(root).get("operational_diagnosis_attempts")
    return isinstance(attempts, list) and fingerprint in attempts


def _load_operational_diagnosis_state(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_alert_state_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise OSError("operational diagnosis state is unavailable") from exc
    if not isinstance(payload, dict):
        raise OSError("operational diagnosis state is unavailable")
    attempts = payload.get("operational_diagnosis_attempts", [])
    if not isinstance(attempts, list) or any(
        not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
        for item in attempts
    ):
        raise OSError("operational diagnosis state is unavailable")
    return payload


def _record_operational_diagnosis_attempt(root: Path, fingerprint: str) -> None:
    payload = _load_operational_diagnosis_state(root)
    attempts = payload.get("operational_diagnosis_attempts")
    if not isinstance(attempts, list):
        attempts = []
    if fingerprint not in attempts:
        attempts.append(fingerprint)
    payload.update(
        schema_version="quant_monitor_alert_state.v1",
        operational_diagnosis_attempts=attempts,
    )
    _write_alert_state(root, payload)


def _forget_operational_diagnosis_attempt(root: Path, fingerprint: str) -> None:
    payload = _load_operational_diagnosis_state(root)
    attempts = payload.get("operational_diagnosis_attempts")
    if not isinstance(attempts, list):
        return
    payload["operational_diagnosis_attempts"] = [item for item in attempts if item != fingerprint]
    _write_alert_state(root, payload)


def _operational_diagnosis_prompt(data_errors: list[dict[str, str]]) -> str:
    incidents: list[dict[str, str]] = []
    for error in data_errors:
        domain = str(error.get("domain") or "")
        if domain not in DOMAINS:
            continue
        code = str(error.get("code") or "")
        error_type = str(error.get("error_type") or "")
        incidents.append(
            {
                "code": code if code in _OPERATIONAL_ERROR_CODES else "artifact_sync_status_unavailable",
                "domain": domain,
                "error_type": error_type if error_type in _OPERATIONAL_ERROR_TYPES else "RuntimeError",
            }
        )
    incidents.sort(key=lambda item: (item["domain"], item["code"], item["error_type"]))
    payload = json.dumps({"incidents": incidents}, sort_keys=True, separators=(",", ":"))
    return (
        "Diagnose these sanitized quant-monitor data availability incidents. "
        "Use repository evidence read-only. Return likely cause, evidence to inspect, and safe next checks. "
        "Do not place orders, access accounts, change files, create patches, publish, deploy, or notify.\n"
        + payload
    )


def _operational_diagnosis_fingerprint(data_errors: list[dict[str, str]]) -> str:
    identities = [
        f"data_error:{error.get('domain', '')}:{error.get('code', '')}:{error.get('error_type', '')}"
        for error in data_errors
    ]
    return _alert_fingerprint(identities)


def _historical_diagnosis_rehearsal_prompt() -> str:
    evidence = {
        "case_id": _HISTORICAL_DIAGNOSIS_REHEARSAL_CASE,
        "evidence_kind": "source_code_audit_of_pr_180_and_181",
        "facts": [
            "static dashboard tokens are not authorized for write requests",
            "a dashboard caller cannot claim QuantStrategyLab/AIAuditBridge as its source repository",
        ],
        "observed_request": False,
    }
    return (
        "This is a historical engineering diagnosis rehearsal based only on a source-code audit. "
        "No real HTTP 403 request was observed. Explain why the guarded request should be rejected, "
        "identify the approved GitHub OIDC route, and give read-only verification steps that do not "
        "weaken authentication. Answer in Simplified Chinese in about 300 Chinese characters or fewer, "
        "covering the cause, correct OIDC route, and safe verification steps. Do not access accounts, "
        "place orders, change files, create patches, publish, deploy, or notify.\n"
        + json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    )


def run_historical_diagnosis_rehearsal(
    *,
    config_loader=None,
    client_factory=None,
) -> dict[str, Any]:
    """Run the fixed source-audit rehearsal once under the workflow's OIDC authority."""
    if (
        os.environ.get("GITHUB_REPOSITORY") != _OPERATIONAL_DIAGNOSIS_SOURCE_REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
    ):
        return {"status": "rejected", "reason": "invalid_workflow_context"}
    if os.environ.get("GITHUB_RUN_ATTEMPT") != "1":
        return {"status": "rejected", "reason": "invalid_workflow_attempt"}
    if not (
        os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
        and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    ):
        return {"status": "rejected", "reason": "github_oidc_required"}
    if any(os.environ.get(name) for name in _NON_OIDC_CREDENTIAL_ENV):
        return {"status": "rejected", "reason": "non_oidc_credentials_rejected"}
    try:
        if config_loader is None:
            from client.config import GatewayConfig

            config_loader = GatewayConfig.from_env
        if client_factory is None:
            from client.gateway_client import AiGatewayClient

            client_factory = AiGatewayClient
        client = client_factory(config_loader())
    except (ImportError, OSError, RuntimeError, ValueError):
        return {"status": "deferred", "reason": "ai_gateway_not_configured"}
    try:
        result = client.execute(
            _historical_diagnosis_rehearsal_prompt(),
            task=_HISTORICAL_DIAGNOSIS_REHEARSAL_TASK,
            mode="review_only",
            sandbox="read-only",
            research_stage="drift_analysis",
            allowed_providers=["codex"],
            source_repository=_OPERATIONAL_DIAGNOSIS_SOURCE_REPOSITORY,
            source_ref="main",
            timeout=600,
        )
    except Exception:
        return {"status": "unavailable", "reason": "codex_outcome_unknown"}
    raw = result.raw if isinstance(getattr(result, "raw", None), dict) else {}
    if raw.get("status") == "deferred":
        return {"status": "deferred", "reason": "capacity_unavailable"}
    if result.success is True and raw.get("status") == "succeeded" and raw.get("job_id"):
        return {"status": "succeeded", "job_id": str(raw["job_id"])}
    return {"status": "unavailable", "reason": "codex_result_unavailable"}


def _run_operational_diagnosis(
    root: Path,
    data_errors: list[dict[str, str]],
    fingerprint: str,
    *,
    config_loader=None,
    client_factory=None,
) -> dict[str, Any]:
    if not data_errors:
        return {"status": "skipped", "reason": "no_data_errors"}
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return {"status": "rejected", "reason": "invalid_fingerprint"}
    try:
        if _operational_diagnosis_attempted(root, fingerprint):
            return {"status": "skipped", "reason": "already_attempted"}
    except OSError:
        return {"status": "deferred", "reason": "dedupe_state_unavailable"}

    if config_loader is None and not (
        os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    ):
        return {"status": "deferred", "reason": "ai_gateway_not_configured"}
    try:
        if config_loader is None:
            from client.config import GatewayConfig

            config_loader = GatewayConfig.from_env
        if client_factory is None:
            from client.gateway_client import AiGatewayClient

            client_factory = AiGatewayClient
        config = config_loader()
        client = client_factory(config)
    except (ImportError, OSError, RuntimeError, ValueError):
        return {"status": "deferred", "reason": "ai_gateway_not_configured"}
    try:
        _record_operational_diagnosis_attempt(root, fingerprint)
    except OSError:
        return {"status": "deferred", "reason": "dedupe_state_unavailable"}
    try:
        result = client.execute(
            _operational_diagnosis_prompt(data_errors),
            task="operational_data_diagnosis",
            mode="review_only",
            sandbox="read-only",
            research_stage="drift_analysis",
            allowed_providers=["codex"],
            source_repository=_OPERATIONAL_DIAGNOSIS_SOURCE_REPOSITORY,
            source_ref="main",
            timeout=600,
        )
    except Exception:
        return {"status": "unavailable", "reason": "codex_outcome_unknown"}

    raw = result.raw if isinstance(getattr(result, "raw", None), dict) else {}
    if raw.get("status") == "deferred":
        try:
            _forget_operational_diagnosis_attempt(root, fingerprint)
        except OSError:
            return {"status": "unavailable", "reason": "dedupe_state_unavailable"}
        return {"status": "deferred", "reason": "capacity_unavailable"}
    if result.success is True and raw.get("status") == "succeeded" and raw.get("job_id"):
        return {"status": "succeeded", "job_id": str(raw["job_id"])}
    return {"status": "unavailable", "reason": "codex_result_unavailable"}


def diagnose_latest_cycle(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Consume the newest saved cycle only; never run collection or replay old incidents."""
    try:
        paths = sorted((root / "data/health").glob("cycle_*.json"))
        if not paths or paths[-1].stat().st_size > 1024 * 1024:
            raise ValueError("latest cycle unavailable")
        payload = json.loads(paths[-1].read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("domains") != list(DOMAINS):
            raise ValueError("invalid cycle")
        as_of = datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            raise ValueError("cycle time must include timezone")
        age = (now or datetime.now(timezone.utc)) - as_of
        if age < -timedelta(minutes=5) or age > timedelta(hours=2):
            raise ValueError("cycle is not current")
        errors = payload.get("data_errors")
        if not isinstance(errors, list) or len(errors) > 100:
            raise ValueError("invalid errors")
        for error in errors:
            if (not isinstance(error, dict) or error.get("domain") not in DOMAINS
                    or error.get("code") not in _OPERATIONAL_ERROR_CODES
                    or error.get("error_type") not in _OPERATIONAL_ERROR_TYPES):
                raise ValueError("invalid error classification")
        # Pass only fixed categories, never fields added to a saved incident.
        errors = [{key: error[key] for key in ("domain", "code", "error_type")} for error in errors]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"status": "rejected", "reason": "latest_cycle_unavailable"}
    if not errors:
        return {"status": "skipped", "reason": "no_data_errors"}
    # Workflow concurrency serializes this consumer; monitor alert state has a separate owner.
    return _run_operational_diagnosis(
        root / "data/diagnosis-consumer", errors, _operational_diagnosis_fingerprint(errors)
    )


def _build_monitoring_findings(
    strategies: list[dict[str, Any]],
    drift_results: dict[str, list[Any]],
) -> list[Any]:
    from service.strategy_watch import build_strategy_monitoring_finding

    records: dict[tuple[str, str], dict[str, Any]] = {}
    metric_keys = (
        "overall_score",
        "performance_score",
        "risk_score",
        "decay_score",
        "stability_score",
        "operational_score",
        "status",
        "as_of",
    )
    for row in strategies:
        try:
            score = float(row.get("overall_score"))
        except (TypeError, ValueError):
            continue
        if score >= SCORE_ALERT:
            continue
        domain = str(row.get("domain") or "").strip()
        profile = str(row.get("strategy_profile") or "").strip()
        if not domain or not profile:
            continue
        record = records.setdefault(
            (domain, profile),
            {"metrics": {}, "signals": [], "severity": "medium", "generated_at": ""},
        )
        record["metrics"].update({key: row[key] for key in metric_keys if key in row})
        record["signals"].append(
            {
                "metric": "overall_score",
                "reason": f"overall_score={score:.1f} is below monitoring threshold {SCORE_ALERT:.1f}",
            }
        )
        record["generated_at"] = str(row.get("as_of") or "")
        if str(row.get("status") or "").lower() == "critical" or score <= 40.0:
            record["severity"] = "high"

    for domain, drifts in drift_results.items():
        for drift in drifts:
            score = float(drift.drift_score or 0.0)
            if score < DRIFT_REVIEW:
                continue
            profile = str(drift.strategy_profile or "").strip()
            if not profile:
                continue
            record = records.setdefault(
                (domain, profile),
                {"metrics": {}, "signals": [], "severity": "medium", "generated_at": ""},
            )
            record["metrics"]["drift_score"] = score
            record["signals"].append(
                {
                    "metric": "drift_score",
                    "reason": f"drift_score={score:.2f} exceeds monitoring threshold {DRIFT_REVIEW:.2f}",
                }
            )
            if score >= DRIFT_CRITICAL:
                record["severity"] = "high"

    return [
        build_strategy_monitoring_finding(
            domain=domain,
            profile=profile,
            severity=record["severity"],
            metrics=record["metrics"],
            signals=record["signals"],
            source="quant-monitor/health-cycle",
            generated_at=record["generated_at"],
        )
        for (domain, profile), record in sorted(records.items())
    ]


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


def main() -> int:
    root = Path(os.environ.get("QUANT_MONITOR_ROOT") or Path(__file__).resolve().parents[1])
    out_dir = root / "data" / "health"
    dash_dir = out_dir / "dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    from quant_platform_kit.strategy_lifecycle.drift_detector import run_drift_detection
    from quant_platform_kit.strategy_lifecycle.health_dashboard import build_dashboard
    from quant_platform_kit.strategy_lifecycle.performance_monitor import run_monitor
    from scripts.run_strategy_optimization_watcher import dispatch_strategy_watch_findings

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

    alert_identities: list[str] = []
    monitoring_findings = _build_monitoring_findings(strategies, drift_results)
    optimization_watch = dispatch_strategy_watch_findings(
        monitoring_findings,
        dry_run=False,
        comment_existing=False,
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
    optimization_error_lines: list[str] = []
    optimization_errors = int(optimization_watch.get("errors") or 0)
    if optimization_errors:
        optimization_error_lines.append(
            f"[optimization] issue_record_failed ({optimization_errors})"
        )
        alert_identities.append(
            f"optimization_error:issue_record_failed:{optimization_errors}"
        )
    notify_lines = data_error_lines + optimization_error_lines
    telegram_sent = False
    duplicate_alert_suppressed = False
    if notify_lines:
        body = _build_alert_body(notify_lines)
        fingerprint = _alert_fingerprint(alert_identities)
        duplicate_alert_suppressed = _is_duplicate_alert(root, fingerprint)
        if not duplicate_alert_suppressed:
            telegram_sent = _send_telegram(body)
            if telegram_sent:
                _record_alert(root, fingerprint)
    else:
        _clear_alert(root)

    operational_diagnosis = _run_operational_diagnosis(
        root,
        data_errors,
        _operational_diagnosis_fingerprint(data_errors),
    )

    summary = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "domains": list(DOMAINS),
        "strategy_count": len(strategies),
        "telegram_alerts": notify_lines,
        "telegram_sent": telegram_sent,
        "duplicate_alert_suppressed": duplicate_alert_suppressed,
        "data_errors": data_errors,
        "operational_diagnosis": operational_diagnosis,
        "snapshot_count": sum(len(rows) for rows in snapshot_results.values()),
        "optimization_findings": len(monitoring_findings),
        "optimization_issues_created": len(
            [result for result in optimization_watch.get("issues", []) if result.get("created")]
        ),
        "optimization_issue_errors": optimization_errors,
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
    if sys.argv[1:] == ["--diagnose-static-token-guard-rehearsal"]:
        result = run_historical_diagnosis_rehearsal()
        print(json.dumps(result))
        sys.exit(0 if result["status"] in {"succeeded", "deferred"} else 2)
    if sys.argv[1:] == ["--diagnose-latest"]:
        result = diagnose_latest_cycle(Path(os.environ["QUANT_MONITOR_ROOT"]))
        print(json.dumps(result))
        sys.exit(0 if result["status"] in {"succeeded", "skipped", "deferred"} else 2)
    if sys.argv[1:]:
        sys.exit("Unsupported arguments")
    sys.exit(main())
