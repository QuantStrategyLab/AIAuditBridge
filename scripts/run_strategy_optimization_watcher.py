#!/usr/bin/env python3
"""Run the issue-only strategy optimization watcher."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.strategy_watch import (  # noqa: E402
    StrategyWatchFinding,
    build_research_input_unavailable_finding,
    evaluate_strategy_watch,
    finding_to_automation_task,
    issue_for_task,
    research_task_context_available,
    research_task_source_snapshot,
    watcher_issue_key,
)

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean value must be one of true/false/yes/no/on/off/1/0")


def resolve_input_path(
    *,
    input_path: str = "",
    source_root: str = "",
    metrics_path: str = "",
) -> Path | None:
    if source_root and metrics_path:
        normalized = PurePosixPath(metrics_path.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("metrics_path must be a relative path inside the source checkout")
        root = Path(source_root).resolve()
        candidate = (root / Path(*normalized.parts)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("metrics_path resolves outside the source checkout") from exc
        return candidate
    if not input_path:
        return None
    candidate = Path(input_path).resolve()
    if source_root:
        root = Path(source_root).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("input path resolves outside the source checkout") from exc
    return candidate


def load_payload(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("strategy watch input must be a JSON object")
    return payload


def list_open_issue_urls(repo: str) -> dict[str, str]:
    if not REPO_RE.fullmatch(repo):
        raise ValueError("repository must be in owner/name form")
    page = 1
    open_issues: dict[str, str] = {}
    while True:
        result = subprocess.run(
            ["gh", "api", "--method", "GET", f"/repos/{repo}/issues", "-f", "state=open", "-f", "per_page=100", "-f", f"page={page}"],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            issues = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("failed to parse open issue list") from exc
        if not isinstance(issues, list) or not issues:
            return open_issues
        for issue in issues:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            body = str(issue.get("body") or "")
            match = re.search(r"<!--\s*strategy-optimization-watcher:([A-Za-z0-9_-]{8,64})\s*-->", body)
            if match and match.group(1) not in open_issues:
                open_issues[match.group(1)] = str(issue.get("html_url") or issue.get("url") or "")
        if len(issues) < 100:
            return open_issues
        page += 1


def find_existing_open_issue(repo: str, issue_key: str) -> str:
    return list_open_issue_urls(repo).get(issue_key, "")


def task_public_summary(task: Any) -> dict[str, Any]:
    payload = task.to_dict()
    trigger = payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {}
    proposed_action = payload.get("proposed_action") if isinstance(payload.get("proposed_action"), dict) else {}
    gate_decision = payload.get("gate_decision") if isinstance(payload.get("gate_decision"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        "trigger": {
            "source": trigger.get("source", ""),
            "kind": trigger.get("kind", ""),
            "severity": trigger.get("severity", ""),
            "subject": trigger.get("subject", ""),
            "reason": trigger.get("reason", ""),
            "signals": [
                {"reason": str(item)}
                for item in trigger.get("evidence", [])
                if isinstance(item, str)
            ],
        },
        "proposed_action": {
            "action": proposed_action.get("action", ""),
            "lane": proposed_action.get("lane", ""),
            "target": proposed_action.get("target", ""),
            "requires_human_review": proposed_action.get("requires_human_review", True),
        },
        "gate_decision": {
            "allowed": gate_decision.get("allowed", False),
            "human_review_required": gate_decision.get("human_review_required", True),
        },
        "finding_type": metadata.get("finding_type", "metric_degradation"),
        "event_key": metadata.get("event_key", ""),
        "status": payload.get("status", ""),
    }


def comment_github_issue(repo: str, issue_url: str, body: str) -> str:
    if not REPO_RE.fullmatch(repo):
        raise ValueError("repository must be in owner/name form")
    result = subprocess.run(
        ["gh", "issue", "comment", issue_url, "--repo", repo, "--body", body],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_github_issue(repo: str, title: str, body: str) -> str:
    if not REPO_RE.fullmatch(repo):
        raise ValueError("repository must be in owner/name form")
    result = subprocess.run(
        ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _payload_for_source_repo(payload: dict[str, Any], source_repo: str) -> dict[str, Any]:
    if not source_repo:
        return payload
    normalized = copy.deepcopy(payload)
    for key in ("repo", "repository"):
        embedded = str(normalized.get(key) or "").strip()
        if embedded and embedded != source_repo:
            raise ValueError("metrics payload repository does not match validated source repository")
    normalized["repo"] = source_repo
    raw_snapshots = normalized.get("snapshots")
    if isinstance(raw_snapshots, list):
        clean_snapshots: list[Any] = []
        for item in raw_snapshots:
            if not isinstance(item, dict):
                clean_snapshots.append(item)
                continue
            snapshot = dict(item)
            for key in ("repo", "repository"):
                embedded = str(snapshot.get(key) or "").strip()
                if embedded and embedded != source_repo:
                    raise ValueError("snapshot repository does not match validated source repository")
            snapshot["repo"] = source_repo
            clean_snapshots.append(snapshot)
        normalized["snapshots"] = clean_snapshots
    return normalized


def dispatch_strategy_watch_findings(
    findings: list[StrategyWatchFinding],
    *,
    source_repo: str = "",
    dry_run: bool = True,
    comment_existing: bool = True,
    create_issue: Callable[[str, str, str], str] = create_github_issue,
    comment_issue: Callable[[str, str, str], str] = comment_github_issue,
    list_issues: Callable[[str], dict[str, str]] = list_open_issue_urls,
) -> dict[str, Any]:
    if source_repo and not REPO_RE.fullmatch(source_repo):
        raise ValueError("source_repo must be in owner/name form")
    issues: list[dict[str, Any]] = []
    open_issue_cache: dict[str, dict[str, str]] = {}
    for finding in findings:
        task = finding_to_automation_task(finding)
        issue = issue_for_task(task)
        issue_key = watcher_issue_key(task)
        repo = source_repo or finding.snapshot.repo
        if not REPO_RE.fullmatch(repo):
            raise ValueError("finding repository must be in owner/name form")
        issue_result: dict[str, Any] = {
            "repo": repo,
            "title": issue["title"],
            "task": task_public_summary(task),
            "watcher_issue_key": issue_key,
            "created": False,
        }
        if dry_run:
            issue_result["dry_run"] = True
        else:
            try:
                if repo not in open_issue_cache:
                    open_issue_cache[repo] = list_issues(repo)
                existing_url = open_issue_cache[repo].get(issue_key, "")
                if existing_url:
                    issue_result["existing_url"] = existing_url
                    if comment_existing:
                        issue_result["comment_url"] = comment_issue(repo, existing_url, issue["body"])
                        issue_result["commented"] = True
                        issue_result["skipped_reason"] = "open issue already exists; appended watcher update"
                    else:
                        issue_result["skipped_reason"] = "open issue already records this strategy"
                else:
                    issue_result["url"] = create_issue(repo, issue["title"], issue["body"])
                    open_issue_cache[repo][issue_key] = str(issue_result["url"])
                    issue_result["created"] = True
            except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
                issue_result["error"] = str(exc)
        issues.append(issue_result)
    errors = sum(1 for issue in issues if issue.get("error"))
    return {
        "status": "partial_error" if errors else "ok",
        "dry_run": dry_run,
        "findings": len(findings),
        "issues": issues,
        "errors": errors,
    }


def run_watcher(
    payload: dict[str, Any],
    *,
    source_repo: str = "",
    dry_run: bool = True,
    create_issue: Callable[[str, str, str], str] = create_github_issue,
    comment_issue: Callable[[str, str, str], str] = comment_github_issue,
    list_issues: Callable[[str], dict[str, str]] = list_open_issue_urls,
) -> dict[str, Any]:
    if not dry_run and not source_repo:
        raise ValueError("source_repo is required for non-dry-run strategy watcher runs")
    if source_repo and not REPO_RE.fullmatch(source_repo):
        raise ValueError("source_repo must be in owner/name form")
    watch_payload = _payload_for_source_repo(payload, source_repo)
    findings = evaluate_strategy_watch(watch_payload)
    result = dispatch_strategy_watch_findings(
        findings,
        source_repo=source_repo,
        dry_run=dry_run,
        create_issue=create_issue,
        comment_issue=comment_issue,
        list_issues=list_issues,
    )
    result["research_task_source_snapshot"] = research_task_source_snapshot(
        findings,
        context_available=research_task_context_available(watch_payload),
        computed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return result


def run_research_input_terminal_watcher(
    terminal: dict[str, Any],
    *,
    source_repo: str,
    profile: str = "",
    source: str = "",
    dry_run: bool = True,
    create_issue: Callable[[str, str, str], str] = create_github_issue,
    comment_issue: Callable[[str, str, str], str] = comment_github_issue,
    list_issues: Callable[[str], dict[str, str]] = list_open_issue_urls,
) -> dict[str, Any]:
    """Surface a trusted deferred P1 record as an issue-only finding.

    An accepted P1 terminal record is not a failure.  It simply means that
    the producer has not yet emitted the two comparable P3 observations the
    watcher needs.  Keep that state visible to the unified console without
    opening a misleading issue or failing the scheduled watcher.
    """
    candidate = terminal.get("candidate") if isinstance(terminal.get("candidate"), dict) else {}
    status = str(terminal.get("status") or "").strip().upper()
    reason_code = str(terminal.get("reason_code") or "").strip()
    if status != "DEFERRED" or not reason_code:
        reason = "p1_terminal_accepted" if status == "ACCEPTED" else "p1_terminal_contract_unavailable"
        return no_comparable_metrics_result(reason=reason, dry_run=dry_run)
    finding = build_research_input_unavailable_finding(
        repo=source_repo,
        profile=profile,
        status=status,
        reason_code=reason_code,
        candidate_id=str(candidate.get("candidate_id") or ""),
        date_cutoff=str(terminal.get("date_cutoff") or ""),
        source=source,
    )
    result = dispatch_strategy_watch_findings(
        [finding],
        source_repo=source_repo,
        dry_run=dry_run,
        create_issue=create_issue,
        comment_issue=comment_issue,
        list_issues=list_issues,
    )
    result["research_task_source_snapshot"] = research_task_source_snapshot(
        [finding],
        context_available=False,
        computed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return result


def no_comparable_metrics_result(
    *, reason: str = "comparable_metrics_unavailable", dry_run: bool = True
) -> dict[str, Any]:
    """Return a successful, source-owned unavailable queue snapshot.

    This is deliberately not an exception: optimization requires two trusted
    comparable P3 observations.  Until they exist, the console must show an
    unavailable source rather than silently retaining stale tasks or marking
    an accepted P1 acquisition as a watcher failure.
    """
    snapshot = research_task_source_snapshot(
        [],
        context_available=False,
        computed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    snapshot["errors"] = sorted(set(snapshot["errors"] + [reason]))
    return {
        "status": "ok",
        "dry_run": dry_run,
        "findings": 0,
        "issues": [],
        "errors": 0,
        "research_task_source_snapshot": snapshot,
    }


def main() -> int:
    try:
        input_path = resolve_input_path(
            input_path=os.environ.get("STRATEGY_WATCH_INPUT", "").strip(),
            source_root=os.environ.get("STRATEGY_WATCH_SOURCE_ROOT", "").strip(),
            metrics_path=os.environ.get("STRATEGY_WATCH_METRICS_PATH", "").strip(),
        )
    except ValueError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 2
    if input_path is None:
        result = no_comparable_metrics_result(reason="metrics_input_not_configured")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    terminal_path_text = os.environ.get("STRATEGY_WATCH_TERMINAL_STATUS_PATH", "").strip()
    terminal_path = None
    if terminal_path_text:
        try:
            terminal_path = resolve_input_path(
                source_root=os.environ.get("STRATEGY_WATCH_SOURCE_ROOT", "").strip(),
                metrics_path=terminal_path_text,
            )
        except ValueError as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
            return 2
    if not input_path.exists():
        if terminal_path is None or not terminal_path.is_file():
            result = no_comparable_metrics_result()
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        try:
            terminal = load_payload(terminal_path)
            dry_run = parse_bool(os.environ.get("STRATEGY_WATCH_DRY_RUN"), default=True)
            result = run_research_input_terminal_watcher(
                terminal,
                source_repo=os.environ.get("STRATEGY_WATCH_SOURCE_REPO", "").strip(),
                profile=os.environ.get("STRATEGY_WATCH_TERMINAL_PROFILE", "").strip(),
                source=str(terminal_path),
                dry_run=dry_run,
            )
        except (OSError, json.JSONDecodeError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if int(result.get("errors", 0)) > 0 or result.get("status") != "ok" else 0
    if not input_path.is_file():
        print(json.dumps({"status": "error", "error": "strategy metrics input is not a file"}, sort_keys=True))
        return 2
    try:
        payload = load_payload(input_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 2
    try:
        dry_run = parse_bool(os.environ.get("STRATEGY_WATCH_DRY_RUN"), default=True)
    except ValueError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 2
    try:
        result = run_watcher(
            payload,
            source_repo=os.environ.get("STRATEGY_WATCH_SOURCE_REPO", "").strip(),
            dry_run=dry_run,
        )
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if int(result.get("errors", 0)) > 0 or result.get("status") != "ok" else 0


if __name__ == "__main__":
    raise SystemExit(main())
