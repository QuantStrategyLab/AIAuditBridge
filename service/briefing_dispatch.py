"""Dispatch briefing consumption results to Telegram / GitHub."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from scripts.run_strategy_optimization_watcher import dispatch_strategy_watch_findings
from service.briefing_consumer import BriefingAction, BriefingConsumptionResult, BriefingFinding
from service.runtime_digest import prepare_runtime_digest
from service.strategy_watch import StrategyWatchFinding, build_strategy_monitoring_finding

_REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*/"
    r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*"
)


def _telegram_token() -> str:
    for key in (
        "TELEGRAM_TOKEN",
        "TG_TOKEN",
        "STRATEGY_PLUGIN_ALERT_TELEGRAM_BOT_TOKEN",
    ):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _telegram_chat_ids() -> tuple[str, ...]:
    for key in (
        "GLOBAL_TELEGRAM_CHAT_ID",
        "QSL_GLOBAL_TELEGRAM_CHAT_ID",
        "STRATEGY_PLUGIN_ALERT_TELEGRAM_CHAT_IDS",
    ):
        raw = os.environ.get(key)
        if not raw:
            continue
        ids = [
            part.strip()
            for part in str(raw).replace(";", ",").split(",")
            if part.strip()
        ]
        if ids:
            return tuple(ids)
    return ()


def _github_issue_target() -> str:
    return str(
        os.environ.get("QSL_GITHUB_REPO")
        or os.environ.get("GITHUB_REPOSITORY")
        or "QuantStrategyLab/QuantStrategyLab"
    ).strip()


def sender_prerequisites() -> dict[str, bool]:
    """Non-secret presence checks for already-configured sender env/tools."""
    return {
        "telegram_token_present": bool(_telegram_token()),
        "telegram_chat_ids_present": bool(_telegram_chat_ids()),
        "github_issue_target_valid": _REPOSITORY_RE.fullmatch(_github_issue_target()) is not None,
        "gh_executable_present": bool(shutil_which("gh")),
    }


def _redact_send_dry_run_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Replace previews with safe presence flags; never emit secrets or bodies."""
    redacted = {
        "action": summary.get("action"),
        "send_dry_run": True,
        "sender_prerequisites": dict(summary.get("sender_prerequisites") or {}),
        "telegram_sent": False,
        "github_issue": None,
        "optimization_watch": None,
        "operational_fallback_sent": False,
        "errors": list(summary.get("errors") or []),
        "skipped": list(summary.get("skipped") or []),
        "telegram_dry_run": {"present": False},
        "github_dry_run": {"present": False},
    }
    if "telegram_dry_run" in summary:
        redacted["telegram_dry_run"] = {
            "present": True,
            "safe_summary": "telegram_preview_available",
        }
    if "github_dry_run" in summary:
        redacted["github_dry_run"] = {
            "present": True,
            "safe_summary": "github_issue_preview_available",
        }
    if summary.get("optimization_watch") is not None:
        watch = summary["optimization_watch"]
        if isinstance(watch, dict):
            redacted["optimization_watch"] = {
                "status": str(watch.get("status") or "dry_run"),
                "errors": int(watch.get("errors") or 0),
            }
        else:
            redacted["optimization_watch"] = {"status": "dry_run", "errors": 0}
    return redacted


def _format_telegram_body(result: BriefingConsumptionResult) -> str:
    lines = [f"🚨 量化哨兵 daily briefing ({result.day})", ""]
    for finding in result.findings:
        if finding.level != BriefingAction.TELEGRAM:
            continue
        prefix = finding.strategy_profile or finding.domain or finding.source
        lines.append(f"• {prefix}: {finding.reason}")
    if len(lines) == 2:
        for finding in result.findings:
            prefix = finding.strategy_profile or finding.domain or finding.source
            lines.append(f"• [{finding.level.value}] {prefix}: {finding.reason}")
    return "\n".join(lines)


def _format_github_body(
    result: BriefingConsumptionResult,
    findings: list[BriefingFinding] | None = None,
) -> str:
    lines = [
        f"## Daily briefing alerts ({result.day})",
        "",
        f"Report dir: `{result.report_dir}`",
        "",
    ]
    for finding in result.findings if findings is None else findings:
        if finding.level != BriefingAction.GITHUB_ISSUE:
            continue
        lines.append(
            f"- **{finding.strategy_profile or finding.domain or finding.source}**: {finding.reason}"
        )
    return "\n".join(lines)


def _telegram_body_status(raw: bytes) -> str:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unknown"
    if isinstance(body, dict) and body.get("ok") is True:
        return "sent"
    if isinstance(body, dict) and body.get("ok") is False:
        return "failed"
    return "unknown"


def telegram_target_outcome(*, text: str, token: str, chat_id: str) -> str:
    """Classify one Telegram send without collapsing other targets into one bool."""
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    request = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status_value = getattr(response, "status", None)
            if status_value is None and callable(getattr(response, "getcode", None)):
                status_value = response.getcode()
            status = 200 if status_value is None else int(status_value)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() if callable(getattr(exc, "read", None)) else b""
        if _telegram_body_status(raw) == "failed" or 400 <= int(exc.code) < 500:
            return "failed"
        return "unknown"
    except TimeoutError:
        return "unknown"
    except urllib.error.URLError:
        return "unknown"
    except Exception:
        return "unknown"
    if status < 200 or status >= 300:
        return "unknown"
    return _telegram_body_status(raw)


def send_telegram_alert(*, text: str, token: str, chat_ids: tuple[str, ...]) -> bool:
    if not token or not chat_ids:
        return False
    outcomes = [
        telegram_target_outcome(text=text, token=token, chat_id=chat_id)
        for chat_id in chat_ids
    ]
    return all(outcome == "sent" for outcome in outcomes)


_HEALTH_CYCLE = None


def _health_cycle_module():
    global _HEALTH_CYCLE
    if _HEALTH_CYCLE is not None:
        return _HEALTH_CYCLE
    path = Path(__file__).resolve().parents[1] / "ops" / "quant-monitor" / "scripts" / "health_cycle.py"
    spec = importlib.util.spec_from_file_location("quant_monitor_health_cycle", path)
    if spec is None or spec.loader is None:
        raise OSError("alert_state_unreadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _HEALTH_CYCLE = module
    return module


def _monitor_state_root() -> Path | None:
    raw = str(os.environ.get("QUANT_MONITOR_ROOT") or "").strip()
    if not raw:
        return None
    root = Path(raw)
    if not root.is_dir():
        return None
    return root


def _briefing_delivery_event_id(result: BriefingConsumptionResult, *, record_failed: bool) -> str:
    parts = [result.day, "record_failed" if record_failed else result.action.value]
    for finding in result.findings:
        if finding.level != BriefingAction.TELEGRAM and not record_failed:
            continue
        parts.append("\n".join((
            finding.source,
            finding.level.value,
            finding.kind,
            finding.strategy_profile,
            finding.domain,
            finding.reason,
        )))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def create_github_issue(*, title: str, body: str, labels: tuple[str, ...] = ()) -> str | None:
    repo = _github_issue_target()
    if _REPOSITORY_RE.fullmatch(repo) is None:
        return None
    if not shutil_which("gh"):
        return None
    base_cmd = [
        "gh",
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        body,
    ]
    cmd = list(base_cmd)
    for label in labels:
        cmd.extend(["--label", label])
    try:
        output = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
        return output
    except subprocess.CalledProcessError as exc:
        if labels:
            print(
                "::warning title=GitHub issue labels unavailable::"
                "retrying durable issue creation without labels"
            )
            try:
                return subprocess.check_output(
                    base_cmd,
                    text=True,
                    stderr=subprocess.STDOUT,
                ).strip()
            except (OSError, subprocess.CalledProcessError) as retry_exc:
                detail = str(getattr(retry_exc, "output", "") or retry_exc).strip()
                print(f"::warning title=GitHub issue creation failed::{detail}")
                return None
        detail = str(exc.output or exc).strip()
        print(f"::warning title=GitHub issue creation failed::{detail}")
        return None
    except OSError as exc:
        print(f"::warning title=GitHub issue creation failed::{exc}")
        return None


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _strategy_monitoring_findings(
    result: BriefingConsumptionResult,
) -> list[StrategyWatchFinding]:
    findings: list[StrategyWatchFinding] = []
    for finding in result.findings:
        if (
            finding.level != BriefingAction.GITHUB_ISSUE
            or finding.kind != "strategy_monitoring"
            or not finding.strategy_profile
        ):
            continue
        findings.append(
            build_strategy_monitoring_finding(
                domain=finding.domain,
                profile=finding.strategy_profile,
                severity=finding.severity,
                metrics=finding.metrics,
                signals=finding.signals or [{"metric": "briefing", "reason": finding.reason}],
                source=f"quant-monitor/daily-briefing/{finding.source}",
                generated_at=str(finding.metrics.get("as_of") or result.day),
            )
        )
    return findings


def dispatch_briefing_result(
    result: BriefingConsumptionResult,
    *,
    dry_run: bool = False,
    send_dry_run: bool = False,
) -> dict[str, Any]:
    """Execute notification side-effects for a briefing consumption result.

    When ``send_dry_run`` is true, reuse dry-run preview semantics, report
    non-secret sender prerequisite booleans, and never call Telegram HTTP,
    ``gh issue create``, or external write paths.
    """
    if send_dry_run:
        dry_run = True

    summary: dict[str, Any] = {
        "action": result.action.value,
        "telegram_sent": False,
        "github_issue": None,
        "optimization_watch": None,
        "operational_fallback_sent": False,
        "errors": [],
        "skipped": [],
    }

    if result.action == BriefingAction.QUIET:
        summary["skipped"].append("quiet")
        if send_dry_run:
            summary["sender_prerequisites"] = sender_prerequisites()
            return _redact_send_dry_run_summary(summary)
        return summary

    needs_github_issue = False
    if result.action in {BriefingAction.GITHUB_ISSUE, BriefingAction.TELEGRAM}:
        try:
            optimization_findings = _strategy_monitoring_findings(result)
            if optimization_findings:
                summary["optimization_watch"] = dispatch_strategy_watch_findings(
                    optimization_findings,
                    dry_run=dry_run,
                    comment_existing=False,
                )
                if int(summary["optimization_watch"].get("errors") or 0):
                    summary["errors"].append("optimization_record_failed")
        except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
            summary["optimization_watch"] = {
                "status": "error",
                "errors": 1,
                "error_type": type(exc).__name__,
            }
            summary["errors"].append("optimization_record_failed")
        github_findings = [
            finding
            for finding in result.findings
            if finding.level == BriefingAction.GITHUB_ISSUE
            and not (finding.kind == "strategy_monitoring" and finding.strategy_profile)
        ]
        if github_findings:
            needs_github_issue = True
            title = f"[briefing] {result.day} — {len(github_findings)} review-level alert(s)"
            body = _format_github_body(result, github_findings)
            if dry_run:
                summary["github_dry_run"] = {"title": title, "body": body}
            else:
                summary["github_issue"] = create_github_issue(title=title, body=body)
                if not summary["github_issue"]:
                    summary["errors"].append("github_issue_record_failed")

    record_failed = any(
        error in {"optimization_record_failed", "github_issue_record_failed"}
        for error in summary["errors"]
    )
    needs_telegram = result.action == BriefingAction.TELEGRAM or record_failed
    if needs_telegram:
        if result.action == BriefingAction.TELEGRAM:
            text = _format_telegram_body(result)
        else:
            text = (
                f"🚨 量化哨兵 operational ({result.day})\n\n"
                "• optimization-record delivery failure; manual review required"
            )
        if record_failed and result.action == BriefingAction.TELEGRAM:
            text += "\n• optimization-record delivery failure; manual review required"
        if dry_run:
            summary["telegram_dry_run"] = text
        else:
            token = _telegram_token()
            chat_ids = _telegram_chat_ids()
            if not token or not chat_ids:
                summary["skipped"].append("telegram_missing_env")
                summary["errors"].append("telegram_missing_env")
            else:
                root = _monitor_state_root()
                if root is None:
                    summary["telegram_sent"] = False
                    summary["errors"].append("alert_state_root_unavailable")
                else:
                    event_id = _briefing_delivery_event_id(result, record_failed=record_failed)

                    def send_one(chat_id: str) -> str:
                        return telegram_target_outcome(text=text, token=token, chat_id=chat_id)

                    delivery = _health_cycle_module().deliver_telegram_targets(
                        root, event_id, chat_ids, send_one,
                    )
                    summary["telegram_sent"] = bool(delivery["all_sent"])
                    summary["operational_fallback_sent"] = bool(record_failed and delivery["all_sent"])
                    for code in delivery["errors"]:
                        if code not in summary["errors"]:
                            summary["errors"].append(code)
                    if delivery["suppressed"]:
                        summary["skipped"].append("duplicate_delivered")

    if send_dry_run:
        prereqs = sender_prerequisites()
        summary["sender_prerequisites"] = prereqs
        if needs_telegram and not (
            prereqs["telegram_token_present"] and prereqs["telegram_chat_ids_present"]
        ):
            if "telegram_missing_env" not in summary["errors"]:
                summary["errors"].append("telegram_missing_env")
            if "telegram_missing_env" not in summary["skipped"]:
                summary["skipped"].append("telegram_missing_env")
        if needs_github_issue:
            if not prereqs["github_issue_target_valid"]:
                if "github_issue_target_invalid" not in summary["errors"]:
                    summary["errors"].append("github_issue_target_invalid")
            elif not prereqs["gh_executable_present"]:
                if "gh_executable_missing" not in summary["errors"]:
                    summary["errors"].append("gh_executable_missing")
        return _redact_send_dry_run_summary(summary)

    return summary


def dispatch_runtime_digest(
    projection: dict[str, Any],
    *,
    dry_run: bool = False,
    send_dry_run: bool = False,
) -> dict[str, Any]:
    """Send one daily runtime projection, or preview it without persistence.

    Health-cycle delivery state is reused. This does not confirm a health
    fingerprint, open a GitHub issue, or call a model.
    """
    if send_dry_run:
        dry_run = True
    prepared = prepare_runtime_digest(projection)
    summary: dict[str, Any] = {
        "action": "runtime_digest",
        "telegram_sent": False,
        "github_issue": None,
        "errors": [],
        "skipped": [],
        "business_date": prepared.get("business_date"),
        "event_id": prepared.get("event_id"),
    }
    if not prepared.get("ok"):
        summary["errors"].append(str(prepared.get("reason") or "runtime_projection_rejected"))
        return summary
    summary["telegram_preview"] = prepared["text"]
    if not prepared.get("sendable"):
        summary["errors"].append(str(prepared.get("reason") or "runtime_digest_too_long"))
        return summary
    if dry_run:
        summary["skipped"].append("dry_run")
        summary["telegram_dry_run"] = prepared["text"]
        return summary
    token = _telegram_token()
    chat_ids = _telegram_chat_ids()
    if not token or not chat_ids:
        summary["skipped"].append("telegram_missing_env")
        summary["errors"].append("telegram_missing_env")
        return summary
    root = _monitor_state_root()
    if root is None:
        summary["errors"].append("alert_state_root_unavailable")
        return summary

    def send_one(chat_id: str) -> str:
        return telegram_target_outcome(text=prepared["text"], token=token, chat_id=chat_id)

    delivery = _health_cycle_module().deliver_telegram_targets(
        root,
        str(prepared["event_id"]),
        chat_ids,
        send_one,
    )
    summary["telegram_sent"] = bool(delivery["all_sent"])
    for code in delivery["errors"]:
        if code not in summary["errors"]:
            summary["errors"].append(code)
    if delivery["suppressed"]:
        summary["skipped"].append("duplicate_delivered")
    return summary
