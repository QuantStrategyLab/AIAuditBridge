#!/usr/bin/env python3
"""Build per-domain daily briefing JSON for AIAuditBridge consume (task 10)."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DOMAINS = ("cn_equity", "hk_equity", "us_equity", "crypto")
_STATUS_RELATIVE_PATH = Path("data/lifecycle-artifacts/status.json")
_HEALTH_STATUS_ORDER = ("healthy", "watch", "review", "critical")
_ALLOWED_HEALTH_STATUSES = set(_HEALTH_STATUS_ORDER)


def _collect_drift_results(run_drift_detection, *, domains=DOMAINS):
    results: dict[str, list[Any]] = {}
    errors: dict[str, dict[str, str]] = {}
    for domain in domains:
        try:
            results[domain] = list(run_drift_detection(domain))
        except Exception as exc:
            errors[domain] = {
                "code": "drift_data_unavailable",
                "error_type": type(exc).__name__,
            }
    return results, errors


def _status_counts(strategies: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in _HEALTH_STATUS_ORDER}
    for row in strategies:
        status = str(row["status"]).lower()
        counts[status] += 1
    return counts


def _health_cycle_module():
    spec = importlib.util.spec_from_file_location(
        "quant_monitor_health_cycle_for_daily_briefing",
        Path(__file__).with_name("health_cycle.py"),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("health_cycle_validation_unavailable")
    health_cycle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health_cycle)
    return health_cycle


def _load_expected_coverage(root: Path):
    """Return trusted artifact coverage, source timestamp, and domain errors."""
    health_cycle = _health_cycle_module()

    return health_cycle._load_expected_coverage(root, domains=DOMAINS)


def _valid_dashboard_rows(
    payload: Any,
    *,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, str] | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("strategies"), list):
        return [], {}, {"code": "dashboard_data_unavailable", "error_type": "ValueError"}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    errors_by_domain: dict[str, dict[str, str]] = {}
    for row in payload["strategies"]:
        if not isinstance(row, dict):
            return [], {}, {"code": "dashboard_data_unavailable", "error_type": "ValueError"}
        domain = row.get("domain")
        if not isinstance(domain, str) or domain not in DOMAINS:
            return [], {}, {"code": "dashboard_data_unavailable", "error_type": "ValueError"}
        profile = row.get("strategy_profile")
        status = row.get("status")
        observed_raw = row.get("as_of")
        if status == "unavailable":
            errors_by_domain.setdefault(
                domain, {"code": "dashboard_data_unavailable", "error_type": "ValueError"}
            )
            continue
        try:
            observed = date.fromisoformat(observed_raw)
            observation_age_days = ((today or datetime.now(timezone.utc).date()) - observed).days
        except (TypeError, ValueError):
            errors_by_domain.setdefault(
                domain, {"code": "dashboard_data_unavailable", "error_type": "ValueError"}
            )
            continue
        if not isinstance(profile, str) or not profile:
            errors_by_domain.setdefault(
                domain, {"code": "dashboard_data_unavailable", "error_type": "ValueError"}
            )
            continue
        key = (domain, profile)
        # Keep the same seven-natural-day window as the existing AI summary consumer.
        if (
            not isinstance(status, str)
            or status not in _ALLOWED_HEALTH_STATUSES
            or observed.isoformat() != observed_raw
            or not 0 <= observation_age_days <= 7
            or key in seen
        ):
            errors_by_domain.setdefault(
                domain, {"code": "dashboard_data_unavailable", "error_type": "ValueError"}
            )
            continue
        seen.add(key)
        rows.append(dict(row))
    return rows, errors_by_domain, None


def main() -> int:
    root = Path(os.environ.get("QUANT_MONITOR_ROOT") or Path(__file__).resolve().parents[1])
    now_utc = datetime.now(timezone.utc)
    day = os.environ.get("DAY") or now_utc.strftime("%Y-%m-%d")
    out_dir = root / "data" / "daily-reports" / day
    out_dir.mkdir(parents=True, exist_ok=True)

    from quant_platform_kit.strategy_lifecycle.drift_detector import run_drift_detection
    from quant_platform_kit.strategy_lifecycle.health_dashboard import build_dashboard

    expected_profiles, source_revisions, source_as_of, artifact_errors, not_configured = _load_expected_coverage(root)
    ready_domains = tuple(domain for domain in DOMAINS if domain in expected_profiles)
    live_coverage_errors: dict[str, str] = {}
    live_drift_errors: dict[str, str] = {}
    live_snapshots: list[Any] = []
    health_cycle = None
    live_profile = "crypto_live_pool_rotation"
    if live_profile in expected_profiles.get("crypto", []):
        try:
            from quant_platform_kit.strategy_lifecycle.performance_monitor import run_monitor

            health_cycle = _health_cycle_module()
            live_snapshots, live_error = health_cycle._run_binance_live_profile_monitor(
                run_monitor, source_revision=source_revisions["crypto"],
            )
            if live_error:
                live_coverage_errors["crypto"] = live_error
        except Exception:
            live_coverage_errors["crypto"] = "live_coverage_incomplete"

    def run_expected_drift(domain: str):
        if domain != "crypto" or live_profile not in expected_profiles.get(domain, []):
            return run_drift_detection(domain)
        if health_cycle is None:
            live_drift_errors[domain] = "live_drift_snapshot_unavailable"
            results = []
            for profile in expected_profiles[domain]:
                if profile == live_profile:
                    continue
                try:
                    results.extend(run_drift_detection(domain, strategy_profile=profile))
                except Exception:
                    continue
            return results
        drifts, live_error = health_cycle._run_expected_monitor_drifts(
            run_drift_detection,
            domain,
            expected_profiles[domain],
            live_snapshots[0] if live_snapshots and domain not in live_coverage_errors else None,
        )
        if live_error:
            live_drift_errors[domain] = live_error
        return drifts

    drift_results, drift_errors = _collect_drift_results(run_expected_drift, domains=ready_domains)
    drift_by_key: dict[tuple[str, str], float] = {}
    drift_result_by_key: dict[tuple[str, str], Any] = {}
    for domain, domain_results in drift_results.items():
        for drift in domain_results:
            profile = getattr(drift, "strategy_profile", None)
            score = getattr(drift, "drift_score", None)
            try:
                numeric_score = float(score)
            except (TypeError, ValueError):
                numeric_score = math.nan
            key = (domain, profile)
            if not isinstance(profile, str) or not profile or not math.isfinite(numeric_score) or key in drift_by_key:
                drift_errors[domain] = {"code": "drift_data_unavailable", "error_type": "ValueError"}
                continue
            drift_by_key[key] = numeric_score
            drift_result_by_key[key] = drift

    with tempfile.TemporaryDirectory() as tmp:
        dashboard_error: dict[str, str] | None = None
        dashboard_errors_by_domain: dict[str, dict[str, str]] = {}
        try:
            build_dashboard(output_dir=tmp, output_format="json", domains=ready_domains)
            dash_path = Path(tmp) / "strategy_health_dashboard.json"
            if not dash_path.is_file():
                raise FileNotFoundError
            dashboard_payload = json.loads(dash_path.read_text(encoding="utf-8"))
            strategies_raw, dashboard_errors_by_domain, dashboard_error = _valid_dashboard_rows(
                dashboard_payload, today=now_utc.date(),
            )
            # The global dashboard's latest profile snapshot is not keyed by
            # live stream. Replace it with the exact verified monitor result.
            strategies_raw = [
                row for row in strategies_raw
                if not (row["domain"] == "crypto" and row["strategy_profile"] == live_profile)
            ]
            if (live_snapshots and "crypto" not in live_coverage_errors
                    and "crypto" not in live_drift_errors):
                try:
                    from quant_platform_kit.strategy_lifecycle.strategy_health_score import compute_health_score

                    live_score = compute_health_score(
                        live_snapshots[0], drift=drift_result_by_key.get(("crypto", live_profile)),
                    )
                    verified_rows, verified_errors, verified_error = _valid_dashboard_rows(
                        {"strategies": [live_score.to_dict()]}, today=now_utc.date(),
                    )
                    if (verified_error or verified_errors or len(verified_rows) != 1
                            or verified_rows[0]["strategy_profile"] != live_profile):
                        live_coverage_errors["crypto"] = "live_coverage_incomplete"
                    else:
                        strategies_raw.extend(verified_rows)
                except Exception:
                    live_coverage_errors["crypto"] = "live_coverage_incomplete"
        except Exception as exc:
            strategies_raw = []
            dashboard_error = {"code": "dashboard_data_unavailable", "error_type": type(exc).__name__}
            dashboard_errors_by_domain = {}

    by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    observed_profiles: dict[str, set[str]] = defaultdict(set)
    for row in strategies_raw:
        domain = row["domain"]
        profile = row["strategy_profile"]
        if domain not in DOMAINS:
            for ready_domain in ready_domains:
                artifact_errors[ready_domain] = {
                    "domain": ready_domain,
                    "code": "dashboard_coverage_unexpected",
                    "error_type": "ValueError",
                }
            continue
        if domain not in expected_profiles or profile not in expected_profiles[domain]:
            artifact_errors[domain] = {
                "domain": domain,
                "code": "dashboard_coverage_unexpected",
                "error_type": "ValueError",
            }
            continue
        enriched = dict(row)
        if (domain, profile) not in drift_by_key:
            drift_errors[domain] = {"code": "drift_data_unavailable", "error_type": "KeyError"}
        else:
            enriched["drift_score"] = drift_by_key[(domain, profile)]
        by_domain[domain].append(enriched)
        observed_profiles[domain].add(profile)

    generated_at = now_utc.isoformat()
    for domain in DOMAINS:
        strategies = by_domain.get(domain, [])
        domain_errors = []
        if domain in artifact_errors:
            domain_errors.append({key: value for key, value in artifact_errors[domain].items() if key != "domain"})
        if domain in live_coverage_errors:
            domain_errors.append({
                "code": "monitor_data_unavailable",
                "error_type": "RuntimeError",
                "reason_code": live_coverage_errors[domain],
            })
        if domain in live_drift_errors:
            domain_errors.append({
                "code": "drift_data_unavailable",
                "error_type": "RuntimeError",
                "reason_code": live_drift_errors[domain],
            })
        if dashboard_error and domain in expected_profiles:
            domain_errors.append(dashboard_error)
        if domain in dashboard_errors_by_domain:
            domain_errors.append(dashboard_errors_by_domain[domain])
        if domain in drift_errors:
            domain_errors.append(drift_errors[domain])
        expected = set(expected_profiles.get(domain, []))
        missing = sorted(expected - observed_profiles.get(domain, set()))
        if missing and not dashboard_error:
            domain_errors.append({"code": "dashboard_coverage_incomplete", "error_type": "ValueError"})
        missing_drift = sorted(profile for profile in expected if (domain, profile) not in drift_by_key)
        if missing_drift and domain not in drift_errors:
            domain_errors.append({"code": "drift_coverage_incomplete", "error_type": "ValueError"})

        report = {
            "domain": domain,
            "ok": not domain_errors,
            "data_status": "unavailable" if domain_errors else ("not_configured" if domain in not_configured else "ready"),
            "as_of": source_as_of,
            "generated_at": generated_at,
            "source_revision": source_revisions.get(domain),
            "coverage": {
                "expected_profiles": sorted(expected),
                "observed_profiles": sorted(observed_profiles.get(domain, set())),
                "missing_profiles": missing,
            },
            "strategies": strategies,
            "summary": _status_counts(strategies),
            "errors": domain_errors,
        }
        if domain_errors:
            error = domain_errors[0]
            report["error"] = f"{error['code']}:{error['error_type']}"
        path = out_dir / f"{domain}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[briefing] wrote {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
