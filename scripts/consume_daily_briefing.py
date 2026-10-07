#!/usr/bin/env python3
"""CLI entrypoint for quant-monitor daily briefing consumption."""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops/quant-monitor"))
from quant_monitor_domain.briefing_consumer import consume_briefing_dir, summarize_briefing
from quant_monitor_domain.briefing_dispatch import dispatch_briefing_result, dispatch_runtime_digest
from quant_monitor_domain.runtime_digest import prepare_runtime_digest


def _canonical_iso_day(value: str) -> str | None:
    if len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    rendered = parsed.isoformat()
    if rendered != value:
        return None
    return rendered


def _producer_target_key(key: object) -> bool:
    """Accept only the identity string the producer already emits.

    That string is ``service|strategy|scope`` after strip and lowercase.
    Empty strategy or scope is ``*``. This does not rewrite the caller's key.
    """
    if not isinstance(key, str) or not key or any(char.isspace() for char in key):
        return False
    parts = key.split("|")
    if len(parts) != 3:
        return False
    service, strategy, scope = parts
    if not service or not strategy or not scope:
        return False
    return (
        service == service.strip().lower()
        and strategy == strategy.strip().lower()
        and scope == scope.strip().lower()
    )


def _runtime_day_problem(day: str, business_date: object, *, required: bool) -> str | None:
    if not day:
        return "missing_dispatch_day" if required else None
    if _canonical_iso_day(day) is None:
        return "invalid_business_day"
    if day != business_date:
        return "business_date_mismatch"
    return None


def _runtime_target_problem(keys: list[str] | None, target_scope: object, *, required: bool) -> str | None:
    if not keys:
        return "missing_expected_target" if required else None
    seen: set[str] = set()
    for key in keys:
        if not _producer_target_key(key):
            return "invalid_expected_target"
        if key in seen:
            return "duplicate_expected_target"
        seen.add(key)
    expected = set(target_scope) if isinstance(target_scope, list) else set()
    if seen != expected:
        return "expected_target_mismatch"
    return None


# Only fixed interface codes are projected to stderr. Never stringify input,
# exception text, dispatch bodies, paths, dates, or target/account identities.
_RUNTIME_INPUT_REASONS = frozenset({
    "malformed", "unsupported_platform", "invalid_observed_at", "invalid_completeness",
    "empty_records", "invalid_target", "duplicate_target", "invalid_business_date",
    "invalid_timezone", "invalid_status", "fills_not_connected", "schedule_hides_anomaly",
    "invalid_run", "invalid_run_time", "invalid_lane", "business_date_conflict",
    "timezone_conflict", "unsafe_text",
})
_RUNTIME_BINDING_REASONS = frozenset({
    "missing_dispatch_day", "invalid_business_day", "business_date_mismatch",
    "missing_expected_target", "invalid_expected_target", "duplicate_expected_target",
    "expected_target_mismatch", "expected_scope_not_paper", "invalid_runtime_object",
    "runtime_object_day_mismatch",
})
_RUNTIME_READ_REASONS = frozenset({
    "runtime_projection_unreadable", "runtime_projection_timeout", "runtime_projection_too_large",
})
_DISPATCH_REASONS = frozenset({
    "optimization_record_failed", "github_issue_record_failed", "telegram_missing_env",
    "alert_state_root_unavailable", "alert_state_unreadable", "alert_state_malformed",
    "alert_state_write_failed", "telegram_delivery_unknown", "telegram_delivery_failed",
    "runtime_digest_too_long", "github_issue_target_invalid", "gh_executable_missing",
})
_RESULT_REASONS = (_RUNTIME_INPUT_REASONS | _RUNTIME_BINDING_REASONS | _RUNTIME_READ_REASONS
                   | _DISPATCH_REASONS | {"unknown", "quiet", "telegram_attention",
                   "github_issue_recorded", "dispatch_not_requested", "dispatch_completed",
                   "dual_review_disagreement", "report_dir_not_found"})


def _emit_result(*, branch: str, stage: str, reason: object, action: str,
                 dispatch_failed: str = "unknown", exit_code: int) -> None:
    branch = branch if branch in {"domain", "runtime"} else "unknown"
    stage = stage if stage in {"input_read", "input_validation", "binding_validation", "dispatch", "routing"} else "unknown"
    reason = reason if isinstance(reason, str) and reason in _RESULT_REASONS else "unknown"
    action = action if action in {"none", "quiet", "github_issue", "telegram", "runtime_digest"} else "unknown"
    dispatch_failed = dispatch_failed if dispatch_failed in {"true", "false", "unknown"} else "unknown"
    # All exits at these existing return sites are fixed small integer codes.
    rendered_exit = str(exit_code) if type(exit_code) is int and 0 <= exit_code <= 255 else "unknown"
    print(f"[briefing-result:v1] branch={branch} stage={stage} reason={reason} "
          f"action={action} dispatch_failed={dispatch_failed} exit={rendered_exit}", file=sys.stderr)


def _dispatch_failure_evidence(summary: object) -> str:
    """Project failure evidence, never equate no errors with confirmed delivery."""
    if not isinstance(summary, dict):
        return "unknown"
    errors = summary.get("errors")
    errors_valid = isinstance(errors, list) and all(isinstance(item, str) for item in errors)
    if errors_valid and errors:
        return "true"
    watch = summary.get("optimization_watch")
    if isinstance(watch, dict) and type(watch.get("errors")) is int and watch["errors"] > 0:
        return "true"
    action = summary.get("action")
    if (not isinstance(action, str)
        or action not in {"quiet", "github_issue", "telegram", "runtime_digest"}
        or type(summary.get("telegram_sent")) is not bool
        or not errors_valid
        or not isinstance(summary.get("skipped"), list)
        or "github_issue" not in summary
        or (summary["github_issue"] is not None and not isinstance(summary["github_issue"], str))):
        return "unknown"
    if action == "runtime_digest":
        if not isinstance(summary.get("business_date"), str) or not isinstance(summary.get("event_id"), str):
            return "unknown"
    else:
        if "optimization_watch" not in summary or type(summary.get("operational_fallback_sent")) is not bool:
            return "unknown"
        if watch is not None and (not isinstance(watch, dict) or type(watch.get("errors")) is not int or watch["errors"] != 0):
            return "unknown"
    return "false"


def _dispatch_reason(summary: object, failed: str) -> str:
    if failed == "false":
        return "dispatch_completed"
    if failed == "true" and isinstance(summary, dict):
        errors = summary.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            return first if isinstance(first, str) and first in _DISPATCH_REASONS else "unknown"
        watch = summary.get("optimization_watch")
        if isinstance(watch, dict) and type(watch.get("errors")) is int and watch["errors"] > 0:
            return "optimization_record_failed"
    return "unknown"


def _reject_runtime(reason: str) -> int:
    stage = ("input_read" if isinstance(reason, str) and reason in _RUNTIME_READ_REASONS else
             "binding_validation" if isinstance(reason, str) and reason in _RUNTIME_BINDING_REASONS else "input_validation")
    _emit_result(branch="runtime", stage=stage, reason=reason, action="none", exit_code=2)
    print(json.dumps({
        "ok": False,
        "error": "runtime_projection_rejected",
        "reason": reason,
    }, ensure_ascii=False))
    return 2


_GCS_TIMEOUT_SECONDS = 20
_GCS_OBJECT_LIMIT = 1024 * 1024
_GCS_FORBIDDEN = ("*", "?", "#", "@", "\\", " ", "\n", "\r", "\t")


def _paper_scope_problem(keys: list[str] | None) -> str | None:
    if not keys:
        return "missing_expected_target"
    seen: set[str] = set()
    for key in keys:
        if not _producer_target_key(key):
            return "invalid_expected_target"
        if key in seen:
            return "duplicate_expected_target"
        seen.add(key)
        if key.rsplit("|", 1)[-1] != "paper":
            return "expected_scope_not_paper"
    return None


def _runtime_object_problem(uri: str, day: str) -> str | None:
    if (
        not isinstance(uri, str)
        or not uri.startswith("gs://")
        or uri.endswith("/")
        or any(item in uri for item in _GCS_FORBIDDEN)
        or ".." in uri
    ):
        return "invalid_runtime_object"
    rest = uri[5:]
    if not rest or rest.startswith("/") or "//" in rest or "/" not in rest:
        return "invalid_runtime_object"
    bucket, _, object_name = rest.partition("/")
    if not bucket or not object_name:
        return "invalid_runtime_object"
    parts = object_name.split("/")
    if len(parts) < 4 or parts[-4:-1] != ["runtime_daily", "longbridge", "paper"]:
        return "invalid_runtime_object"
    filename = parts[-1]
    if not filename.endswith(".json"):
        return "invalid_runtime_object"
    object_day = filename[: -len(".json")]
    if _canonical_iso_day(object_day) is None:
        return "invalid_runtime_object"
    if object_day != day:
        return "runtime_object_day_mismatch"
    return None


def _stop_gcs_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _read_ready(fd: int, deadline: float) -> bytes | None:
    """Read bytes already available. None means the deadline passed."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    ready, _, _ = select.select([fd], [], [], remaining)
    if not ready:
        return None
    return os.read(fd, 65536)


def _read_gcs_object(uri: str) -> tuple[bytes | None, str | None]:
    """Read one object with gcloud. stderr is discarded and never returned."""
    try:
        proc = subprocess.Popen(
            ["gcloud", "storage", "cat", "--", uri],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError:
        return None, "runtime_projection_unreadable"
    if proc.stdout is None or proc.stderr is None:
        _stop_gcs_process(proc)
        return None, "runtime_projection_unreadable"
    stdout_fd = proc.stdout.fileno()
    stderr_fd = proc.stderr.fileno()
    deadline = time.monotonic() + _GCS_TIMEOUT_SECONDS
    chunks: list[bytes] = []
    total = 0
    stderr_open = True
    try:
        while total <= _GCS_OBJECT_LIMIT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_gcs_process(proc)
                return None, "runtime_projection_timeout"
            watch = [stdout_fd]
            if stderr_open:
                watch.append(stderr_fd)
            ready, _, _ = select.select(watch, [], [], remaining)
            if not ready:
                _stop_gcs_process(proc)
                return None, "runtime_projection_timeout"
            if stderr_fd in ready:
                err = os.read(stderr_fd, 4096)
                if not err:
                    stderr_open = False
            if stdout_fd not in ready:
                continue
            block = os.read(stdout_fd, min(65536, _GCS_OBJECT_LIMIT + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
        if total > _GCS_OBJECT_LIMIT:
            _stop_gcs_process(proc)
            return None, "runtime_projection_too_large"
        code = None
        while code is None:
            if not stderr_open:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop_gcs_process(proc)
                    return None, "runtime_projection_timeout"
                code = proc.poll()
                if code is None:
                    time.sleep(min(0.05, remaining))
                continue
            err = _read_ready(stderr_fd, deadline)
            if err is None:
                _stop_gcs_process(proc)
                return None, "runtime_projection_timeout"
            if not err:
                stderr_open = False
            code = proc.poll()
    except OSError:
        _stop_gcs_process(proc)
        return None, "runtime_projection_unreadable"
    if code != 0:
        _stop_gcs_process(proc)
        return None, "runtime_projection_unreadable"
    return b"".join(chunks), None


def _consume_runtime_payload(
    args: argparse.Namespace,
    payload: dict,
    *,
    scope_required: bool,
) -> int:
    prepared = prepare_runtime_digest(payload)
    if not prepared.get("ok"):
        _emit_result(branch="runtime", stage="input_validation", reason=prepared.get("reason"),
                     action="none", exit_code=2)
        print(json.dumps({
            "ok": False,
            "error": "runtime_projection_rejected",
            "reason": prepared.get("reason"),
        }, ensure_ascii=False))
        return 2
    dispatching = bool(args.dispatch) or scope_required
    day_problem = _runtime_day_problem(
        str(args.day or ""),
        prepared.get("business_date"),
        required=dispatching,
    )
    if day_problem is not None:
        return _reject_runtime(day_problem)
    target_problem = _runtime_target_problem(
        args.expected_target_key,
        prepared.get("target_scope"),
        required=dispatching,
    )
    if target_problem is not None:
        return _reject_runtime(target_problem)
    body = {
        "ok": True,
        "kind": "runtime_digest",
        "platform": prepared["platform"],
        "business_date": prepared["business_date"],
        "timezone": prepared["timezone"],
        "completeness": prepared["completeness"],
        "event_id": prepared["event_id"],
        "text": prepared["text"],
        "sendable": prepared["sendable"],
        "accounts": prepared["accounts"],
    }
    if args.dispatch or args.send_dry_run or args.dry_run:
        body["dispatch"] = dispatch_runtime_digest(
            payload,
            dry_run=bool(args.dry_run or args.send_dry_run or not args.dispatch),
            send_dry_run=bool(args.send_dry_run),
        )
    print(json.dumps(body, ensure_ascii=False, indent=2))
    dispatch = body.get("dispatch")
    exit_code = 0 if dispatch is None or not _dispatch_failed(dispatch) else 2
    requested = bool(args.dispatch or args.send_dry_run or args.dry_run)
    failed = _dispatch_failure_evidence(dispatch)
    _emit_result(branch="runtime", stage="dispatch" if requested else "routing",
                 reason=_dispatch_reason(dispatch, failed) if requested else "dispatch_not_requested",
                 action="runtime_digest", dispatch_failed=failed, exit_code=exit_code)
    return exit_code


def _consume_runtime_projection(args: argparse.Namespace) -> int:
    path = Path(args.runtime_projection)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _emit_result(branch="runtime", stage="input_read", reason="runtime_projection_unreadable",
                     action="none", exit_code=2)
        print(json.dumps({"ok": False, "error": "runtime_projection_unreadable"}, ensure_ascii=False))
        return 2
    if not isinstance(payload, dict):
        return _reject_runtime("malformed")
    return _consume_runtime_payload(args, payload, scope_required=False)


def _consume_runtime_gcs(args: argparse.Namespace) -> int:
    day = str(args.day or "")
    if not day:
        return _reject_runtime("missing_dispatch_day")
    if _canonical_iso_day(day) is None:
        return _reject_runtime("invalid_business_day")
    key_problem = _paper_scope_problem(args.expected_target_key)
    if key_problem is not None:
        return _reject_runtime(key_problem)
    object_problem = _runtime_object_problem(str(args.runtime_projection_gcs), day)
    if object_problem is not None:
        return _reject_runtime(object_problem)
    raw, read_problem = _read_gcs_object(str(args.runtime_projection_gcs))
    if read_problem is not None or raw is None:
        return _reject_runtime(read_problem or "runtime_projection_unreadable")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _reject_runtime("runtime_projection_unreadable")
    if not isinstance(payload, dict):
        return _reject_runtime("malformed")
    return _consume_runtime_payload(args, payload, scope_required=True)


def _dispatch_failed(summary: object) -> bool:
    if not isinstance(summary, dict):
        return True
    if summary.get("errors"):
        return True
    optimization_watch = summary.get("optimization_watch")
    return isinstance(optimization_watch, dict) and int(
        optimization_watch.get("errors") or 0
    ) > 0


def _failure_category(error: Exception) -> str:
    """Fixed interface-error categories; never expose exception text or names."""
    return {
        OSError: "io_error", FileNotFoundError: "io_error", PermissionError: "io_error",
        IsADirectoryError: "io_error", NotADirectoryError: "io_error", TimeoutError: "io_error",
        ValueError: "value_error", json.JSONDecodeError: "value_error",
        TypeError: "type_error", KeyError: "key_error", OverflowError: "overflow_error",
    }.get(type(error), "unknown_error")


# These reasons identify explicit returns in summarize_briefing, not root causes.
_SUMMARY_FAILURE_DETAILS = {
    "briefing_input_unavailable": ("briefing_input_validation", "input_unavailable"),
    "briefing_source_time_unavailable": ("briefing_input_validation", "input_unavailable"),
    "github_oidc_required": ("summary_configuration", "configuration_unavailable"),
    "ai_gateway_not_configured": ("summary_configuration", "configuration_unavailable"),
    "source_repository_required": ("summary_configuration", "configuration_unavailable"),
    "summary_execution_unavailable": ("summary_execution", "outcome_unknown"),
    "summary_result_unavailable": ("summary_result_processing", "result_unavailable"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consume quant-monitor daily briefing reports.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--report-dir",
        help="Directory containing domain JSON files (e.g. data/daily-reports/2026-07-08)",
    )
    source.add_argument(
        "--runtime-projection",
        help="Offline LongBridge daily runtime projection JSON. Default is preview only.",
    )
    source.add_argument(
        "--runtime-projection-gcs",
        help=(
            "One gs:// runtime_daily/longbridge/paper/YYYY-MM-DD.json object. "
            "Requires --day and --expected-target-key. Preview unless --dispatch."
        ),
    )
    parser.add_argument("--day", default="", help="Report day label (defaults to directory name)")
    parser.add_argument(
        "--expected-target-key",
        action="append",
        default=None,
        help="Exact deployed runtime target key. Repeat once per target. Required with --dispatch.",
    )
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="Send Telegram / GitHub notifications per briefing severity",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print dispatch actions without sending")
    parser.add_argument(
        "--send-dry-run",
        action="store_true",
        help=(
            "Configuration-check dispatch: report non-secret sender prerequisites and "
            "redacted preview; never send Telegram, create GitHub issues, or write ledger"
        ),
    )
    parser.add_argument("--ai-summary", action="store_true", help="Add an advisory subscription AI summary (requires GitHub OIDC)")
    parser.add_argument("--summary-only", action="store_true", help="Export only the advisory summary; never dispatch alerts or run dual review")
    parser.add_argument(
        "--dual-review",
        action="store_true",
        help="Request a three-role advisory review of validated briefing evidence",
    )
    args = parser.parse_args(argv)
    if args.summary_only and (args.dispatch or args.send_dry_run or args.dual_review):
        parser.error("--summary-only cannot dispatch alerts or run dual review")
    if args.send_dry_run and (args.ai_summary or args.dual_review):
        parser.error("--send-dry-run cannot run AI summary or dual review")
    if (args.runtime_projection or args.runtime_projection_gcs) and (
        args.ai_summary or args.dual_review or args.summary_only
    ):
        parser.error("--runtime-projection cannot run AI summary or dual review")

    if args.runtime_projection:
        return _consume_runtime_projection(args)
    if args.runtime_projection_gcs:
        return _consume_runtime_gcs(args)

    report_dir = Path(args.report_dir)
    if not report_dir.is_dir():
        if args.summary_only:
            print(json.dumps({"day": args.day, "ai_summary": {
                "status": "unavailable", "reason": "briefing_input_unavailable", "advisory_only": True,
                "failure_stage": "briefing_directory_check", "failure_category": "input_unavailable",
            }}))
            return 3
        _emit_result(branch="domain", stage="input_validation", reason="report_dir_not_found",
                     action="none", exit_code=1)
        print(json.dumps({"ok": False, "error": f"report_dir_not_found: {report_dir}"}))
        return 1

    if args.summary_only:
        failure_stage = "briefing_input_processing"
        try:
            result = consume_briefing_dir(report_dir, day=args.day)
            failure_stage = "summary_processing"
            summary = summarize_briefing(result, dry_run=args.dry_run)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            summary = {
                "status": "unavailable", "reason": "briefing_input_unavailable", "advisory_only": True,
                "failure_stage": failure_stage, "failure_category": _failure_category(exc),
            }
        else:
            if isinstance(summary, dict) and summary.get("status") == "unavailable":
                reason = summary.get("reason")
                details = _SUMMARY_FAILURE_DETAILS.get(reason) if isinstance(reason, str) else None
                stage, category = details or ("summary_processing", "unknown_error")
                summary = {**summary, "failure_stage": stage, "failure_category": category}
        print(json.dumps({"day": args.day, "ai_summary": summary}, ensure_ascii=False, indent=2))
        # deferred = trusted capacity/scheduling decision with artifact; do not
        # fail the OIDC Actions job (red CI) the way unavailable/input errors do.
        return 0 if summary["status"] in {"available", "dry_run", "deferred"} else 3
    result = consume_briefing_dir(report_dir, day=args.day)
    payload: dict = result.to_dict()
    should_dispatch = bool(args.dispatch or args.send_dry_run)
    if should_dispatch:
        payload["dispatch"] = dispatch_briefing_result(
            result,
            dry_run=bool(args.dry_run or args.send_dry_run),
            send_dry_run=bool(args.send_dry_run),
        )
    if args.ai_summary:
        payload["ai_summary"] = summarize_briefing(result, dry_run=args.dry_run)
    if args.dual_review:
        from quant_platform_kit.strategy_lifecycle.task_review import review_material
        import hashlib
        material = {"summary_reports": result.summary_reports, "day": args.day, "advisory_only": True}
        identity = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        try:
            review = review_material(material, operation_id="briefing-review:" + identity,
                                     required_roles=("primary", "secondary", "verification"))
        except Exception:
            review = {"status": "unavailable", "advisory_only": True}
        payload["dual_review"] = {"schema":"quant_monitor.briefing_review.v2", "review":review,
                                  "disagreements": review.get("status") != "completed" or review.get("outcome") != "agree_approve",
                                  "advisory_only":True, "execution_authority_granted":False}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.send_dry_run:
        exit_code = 0 if not _dispatch_failed(payload.get("dispatch")) else 2
    elif result.action.value == "quiet":
        exit_code = 0
    elif (
        result.action.value == "github_issue"
        and should_dispatch
        and not _dispatch_failed(payload.get("dispatch"))
    ):
        exit_code = 0
    else:
        exit_code = 2
    disagreement = bool(args.dual_review and payload.get("dual_review", {}).get("disagreements"))
    if disagreement:
        exit_code = 2
    failed = _dispatch_failure_evidence(payload.get("dispatch"))
    if disagreement:
        reason = "dual_review_disagreement"
    elif should_dispatch and failed != "false":
        reason = _dispatch_reason(payload.get("dispatch"), failed)
    elif result.action.value == "telegram" and not args.send_dry_run:
        reason = "telegram_attention"
    elif result.action.value == "quiet":
        reason = "quiet"
    elif not should_dispatch:
        reason = "dispatch_not_requested"
    elif result.action.value == "github_issue":
        reason = "github_issue_recorded"
    else:
        reason = "dispatch_completed"
    _emit_result(branch="domain", stage="dispatch" if failed == "true" else "routing",
                 reason=reason, action=result.action.value, dispatch_failed=failed, exit_code=exit_code)
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
