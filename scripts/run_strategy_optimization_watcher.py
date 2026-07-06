#!/usr/bin/env python3
"""Run the issue-only strategy optimization watcher."""

from __future__ import annotations

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
    return text in {"1", "true", "yes", "on"}


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


def find_existing_open_issue(repo: str, title: str) -> str:
    if not REPO_RE.fullmatch(repo):
        raise ValueError("repository must be in owner/name form")
    page = 1
    while True:
        result = subprocess.run(
            ["gh", "api", f"/repos/{repo}/issues", "-f", "state=open", "-f", "per_page=100", "-f", f"page={page}"],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            issues = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return ""
        if not isinstance(issues, list) or not issues:
            return ""
        for issue in issues:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            if issue.get("title") == title:
                return str(issue.get("html_url") or issue.get("url") or "")
        if len(issues) < 100:
            return ""
        page += 1


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


def run_watcher(
    payload: dict[str, Any],
    *,
    source_repo: str = "",
    dry_run: bool = True,
    create_issue: Callable[[str, str, str], str] = create_github_issue,
    find_issue: Callable[[str, str], str] = find_existing_open_issue,
) -> dict[str, Any]:
    findings = evaluate_strategy_watch(payload)
    issues: list[dict[str, Any]] = []
    for finding in findings:
        task = finding_to_automation_task(finding)
        issue = issue_for_task(task)
        repo = source_repo or finding.snapshot.repo
        issue_result: dict[str, Any] = {
            "repo": repo,
            "title": issue["title"],
            "task": task.to_dict(),
            "created": False,
        }
        if dry_run:
            issue_result["dry_run"] = True
        else:
            existing_url = find_issue(repo, issue["title"])
            if existing_url:
                issue_result["existing_url"] = existing_url
                issue_result["skipped_reason"] = "open issue already exists"
            else:
                issue_result["url"] = create_issue(repo, issue["title"], issue["body"])
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
    payload = load_payload(input_path)
    result = run_watcher(
        payload,
        source_repo=os.environ.get("STRATEGY_WATCH_SOURCE_REPO", "").strip(),
        dry_run=parse_bool(os.environ.get("STRATEGY_WATCH_DRY_RUN"), default=True),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
