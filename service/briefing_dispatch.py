"""Dispatch briefing consumption results to Telegram / GitHub."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from typing import Any

from service.briefing_consumer import BriefingAction, BriefingConsumptionResult


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


def _format_github_body(result: BriefingConsumptionResult) -> str:
    lines = [
        f"## Daily briefing alerts ({result.day})",
        "",
        f"Report dir: `{result.report_dir}`",
        "",
    ]
    for finding in result.findings:
        if finding.level != BriefingAction.GITHUB_ISSUE:
            continue
        lines.append(
            f"- **{finding.strategy_profile or finding.domain or finding.source}**: {finding.reason}"
        )
    return "\n".join(lines)


def send_telegram_alert(*, text: str, token: str, chat_ids: tuple[str, ...]) -> bool:
    if not token or not chat_ids:
        return False
    ok = True
    for chat_id in chat_ids:
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
                body = json.loads(response.read().decode("utf-8"))
            ok = ok and bool(body.get("ok"))
        except Exception:
            ok = False
    return ok


def create_github_issue(*, title: str, body: str, labels: tuple[str, ...] = ("briefing", "monitoring")) -> str | None:
    repo = str(
        os.environ.get("QSL_GITHUB_REPO")
        or os.environ.get("GITHUB_REPOSITORY")
        or "QuantStrategyLab/QuantStrategyLab"
    ).strip()
    if not shutil_which("gh"):
        return None
    cmd = [
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
    for label in labels:
        cmd.extend(["--label", label])
    try:
        output = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
        return output
    except Exception:
        return None


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def dispatch_briefing_result(
    result: BriefingConsumptionResult,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute notification side-effects for a briefing consumption result."""
    summary: dict[str, Any] = {
        "action": result.action.value,
        "telegram_sent": False,
        "github_issue": None,
        "skipped": [],
    }

    if result.action == BriefingAction.QUIET:
        summary["skipped"].append("quiet")
        return summary

    if result.action == BriefingAction.TELEGRAM:
        token = _telegram_token()
        chat_ids = _telegram_chat_ids()
        text = _format_telegram_body(result)
        if dry_run:
            summary["telegram_dry_run"] = text
            return summary
        if token and chat_ids:
            summary["telegram_sent"] = send_telegram_alert(text=text, token=token, chat_ids=chat_ids)
        else:
            summary["skipped"].append("telegram_missing_env")

    if result.action in {BriefingAction.GITHUB_ISSUE, BriefingAction.TELEGRAM}:
        github_findings = [f for f in result.findings if f.level == BriefingAction.GITHUB_ISSUE]
        if github_findings:
            title = f"[briefing] {result.day} — {len(github_findings)} review-level alert(s)"
            body = _format_github_body(result)
            if dry_run:
                summary["github_dry_run"] = {"title": title, "body": body}
            else:
                summary["github_issue"] = create_github_issue(title=title, body=body)

    return summary
