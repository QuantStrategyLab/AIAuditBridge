#!/usr/bin/env python3
"""Import trusted Binance live-run records into the existing PerformanceStore.

The producer owns the record contents.  This consumer only reads the exact
workflow artifact, validates its provenance and shape, and persists the
recorded timestamp unchanged.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import stat
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode


REPOSITORY = "QuantStrategyLab/BinancePlatform"
WORKFLOW_PATH = ".github/workflows/main.yml"
WORKFLOW_DISPLAY_TITLE = "Runtime · strategy"
DOMAIN = "crypto"
STRATEGY_PROFILE = "crypto_live_pool_rotation"
ARTIFACT_PREFIX = "binance-live-run-"
ARTIFACT_MEMBER = "lifecycle-run.json"
LIFECYCLE_SCHEMA = "strategy_lifecycle.v1"
LIFECYCLE_STATUS_SCHEMA = "quant_monitor_lifecycle_artifact_status.v1"
SOURCE_KEY = "_source"
MAX_RECENT_DAYS = 7
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_PAGES = 20
MAX_RECORD_KEYS = 32
UTC = timezone.utc
_SAFE_STREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INTERVAL_KEYS = frozenset({
    "account_scope_sha256",
    "start_at",
    "end_at",
    "end_equity_usdt",
    "net_external_cash_flow",
    "currency",
    "valuation_basis",
})
_ALLOWED_RESULT_KEYS = frozenset({
    "platform",
    "status",
    "external_cash_flow",
    "external_cash_flow_interval",
    "total_equity_usdt",
    "trend_equity_usdt",
    "degraded_mode_level",
    "error_code",
})


class BinanceLiveRunsError(RuntimeError):
    def __init__(self, reason: str, *, unknown_days: set[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.unknown_days = set(unknown_days or ())


def _parse_utc(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BinanceLiveRunsError("invalid_timestamp") from exc
    if parsed.tzinfo is None:
        raise BinanceLiveRunsError("timestamp_without_timezone")
    return parsed.astimezone(UTC)


def _run_gh_json(path: str) -> Mapping[str, Any]:
    result = subprocess.run(
        ["gh", "api", path],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise BinanceLiveRunsError("github_api_unavailable")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BinanceLiveRunsError("github_api_invalid") from exc
    if not isinstance(payload, Mapping):
        raise BinanceLiveRunsError("github_api_invalid")
    return payload


def _run_gh_bytes(path: str) -> bytes:
    process = subprocess.Popen(
        ["gh", "api", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    data = process.stdout.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        process.kill()
        process.wait()
        raise BinanceLiveRunsError("artifact_too_large")
    return_code = process.wait()
    if return_code:
        raise BinanceLiveRunsError("github_artifact_unavailable")
    return data


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _mark_crypto_unavailable(path: Path, *, reason: str, now: datetime) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        payload = {}
    existing_status = isinstance(payload, dict) and payload.get("schema_version") == LIFECYCLE_STATUS_SCHEMA
    if not existing_status:
        payload = {
            "schema_version": LIFECYCLE_STATUS_SCHEMA,
            "domains": {},
            "as_of": now.astimezone(UTC).isoformat(),
        }
    domains = payload.get("domains")
    if not isinstance(domains, dict):
        domains = {}
        payload["domains"] = domains
    domains[DOMAIN] = {
        "status": "error",
        "code": "artifact_sync_status_unavailable",
        "error_type": "RuntimeError",
        "source": "binance_live_runs",
        "reason_code": reason,
    }
    payload["ok"] = False
    _write_json(path, payload)


def _run_pages(
    gh_json: Callable[[str], Mapping[str, Any]],
    path_prefix: str,
    collection_key: str,
) -> list[Mapping[str, Any]]:
    values: list[Mapping[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        path = f"{path_prefix}&{urlencode({'page': page})}"
        payload = gh_json(path)
        page_values = payload.get(collection_key)
        if not isinstance(page_values, list):
            raise BinanceLiveRunsError("github_api_invalid")
        for value in page_values:
            if not isinstance(value, Mapping):
                raise BinanceLiveRunsError("github_api_invalid")
            values.append(value)
        if len(page_values) < 100:
            return values
    raise BinanceLiveRunsError("github_list_incomplete")


def _trusted_strategy_runs(
    gh_json: Callable[[str], Mapping[str, Any]],
    *,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    path = (
        f"/repos/{REPOSITORY}/actions/workflows/main.yml/runs?"
        + urlencode({
            "branch": "main",
            "event": "workflow_dispatch",
            "created": f">={start_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "per_page": 100,
        })
    )
    selected: list[dict[str, Any]] = []
    for raw in _run_pages(gh_json, path, "workflow_runs"):
        if (
            raw.get("event") != "workflow_dispatch"
            or raw.get("head_branch") != "main"
            or raw.get("path") != WORKFLOW_PATH
            or raw.get("display_title") != WORKFLOW_DISPLAY_TITLE
        ):
            continue
        head_repository = raw.get("head_repository")
        if not isinstance(head_repository, Mapping) or head_repository.get("full_name") != REPOSITORY:
            continue
        created_at = _parse_utc(raw.get("created_at"))
        if created_at < start_at or created_at > end_at:
            continue
        try:
            run_id = int(raw.get("id"))
            run_attempt = int(raw.get("run_attempt"))
        except (TypeError, ValueError) as exc:
            raise BinanceLiveRunsError("run_identity_invalid", unknown_days={created_at.date().isoformat()}) from exc
        if run_id <= 0 or run_attempt <= 0:
            raise BinanceLiveRunsError("run_identity_invalid", unknown_days={created_at.date().isoformat()})
        selected.append({
            "id": run_id,
            "run_attempt": run_attempt,
            "created_at": created_at,
            "updated_at": _parse_utc(raw.get("updated_at") or raw.get("created_at")),
            "status": raw.get("status"),
            "conclusion": raw.get("conclusion"),
        })
    return selected


def _find_artifact(
    gh_json: Callable[[str], Mapping[str, Any]],
    *,
    run_id: int,
    run_attempt: int,
) -> Mapping[str, Any]:
    path = f"/repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100"
    expected_name = f"{ARTIFACT_PREFIX}{run_id}-{run_attempt}"
    candidates = []
    for artifact in _run_pages(gh_json, path, "artifacts"):
        if artifact.get("name") != expected_name or artifact.get("expired") is True:
            continue
        workflow_run = artifact.get("workflow_run")
        if not isinstance(workflow_run, Mapping):
            raise BinanceLiveRunsError("artifact_metadata_invalid")
        try:
            workflow_run_id = int(workflow_run.get("id") or 0)
        except (TypeError, ValueError) as exc:
            raise BinanceLiveRunsError("artifact_metadata_invalid") from exc
        if workflow_run_id != run_id:
            continue
        candidates.append(artifact)
    if len(candidates) != 1:
        raise BinanceLiveRunsError("artifact_missing" if not candidates else "artifact_ambiguous")
    artifact = candidates[0]
    try:
        artifact_id = int(artifact.get("id"))
        size = int(artifact.get("size_in_bytes") or 0)
    except (TypeError, ValueError) as exc:
        raise BinanceLiveRunsError("artifact_metadata_invalid") from exc
    if artifact_id <= 0 or size <= 0 or size > MAX_ARCHIVE_BYTES:
        raise BinanceLiveRunsError("artifact_metadata_invalid")
    return {"id": artifact_id, "size_in_bytes": size, "name": expected_name}


def _safe_member(member: zipfile.ZipInfo) -> None:
    mode = (member.external_attr >> 16) & 0xFFFF
    if stat.S_IFMT(mode) and not stat.S_ISREG(mode):
        raise BinanceLiveRunsError("artifact_symlink_or_nonregular")
    if member.filename != ARTIFACT_MEMBER:
        raise BinanceLiveRunsError("artifact_member_invalid")
    if member.file_size <= 0 or member.file_size > MAX_RECORD_BYTES:
        raise BinanceLiveRunsError("record_too_large")


def _validate_interval(value: Any, *, unknown_day: str) -> None:
    if not isinstance(value, dict) or set(value) != _INTERVAL_KEYS:
        raise BinanceLiveRunsError("external_cash_flow_interval_invalid", unknown_days={unknown_day})
    scope = str(value.get("account_scope_sha256") or "")
    if not _SHA256.fullmatch(scope):
        raise BinanceLiveRunsError("external_cash_flow_interval_invalid", unknown_days={unknown_day})
    try:
        start_at = _parse_utc(value.get("start_at"))
        end_at = _parse_utc(value.get("end_at"))
    except BinanceLiveRunsError as exc:
        raise BinanceLiveRunsError("external_cash_flow_interval_invalid", unknown_days={unknown_day}) from exc
    if start_at >= end_at:
        raise BinanceLiveRunsError("external_cash_flow_interval_invalid", unknown_days={unknown_day})
    for key in ("end_equity_usdt", "net_external_cash_flow"):
        number = value.get(key)
        if isinstance(number, bool):
            raise BinanceLiveRunsError("external_cash_flow_interval_invalid", unknown_days={unknown_day})
        try:
            parsed = float(number)
        except (TypeError, ValueError):
            parsed = float("nan")
        if not math.isfinite(parsed):
            raise BinanceLiveRunsError("external_cash_flow_interval_invalid", unknown_days={unknown_day})
    if value.get("currency") != "USDT" or value.get("valuation_basis") != "checkpoint_quantities_sampled_prices":
        raise BinanceLiveRunsError("external_cash_flow_interval_invalid", unknown_days={unknown_day})


def _validate_record(raw: bytes, *, run_start: datetime, run_end: datetime) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BinanceLiveRunsError("record_invalid") from exc
    if not isinstance(payload, dict) or len(payload) > MAX_RECORD_KEYS:
        raise BinanceLiveRunsError("record_invalid")
    expected = {
        "schema_version",
        "strategy_profile",
        "domain",
        "recorded_at",
        "record_kind",
        "lifecycle_stream_id",
        "execution_result",
    }
    if set(payload) != expected or payload.get("schema_version") != LIFECYCLE_SCHEMA:
        raise BinanceLiveRunsError("record_invalid")
    if payload.get("strategy_profile") != STRATEGY_PROFILE or payload.get("domain") != DOMAIN:
        raise BinanceLiveRunsError("record_scope_mismatch")
    recorded_at = _parse_utc(payload.get("recorded_at"))
    if payload.get("record_kind") != "execution":
        raise BinanceLiveRunsError("record_kind_invalid", unknown_days={recorded_at.date().isoformat()})
    stream = str(payload.get("lifecycle_stream_id") or "")
    if not _SAFE_STREAM.fullmatch(stream):
        raise BinanceLiveRunsError("stream_id_invalid", unknown_days={recorded_at.date().isoformat()})
    result = payload.get("execution_result")
    if not isinstance(result, dict) or result.get("platform") != "binance":
        raise BinanceLiveRunsError("execution_result_invalid", unknown_days={recorded_at.date().isoformat()})
    if set(result) - _ALLOWED_RESULT_KEYS:
        raise BinanceLiveRunsError("execution_result_contains_unknown_fields", unknown_days={recorded_at.date().isoformat()})
    required_result_keys = {
        "platform",
        "status",
        "external_cash_flow",
        "external_cash_flow_interval",
        "total_equity_usdt",
        "trend_equity_usdt",
        "degraded_mode_level",
    }
    if not required_result_keys.issubset(result):
        raise BinanceLiveRunsError("execution_result_invalid", unknown_days={recorded_at.date().isoformat()})
    if "external_cash_flow" not in result or result["external_cash_flow"] is not None:
        raise BinanceLiveRunsError("external_cash_flow_invalid", unknown_days={recorded_at.date().isoformat()})
    if "external_cash_flow_interval" in result and result["external_cash_flow_interval"] is not None:
        _validate_interval(result["external_cash_flow_interval"], unknown_day=recorded_at.date().isoformat())
    if "error_code" in result and result["error_code"] is not None:
        if not isinstance(result["error_code"], str) or not re.fullmatch(r"[a-z0-9_]{1,64}", result["error_code"]):
            raise BinanceLiveRunsError("error_code_invalid", unknown_days={recorded_at.date().isoformat()})
    if recorded_at < run_start - timedelta(minutes=5) or recorded_at > run_end + timedelta(minutes=5):
        raise BinanceLiveRunsError("recorded_at_outside_run", unknown_days={recorded_at.date().isoformat()})
    return payload


def _read_archive(archive_bytes: bytes, *, run_start: datetime, run_end: datetime) -> dict[str, Any]:
    if len(archive_bytes) == 0 or len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise BinanceLiveRunsError("artifact_too_large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except (OSError, zipfile.BadZipFile) as exc:
        raise BinanceLiveRunsError("artifact_invalid") from exc
    with archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if len(members) != 1:
            raise BinanceLiveRunsError("artifact_incomplete")
        member = members[0]
        _safe_member(member)
        try:
            raw = archive.read(member)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise BinanceLiveRunsError("artifact_invalid") from exc
    return _validate_record(raw, run_start=run_start, run_end=run_end)


def _gap_record(recorded_at: datetime, *, stream_id: str) -> dict[str, Any]:
    """Write an explicit unknown point so ReturnCollector cuts continuity."""
    return {
        "schema_version": LIFECYCLE_SCHEMA,
        "strategy_profile": STRATEGY_PROFILE,
        "domain": DOMAIN,
        "recorded_at": recorded_at.astimezone(UTC).isoformat(),
        "record_kind": "execution",
        "lifecycle_stream_id": stream_id,
        "execution_result": {
            "platform": "binance",
            "status": "unknown",
            "external_cash_flow": None,
            "external_cash_flow_interval": None,
            "total_equity_usdt": None,
            "trend_equity_usdt": None,
            "degraded_mode_level": None,
        },
    }


def _source_entry(
    *,
    run_id: int,
    run_attempt: int,
    artifact_id: int | None,
    kind: str,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "repository": REPOSITORY,
        "workflow_path": WORKFLOW_PATH,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "artifact_id": artifact_id,
        "kind": kind,
        "reason": reason,
    }


def _with_source(payload: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    enriched[SOURCE_KEY] = {"entries": [dict(entry)]}
    return enriched


def _record_core(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != SOURCE_KEY}


def _source_entries(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    source = payload.get(SOURCE_KEY)
    entries = source.get("entries") if isinstance(source, Mapping) else None
    return [entry for entry in entries if isinstance(entry, Mapping)] if isinstance(entries, list) else []


def sync_live_runs(
    *,
    start_at: datetime,
    now: datetime,
    gh_json: Callable[[str], Mapping[str, Any]],
    gh_bytes: Callable[[str], bytes],
    store: Any,
    lifecycle_status_path: Path,
    stream_id: str = "binance",
) -> dict[str, Any]:
    start_at = start_at.astimezone(UTC)
    now = now.astimezone(UTC)
    if start_at > now:
        raise BinanceLiveRunsError("start_at_in_future")
    effective_start = max(start_at, now - timedelta(days=MAX_RECENT_DAYS))
    if not _SAFE_STREAM.fullmatch(stream_id):
        raise BinanceLiveRunsError("stream_id_invalid")
    runs = _trusted_strategy_runs(gh_json, start_at=effective_start, end_at=now)
    pending: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    gap_days: set[str] = set()
    critical_reasons = {"github_api_unavailable", "github_api_invalid", "github_list_incomplete"}
    existing_records = store.list_live_run_records(
        DOMAIN,
        strategy_profile=STRATEGY_PROFILE,
        stream_id=stream_id,
    )
    existing_by_identity = {
        (str(item.get("lifecycle_stream_id") or ""), str(item.get("recorded_at") or "")): item
        for item in existing_records
        if isinstance(item, Mapping)
    }
    existing_attempts: set[tuple[int, int]] = set()
    for item in existing_records:
        for entry in _source_entries(item):
            if (
                str(entry.get("kind") or "") != "artifact"
                or str(entry.get("repository") or "") != REPOSITORY
                or str(entry.get("workflow_path") or "") != WORKFLOW_PATH
            ):
                continue
            try:
                run_id = int(entry.get("run_id"))
                run_attempt = int(entry.get("run_attempt"))
            except (TypeError, ValueError):
                continue
            if run_id > 0 and run_attempt > 0:
                existing_attempts.add((run_id, run_attempt))
    for run in runs:
        run_day = run["created_at"].date().isoformat()
        run_start = run["created_at"]
        run_end = max(run_start, run["updated_at"])
        for attempt in range(1, run["run_attempt"] + 1):
            if (run["id"], attempt) in existing_attempts:
                continue
            payload: dict[str, Any]
            source_entry: dict[str, Any]
            if run["status"] != "completed":
                continue
            if not run["conclusion"]:
                payload = _gap_record(run_start, stream_id=stream_id)
                source_entry = _source_entry(
                    run_id=run["id"], run_attempt=attempt, artifact_id=None,
                    kind="gap", reason="run_conclusion_unknown",
                )
                gap_days.add(run_day)
            else:
                try:
                    artifact = _find_artifact(
                        gh_json,
                        run_id=run["id"],
                        run_attempt=attempt,
                    )
                    payload = _read_archive(
                        gh_bytes(f"/repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"),
                        run_start=run_start,
                        run_end=run_end,
                    )
                    recorded_at = _parse_utc(payload["recorded_at"])
                    if recorded_at < effective_start or recorded_at > now + timedelta(minutes=5):
                        raise BinanceLiveRunsError("recorded_at_outside_window", unknown_days={run_day})
                    source_entry = _source_entry(
                        run_id=run["id"], run_attempt=attempt, artifact_id=artifact["id"], kind="artifact",
                    )
                except BinanceLiveRunsError as exc:
                    if exc.reason in critical_reasons:
                        raise
                    payload = _gap_record(run_start, stream_id=stream_id)
                    source_entry = _source_entry(
                        run_id=run["id"], run_attempt=attempt, artifact_id=None,
                        kind="gap", reason=exc.reason,
                    )
                    gap_days.update(exc.unknown_days or {run_day})
            payload = _with_source(payload, source_entry)
            identity = (
                str(payload["lifecycle_stream_id"]),
                str(payload["recorded_at"]),
            )
            existing = seen.get(identity)
            if existing is not None and _record_core(existing) != _record_core(payload):
                raise BinanceLiveRunsError("duplicate_record_conflict", unknown_days={run_day})
            seen[identity] = payload
            if existing is None:
                pending.append(payload)
    if not runs:
        return {
            "status": "no_new_run",
            "runs_seen": 0,
            "imported_records": 0,
            "unknown_days": [],
        }
    for payload in pending:
        identity = (str(payload["lifecycle_stream_id"]), str(payload["recorded_at"]))
        previous = existing_by_identity.get(identity)
        if previous is not None and _record_core(previous) != _record_core(payload):
            raise BinanceLiveRunsError("existing_record_conflict", unknown_days={identity[1][:10]})
    saved_count = 0
    for payload in pending:
        identity = (str(payload["lifecycle_stream_id"]), str(payload["recorded_at"]))
        if identity in existing_by_identity:
            continue
        stream_id = str(payload["lifecycle_stream_id"])
        store.save_live_run_record(
            STRATEGY_PROFILE,
            DOMAIN,
            payload,
            stream_id=stream_id,
        )
        saved_count += 1
    if gap_days:
        return {
            "status": "ready_with_gaps",
            "reason": "unknown_or_incomplete_live_run",
            "unknown_days": sorted(gap_days),
            "imported_records": saved_count,
            "runs_seen": len(runs),
        }
    return {
        "status": "ready",
        "runs_seen": len(runs),
        "imported_records": saved_count,
        "unknown_days": [],
    }


def _env_datetime(env: Mapping[str, str], key: str) -> datetime:
    raw = str(env.get(key) or "").strip()
    if not raw:
        raise BinanceLiveRunsError("missing_start_at")
    return _parse_utc(raw)


def run_from_environment(
    *,
    env: Mapping[str, str] | None = None,
    gh_json: Callable[[str], Mapping[str, Any]] = _run_gh_json,
    gh_bytes: Callable[[str], bytes] = _run_gh_bytes,
    now: datetime | None = None,
) -> dict[str, Any]:
    env = env or os.environ
    if str(env.get("BINANCE_LIVE_RUNS_SYNC_ENABLED") or "").lower() not in {"1", "true", "yes"}:
        return {"status": "disabled", "reason": "not_enabled"}
    root = Path(env.get("QUANT_MONITOR_ROOT") or Path(__file__).resolve().parents[1])
    lifecycle_status_path = root / "data" / "lifecycle-artifacts" / "status.json"
    try:
        start_at = _env_datetime(env, "BINANCE_LIVE_RUNS_START_AT")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore

        result = sync_live_runs(
            start_at=start_at,
            now=current,
            gh_json=gh_json,
            gh_bytes=gh_bytes,
            store=PerformanceStore.from_env(),
            lifecycle_status_path=lifecycle_status_path,
            stream_id=str(env.get("BINANCE_LIVE_RUNS_STREAM_ID") or "binance"),
        )
    except BinanceLiveRunsError as exc:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        _mark_crypto_unavailable(lifecycle_status_path, reason=exc.reason, now=current)
        return {
            "status": "blocked",
            "reason": exc.reason,
            "unknown_days": sorted(exc.unknown_days),
            "imported_records": 0,
            "runs_seen": 0,
        }
    except Exception:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        _mark_crypto_unavailable(lifecycle_status_path, reason="consumer_unavailable", now=current)
        return {
            "status": "blocked",
            "reason": "consumer_unavailable",
            "unknown_days": [],
            "imported_records": 0,
            "runs_seen": 0,
        }
    print(json.dumps(result, sort_keys=True))
    return result


def main() -> int:
    result = run_from_environment()
    if result.get("status") == "blocked":
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
