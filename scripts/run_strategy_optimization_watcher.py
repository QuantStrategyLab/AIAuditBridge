#!/usr/bin/env python3
"""Run the issue-only strategy optimization watcher."""

from __future__ import annotations

import copy
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

from service.strategy_watch import evaluate_strategy_watch, finding_to_automation_task, issue_for_task  # noqa: E402

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
            title = str(issue.get("title") or "")
            if title and title not in open_issues:
                open_issues[title] = str(issue.get("html_url") or issue.get("url") or "")
        if len(issues) < 100:
            return open_issues
        page += 1


def find_existing_open_issue(repo: str, title: str) -> str:
    return list_open_issue_urls(repo).get(title, "")


def task_public_summary(task: Any) -> dict[str, Any]:
    payload = task.to_dict()
    trigger = payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {}
    proposed_action = payload.get("proposed_action") if isinstance(payload.get("proposed_action"), dict) else {}
    gate_decision = payload.get("gate_decision") if isinstance(payload.get("gate_decision"), dict) else {}
    return {
        "trigger": {
            "source": trigger.get("source", ""),
            "kind": trigger.get("kind", ""),
            "severity": trigger.get("severity", ""),
            "subject": trigger.get("subject", ""),
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
        "status": payload.get("status", ""),
    }


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


def run_watcher(
    payload: dict[str, Any],
    *,
    source_repo: str = "",
    dry_run: bool = True,
    create_issue: Callable[[str, str, str], str] = create_github_issue,
    list_issues: Callable[[str], dict[str, str]] = list_open_issue_urls,
) -> dict[str, Any]:
    watch_payload = _payload_for_source_repo(payload, source_repo)
    findings = evaluate_strategy_watch(watch_payload)
    issues: list[dict[str, Any]] = []
    open_issue_cache: dict[str, dict[str, str]] = {}
    for finding in findings:
        task = finding_to_automation_task(finding)
        issue = issue_for_task(task)
        repo = source_repo or finding.snapshot.repo
        issue_result: dict[str, Any] = {
            "repo": repo,
            "title": issue["title"],
            "task": task_public_summary(task),
            "created": False,
        }
        if dry_run:
            issue_result["dry_run"] = True
        else:
            if repo not in open_issue_cache:
                open_issue_cache[repo] = list_issues(repo)
            existing_url = open_issue_cache[repo].get(issue["title"], "")
            if existing_url:
                issue_result["existing_url"] = existing_url
                issue_result["skipped_reason"] = "open issue already exists"
            else:
                issue_result["url"] = create_issue(repo, issue["title"], issue["body"])
                open_issue_cache[repo][issue["title"]] = str(issue_result["url"])
                issue_result["created"] = True
        issues.append(issue_result)
    return {
        "status": "ok",
        "dry_run": dry_run,
        "findings": len(findings),
        "issues": issues,
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
        print(json.dumps({"status": "skipped", "reason": "strategy metrics input not configured"}, sort_keys=True))
        return 0
    if not input_path.exists():
        print(json.dumps({"status": "error", "error": "strategy metrics input not found"}, sort_keys=True))
        return 2
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
