#!/usr/bin/env python3
"""Fixed, bounded, read-only systemd/journal evidence; never emits log text.

No import-time work. Host reads require --inspect or --inspect-daily-only. The
--fixture-test switch uses only in-memory fixtures and does not invoke metadata
commands. Missing evidence is unknown, never a health or deployment clearance.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
import selectors
import subprocess
import sys
import time


SERVICES = ("codex-quant.service", "codex-daily-briefing.service")
TIMERS = ("codex-quant.timer", "codex-daily-briefing.timer")
UNITS = SERVICES + TIMERS
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
TIMEOUT_SECONDS = 5
SYSTEMD_BYTES = 16 * 1024
JOURNAL_BYTES = 64 * 1024
JOURNAL_RECORDS = 64
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "SYSTEMD_COLORS": "0", "TZ": "UTC"}
COMMON_PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState", "InvocationID", "Result")
SERVICE_PROPERTIES = COMMON_PROPERTIES + (
    "MainPID", "ExecMainCode", "ExecMainStatus", "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic", "ActiveEnterTimestampMonotonic", "InactiveEnterTimestampMonotonic",
)
TIMER_PROPERTIES = COMMON_PROPERTIES + (
    "LastTriggerUSec", "LastTriggerUSecMonotonic", "NextElapseUSecRealtime", "NextElapseUSecMonotonic",
)
JOURNAL_PREFIX = (
    JOURNALCTL, "--no-pager", "--quiet", "--output=json",
    "--output-fields=_SYSTEMD_UNIT,_SYSTEMD_INVOCATION_ID,MESSAGE", "--all", "--lines=64",
)
STAGES = (
    "python_import", "process_start", "python_io", "health_cycle", "daily_pipeline",
    "briefing_directory_check", "briefing_input_processing", "briefing_input_validation",
    "summary_processing", "summary_configuration", "summary_execution", "summary_result_processing",
    "diagnosis_state_processing", "diagnosis_attempt_persistence", "diagnosis_execution",
    "diagnosis_deferred_state_update", "diagnosis_result_processing", "cycle_discovery",
    "latest_cycle_input_processing", "recent_cycle_selection", "recent_cycle_input_processing",
    "diagnosis_processing",
)
ERRORS = (
    "import_error", "executable_or_file_missing", "permission_denied", "data_or_artifact_unavailable",
    "monitor_alert", "io_error", "value_error", "type_error", "key_error", "attribute_error",
    "runtime_error", "unknown_error", "input_unavailable", "configuration_unavailable",
    "outcome_unknown", "result_unavailable", "input_limit_exceeded",
)
DATA_CODES = frozenset((
    "artifact_sync_status_unavailable", "drift_data_unavailable", "github_api_unavailable",
    "monitor_data_unavailable", "trusted_artifact_unavailable", "dashboard_data_unavailable",
    "briefing_input_unavailable", "briefing_source_time_unavailable", "runtime_projection_unreadable",
    "runtime_consumer_unavailable",
))


@dataclass(frozen=True, repr=False)
class ReadResult:
    # data is transient input, deliberately excluded from repr and public output.
    data: bytes = b""
    returncode: int | None = None
    timed_out: bool = False
    truncated: bool = False
    stderr_seen: bool = False


def properties_for(unit):
    return SERVICE_PROPERTIES if unit in SERVICES else TIMER_PROPERTIES if unit in TIMERS else ()


def known_invocation(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None and value != "0" * 32


def systemd_command(unit):
    return (SYSTEMCTL, "--no-pager", "show", unit, "--property=" + ",".join(properties_for(unit)))


def journal_command(unit, invocation):
    return JOURNAL_PREFIX + ("_SYSTEMD_UNIT=" + unit, "_SYSTEMD_INVOCATION_ID=" + invocation)


def command_limit(argv):
    if any(argv == systemd_command(unit) for unit in UNITS):
        return SYSTEMD_BYTES
    if len(argv) == len(JOURNAL_PREFIX) + 2 and argv[:len(JOURNAL_PREFIX)] == JOURNAL_PREFIX:
        for unit in SERVICES:
            prefix = "_SYSTEMD_INVOCATION_ID="
            if argv[-2] == "_SYSTEMD_UNIT=" + unit and argv[-1].startswith(prefix) and known_invocation(argv[-1][len(prefix):]):
                return JOURNAL_BYTES
    return None


def run_bounded(argv):
    """Stream both pipes within one byte/deadline bound; kill only this child.

    Reaching the bound is conservatively truncated, without reading an extra
    byte or first collecting unlimited output. stderr is counted but discarded.
    """
    argv = tuple(argv)
    limit = command_limit(argv)
    if limit is None:
        return ReadResult()
    proc = None
    output = bytearray()
    total = 0
    timed_out = False
    truncated = False
    stderr_seen = False
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=False, env=CLEAN_ENV, close_fds=True)
        with selectors.DefaultSelector() as selector:
            for stream in (proc.stdout, proc.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), min(4096, limit - total))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if key.fileobj is proc.stdout:
                        output.extend(chunk)
                    else:
                        stderr_seen = True
                    if total >= limit:
                        truncated = True
                        break
                if truncated:
                    break
            if not timed_out and not truncated:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                else:
                    try:
                        proc.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        timed_out = True
        return ReadResult(bytes(output), proc.returncode, timed_out, truncated, stderr_seen)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ReadResult()
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


def safe_read(runner, argv):
    try:
        result = runner(argv)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ReadResult()
    return result if isinstance(result, ReadResult) and isinstance(result.data, bytes) else ReadResult()


def readable(result, limit):
    return result.returncode == 0 and not (result.timed_out or result.truncated or result.stderr_seen) and len(result.data) < limit


def parse_systemd(result, unit):
    if not readable(result, SYSTEMD_BYTES):
        return None
    try:
        values = {}
        for line in result.data.decode("utf-8", errors="strict").splitlines():
            key, value = line.split("=", 1)
            if key not in properties_for(unit) or key in values:
                return None
            values[key] = value
        if set(values) != set(properties_for(unit)) or values["Id"] != unit or values["LoadState"] != "loaded":
            return None
        return values
    except (UnicodeError, ValueError):
        return None


def enum(value, allowed):
    return value if value in allowed else "unknown"


def number(value):
    return int(value) if re.fullmatch(r"[0-9]{1,20}", value or "") and int(value) <= 2**64 - 1 else None


def utc_time(value):
    if not re.fullmatch(r"[A-Z][a-z]{2} [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} UTC", value or ""):
        return None
    try:
        return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def monotonic_timespan(value):
    """systemctl CLI formats USecMonotonic as duration, not DBus integers.

    Preserve only bounded official duration tokens; do not infer UTC or time
    remaining. Zero/infinity are sentinels, never a scheduled instant.
    """
    output = {"status": "unknown", "clock": "monotonic", "elapsed_timespan": None}
    if value in ("0", "infinity"):
        output["status"] = "unset" if value == "0" else "infinite"
        return output
    if not isinstance(value, str) or not value or len(value) > 128:
        return output
    units = ("y", "month", "w", "d", "h", "min", "s", "ms", "us")
    previous = -1
    parts = value.split(" ")
    for index, part in enumerate(parts):
        match = re.fullmatch(r"([1-9][0-9]{0,19})(?:\.([0-9]{1,6}))?(y|month|w|d|h|min|s|ms|us)", part)
        if not match or int(match[1]) >= 2**64:
            return output
        position = units.index(match[3])
        if position <= previous or (match[2] and (index != len(parts) - 1 or match[3] not in ("s", "ms") or len(match[2]) != (6 if match[3] == "s" else 3))):
            return output
        previous = position
    output.update(status="known", elapsed_timespan=" ".join(parts))
    return output


def project_systemd(values, unit):
    if values is None:
        return {"available": False}
    output = {
        "available": True,
        "load_state": "loaded",
        "active_state": enum(values["ActiveState"], ("active", "inactive", "failed", "activating", "deactivating", "reloading")),
        "sub_state": enum(values["SubState"], ("dead", "failed", "running", "exited", "start", "start-pre", "start-post", "stop", "stop-post", "waiting", "elapsed")),
        "unit_file_state": enum(values["UnitFileState"], ("enabled", "enabled-runtime", "disabled", "static", "indirect", "masked", "masked-runtime", "generated", "transient", "alias", "linked", "linked-runtime")),
        "result": enum(values["Result"], ("success", "exit-code", "signal", "core-dump", "timeout", "resources", "protocol", "watchdog", "start-limit-hit", "oom-kill")),
        "invocation_known": known_invocation(values["InvocationID"]),
    }
    if unit in SERVICES:
        exit_code = number(values["ExecMainCode"])
        exit_status = number(values["ExecMainStatus"])
        output.update(
            main_pid_present=None if number(values["MainPID"]) is None else number(values["MainPID"]) > 0,
            exit_code=exit_code if exit_code in (0, 1, 2, 3) else None,
            exit_status=exit_status if exit_status is not None and exit_status <= 255 else None,
            main_started=None if number(values["ExecMainStartTimestampMonotonic"]) is None else number(values["ExecMainStartTimestampMonotonic"]) > 0,
            main_exited=None if number(values["ExecMainExitTimestampMonotonic"]) is None else number(values["ExecMainExitTimestampMonotonic"]) > 0,
        )
    else:
        output.update(last_trigger_utc=utc_time(values["LastTriggerUSec"]),
                      next_elapse_utc=utc_time(values["NextElapseUSecRealtime"]),
                      last_trigger_monotonic=monotonic_timespan(values["LastTriggerUSecMonotonic"]),
                      next_elapse_monotonic=monotonic_timespan(values["NextElapseUSecMonotonic"]))
    return output


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def evidence(message, unit, stages, errors):
    """Only fixed symbols survive; raw text/JSON is never copied into output."""
    if re.match(r"^(?:ModuleNotFoundError|ImportError):", message):
        errors["import_error"] += 1
        stages["python_import"] += 1
    elif re.match(r"^FileNotFoundError:", message):
        errors["executable_or_file_missing"] += 1
        stages["python_io"] += 1
    elif re.match(r"^PermissionError:", message):
        errors["permission_denied"] += 1
        stages["python_io"] += 1
    elif re.search(r"^(?:Failed at step EXEC spawning .+|(?:bash|/bin/bash|/usr/bin/bash|python3|/usr/bin/python3): .+): No such file or directory$", message) or re.match(r"^(?:python3|/usr/bin/python3): can't open file .+\[Errno 2\] No such file or directory", message):
        errors["executable_or_file_missing"] += 1
        stages["process_start"] += 1
    elif re.search(r"^(?:Failed at step EXEC spawning .+|(?:bash|/bin/bash|/usr/bin/bash): .+): Permission denied$", message):
        errors["permission_denied"] += 1
        stages["process_start"] += 1
    if message.startswith("[briefing-pipeline] runtime digest rejected: "):
        reason = message.removeprefix("[briefing-pipeline] runtime digest rejected: ")
        if reason in ("runtime_projection_config_invalid", "invalid_runtime_timezone", "runtime_consumer_unavailable"):
            stages["daily_pipeline"] += 1
            errors["data_or_artifact_unavailable" if reason == "runtime_consumer_unavailable" else "configuration_unavailable"] += 1
    for key, allowed, counts in (("failure_stage", STAGES, stages), ("failure_category", ERRORS, errors)):
        fragment = re.fullmatch(r'\s*"' + key + r'":\s*"([a-z_]+)"\s*,?\s*', message)
        if fragment and fragment[1] in allowed:
            counts[fragment[1]] += 1
    for key in ("code", "reason", "error"):
        fragment = re.fullmatch(r'\s*"' + key + r'":\s*"([a-z_]+)"\s*,?\s*', message)
        if fragment and fragment[1] in DATA_CODES:
            errors["data_or_artifact_unavailable"] += 1
    try:
        payload = json.loads(message, object_pairs_hook=no_duplicates)
    except (ValueError, RecursionError):
        return
    if not isinstance(payload, dict):
        return
    if unit == SERVICES[0] and all(key in payload for key in ("ok", "telegram_alerts", "data_errors", "collector_payload_valid", "snapshot_count", "optimization_findings", "optimization_issue_errors")):
        if payload["ok"] is False and isinstance(payload["telegram_alerts"], list) and isinstance(payload["data_errors"], list) and (payload["telegram_alerts"] or payload["collector_payload_valid"] is False):
            stages["health_cycle"] += 1
            errors["monitor_alert"] += 1

    def fixed_fields(item, depth=0):
        if depth > 4 or not isinstance(item, dict) or len(item) > 64:
            return
        for key, allowed, counts in (("failure_stage", STAGES, stages), ("failure_category", ERRORS, errors)):
            value = item.get(key)
            if isinstance(value, str) and value in allowed:
                counts[value] += 1
        if any(isinstance(item.get(key), str) and item[key] in DATA_CODES for key in ("code", "reason", "error")):
            errors["data_or_artifact_unavailable"] += 1
        for key in ("ai_summary", "operational_diagnosis"):
            fixed_fields(item.get(key), depth + 1)
        rows = item.get("data_errors")
        if isinstance(rows, list) and len(rows) <= 64:
            for row in rows:
                fixed_fields(row, depth + 1)

    fixed_fields(payload)


def empty_sample():
    return {"available": False, "complete": False, "absence_proven": False, "record_count": 0,
            "stage_counts": dict.fromkeys(STAGES, 0), "error_counts": dict.fromkeys(ERRORS, 0)}


def reject_json_constant(_):
    raise ValueError("unsupported_json_constant")


def inspect_journal(result, unit, invocation, *, allow_sample=False):
    stages = dict.fromkeys(STAGES, 0)
    errors = dict.fromkeys(ERRORS, 0)
    summary = {"visible": False, "current_invocation_matched": False, "record_count": 0,
               "truncated": result.truncated or len(result.data) >= JOURNAL_BYTES,
               "timed_out": result.timed_out, "read_unavailable": not readable(result, JOURNAL_BYTES),
               "malformed": False}
    if allow_sample:
        summary["sampled_evidence"] = empty_sample()
    if summary["read_unavailable"]:
        return summary, stages, errors
    try:
        lines = result.data.decode("utf-8", errors="strict").splitlines()
        if len(lines) >= JOURNAL_RECORDS:
            summary["truncated"] = True
            if not allow_sample or len(lines) > JOURNAL_RECORDS:
                return summary, stages, errors
        records = []
        for line in lines:
            record = json.loads(line, object_pairs_hook=no_duplicates, parse_constant=reject_json_constant) if allow_sample else json.loads(line, object_pairs_hook=no_duplicates)
            if not isinstance(record, dict) or not isinstance(record.get("MESSAGE"), str):
                raise ValueError("malformed_record")
            if record.get("_SYSTEMD_UNIT") != unit or record.get("_SYSTEMD_INVOCATION_ID") != invocation:
                return summary, stages, errors
            records.append(record)
        summary.update(visible=bool(records), current_invocation_matched=bool(records), record_count=len(records))
        for record in records:
            evidence(record["MESSAGE"], unit, stages, errors)
        if allow_sample and len(records) == JOURNAL_RECORDS:
            summary["sampled_evidence"] = {
                "available": True, "complete": False, "absence_proven": False, "record_count": len(records),
                "stage_counts": stages, "error_counts": errors,
            }
            return summary, dict.fromkeys(STAGES, 0), dict.fromkeys(ERRORS, 0)
    except (UnicodeError, ValueError, RecursionError):
        summary["malformed"] = True
        return summary, dict.fromkeys(STAGES, 0), dict.fromkeys(ERRORS, 0)
    return summary, stages, errors


def collect(*, runner=None, daily_only=False):
    if type(daily_only) is not bool:
        raise ValueError("unsupported_scope")
    runner = runner or run_bounded
    units = {}
    for unit in (SERVICES[1],) if daily_only else UNITS:
        before = parse_systemd(safe_read(runner, systemd_command(unit)), unit)
        journal = None
        if unit in SERVICES and before is not None and known_invocation(before["InvocationID"]):
            journal = safe_read(runner, journal_command(unit, before["InvocationID"]))
        after = parse_systemd(safe_read(runner, systemd_command(unit)), unit)
        stable = before is not None and before == after
        projected = project_systemd(after, unit)
        entry = {"systemd": projected, "snapshot_stable": stable, "diagnosis": "unknown"}
        if unit in SERVICES:
            metadata_known = projected.get("available") and all(
                projected.get(key) not in (None, "unknown") for key in (
                    "active_state", "sub_state", "unit_file_state", "result", "main_pid_present",
                    "exit_code", "exit_status", "main_started", "main_exited",
                )
            )
            entry.update(execution_phase="unknown", stage_counts=dict.fromkeys(STAGES, 0), error_counts=dict.fromkeys(ERRORS, 0))
            summary, stages, errors = inspect_journal(journal or ReadResult(), unit, before["InvocationID"] if before else "", allow_sample=daily_only)
            if daily_only:
                sample = summary.pop("sampled_evidence")
                entry["sampled_evidence"] = sample if stable and metadata_known else empty_sample()
            entry["journal"] = summary
            if stable and metadata_known and summary["current_invocation_matched"] and not (summary["truncated"] or summary["malformed"] or summary["read_unavailable"]):
                entry.update(stage_counts=stages, error_counts=errors)
                if any(value for key, value in errors.items() if key != "monitor_alert"):
                    entry["diagnosis"] = "observed_error"
                elif errors["monitor_alert"]:
                    entry["diagnosis"] = "observed_monitor_alert"
                if projected.get("main_started") is False and projected.get("result") != "success":
                    entry["execution_phase"] = "pre_start_or_start"
                elif projected.get("main_started") is True:
                    entry["execution_phase"] = "main_execution"
            if not stable:
                summary["current_invocation_matched"] = False
        units[unit] = entry
    scope = "daily_current_invocation_failure_sample" if daily_only else "fixed_current_invocation_failure_evidence"
    return {"schema_version": 1, "scope": scope, "units": units,
            "business_recovery_proven": False, "deployment_authorized": False}


def fixture_test():
    """Exercise the real CLI/collector with a tiny fake runner, never host reads."""
    invocation = "a" * 32

    def runner(argv):
        if argv[0] == SYSTEMCTL:
            unit = argv[3]
            values = dict.fromkeys(properties_for(unit), "0")
            values.update(Id=unit, LoadState="loaded", ActiveState="failed", SubState="failed",
                          UnitFileState="disabled", InvocationID=invocation, Result="exit-code")
            return ReadResult(data="".join(f"{key}={values[key]}\n" for key in properties_for(unit)).encode(), returncode=0)
        unit = argv[-2].split("=", 1)[1]
        return ReadResult(data=(json.dumps({"_SYSTEMD_UNIT": unit, "_SYSTEMD_INVOCATION_ID": invocation,
                                          "MESSAGE": "ModuleNotFoundError: fixture-private-marker"}) + "\n").encode(), returncode=0)

    result = collect(runner=runner)
    if any(result["units"][unit]["error_counts"]["import_error"] != 1 for unit in SERVICES) or "fixture-private-marker" in json.dumps(result):
        return {"fixture_test": "failed"}
    requests = []

    def daily_runner(argv):
        requests.append(argv)
        result = runner(argv)
        return ReadResult(data=result.data * JOURNAL_RECORDS, returncode=0) if argv[0] == JOURNALCTL else result

    daily = collect(runner=daily_runner, daily_only=True)
    entry = daily["units"][SERVICES[1]]
    if (set(daily["units"]) != {SERVICES[1]} or len(requests) != 3 or entry["diagnosis"] != "unknown"
            or not entry["sampled_evidence"]["available"] or entry["sampled_evidence"]["complete"]
            or entry["sampled_evidence"]["error_counts"]["import_error"] != JOURNAL_RECORDS
            or "fixture-private-marker" in json.dumps(daily)):
        return {"fixture_test": "failed"}
    return {"fixture_test": "passed", "host_metadata_commands": 0}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--fixture-test"]:
        result = fixture_test()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["fixture_test"] == "passed" else 1
    if argv not in (["--inspect"], ["--inspect-daily-only"]):
        print('{"error":"explicit_mode_required"}')
        return 64
    try:
        result = collect(daily_only=True) if argv == ["--inspect-daily-only"] else collect()
    except KeyboardInterrupt:
        print('{"error":"diagnostic_interrupted","diagnosis":"unknown"}')
        return 130
    except Exception:
        # Do not serialize exception messages, argv, raw logs, or traceback.
        print('{"error":"diagnostic_unavailable","diagnosis":"unknown"}')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
