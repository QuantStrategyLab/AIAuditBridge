"""Dispatch dual-review outcomes to GitHub (roadmap task 11)."""

from __future__ import annotations

import json
import os
from typing import Any

from service.dual_review import VERDICT_DISAGREEMENT, VERDICT_UNAVAILABLE
from service.dual_review_orchestrator import DualReviewResult
from service.briefing_dispatch import create_github_issue, shutil_which


def _github_assignee() -> str:
    for key in ("DUAL_REVIEW_GITHUB_ASSIGNEE", "QSL_DUAL_REVIEW_ASSIGNEE", "GITHUB_ACTOR"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _format_disagreement_body(result: DualReviewResult) -> str:
    assignee = _github_assignee()
    lines = [
        "## Dual-review disagreement — operator decision required",
        "",
        f"- **Trigger**: `{result.trigger.value}`",
        f"- **Strategy**: `{result.strategy_profile}`",
        f"- **Outcome**: `{result.outcome}`",
        f"- **Reason**: {result.reason}",
        "",
    ]
    if assignee:
        lines.append(f"@{assignee} please arbitrate.")
        lines.append("")
    lines.extend(
        [
            "### Primary review",
            "```json",
            json.dumps(result.primary_review, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    if result.secondary_review is not None:
        secondary = result.secondary_review
        if isinstance(secondary, dict) and "gpt" in secondary and "claude" in secondary:
            lines.extend(
                [
                    "### Secondary review — GPT",
                    "```json",
                    json.dumps(secondary.get("gpt"), ensure_ascii=False, indent=2),
                    "```",
                    "",
                    "### Secondary review — Claude",
                    "```json",
                    json.dumps(secondary.get("claude"), ensure_ascii=False, indent=2),
                    "```",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "### Secondary review",
                    "```json",
                    json.dumps(secondary, ensure_ascii=False, indent=2),
                    "```",
                    "",
                ]
            )
    if result.comparison is not None:
        lines.extend(
            [
                "### Comparison",
                "```json",
                json.dumps(result.comparison, ensure_ascii=False, indent=2),
                "```",
            ]
        )
    return "\n".join(lines)


def dispatch_dual_review_result(
    result: DualReviewResult,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Notify operators only when dual reviews disagree."""
    summary: dict[str, Any] = {
        "trigger": result.trigger.value,
        "strategy_profile": result.strategy_profile,
        "outcome": result.outcome,
        "escalated": result.escalated,
        "github_issue": None,
        "skipped": [],
    }

    if result.outcome == VERDICT_UNAVAILABLE:
        summary["skipped"].append("reviewers_unavailable")
        return summary
    if result.outcome != VERDICT_DISAGREEMENT:
        summary["skipped"].append("no_disagreement")
        return summary

    title = (
        f"[dual-review] {result.strategy_profile} — "
        f"{result.trigger.value} disagreement"
    )
    body = _format_disagreement_body(result)
    labels = ("dual-review", result.trigger.value, "needs-human")

    if dry_run:
        summary["github_dry_run"] = {"title": title, "body": body, "labels": list(labels)}
        return summary

    if not shutil_which("gh"):
        summary["skipped"].append("gh_cli_missing")
        return summary

    assignee = _github_assignee()
    issue_url = create_github_issue(title=title, body=body, labels=labels)
    if issue_url and assignee:
        _try_assign_issue(issue_url, assignee)
    summary["github_issue"] = issue_url
    if not issue_url:
        summary["skipped"].append("github_issue_failed")
    return summary


def _try_assign_issue(issue_url: str, assignee: str) -> None:
    import subprocess

    repo = str(os.environ.get("QSL_GITHUB_REPO") or "QuantStrategyLab/QuantStrategyLab").strip()
    issue_number = issue_url.rstrip("/").split("/")[-1]
    if not issue_number.isdigit():
        return
    try:
        subprocess.run(
            ["gh", "issue", "edit", issue_number, "--repo", repo, "--add-assignee", assignee],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return
