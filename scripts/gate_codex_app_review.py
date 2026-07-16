#!/usr/bin/env python3
"""Required PR static gate; connector reviews are reported separately."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from scripts.gate_codex_app_review_static import (
        check_metadata as _check_metadata,
        compile_patterns as _compile_patterns,
        collect_static_gate_issues,
        load_policy as _load_policy,
        scan_diff as _scan_diff,
    )
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from gate_codex_app_review_static import (
        check_metadata as _check_metadata,
        compile_patterns as _compile_patterns,
        collect_static_gate_issues,
        load_policy as _load_policy,
        scan_diff as _scan_diff,
    )

API_BASE = "https://api.github.com"
POLICY_PATH = Path(".github/codex_auto_merge_policy.json")
HEAD_CHECK_NAME = "Codex Review Gate"


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    return _load_policy(path)


def compile_patterns(policy: dict[str, Any]) -> list[Any]:
    return _compile_patterns(policy)


def scan_diff(diff_text: str, path_patterns: list[Any]) -> list[str]:
    return _scan_diff(diff_text, path_patterns)


def check_metadata(files: list[dict[str, Any]], policy: dict[str, Any]) -> list[str]:
    return _check_metadata(files, policy)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def github_request(token: str, method: str, path: str,
                   payload: dict[str, Any] | None = None) -> Any:
    url = f"{API_BASE}{path}" if not path.startswith("https://") else path
    data = json.dumps(payload).encode() if payload else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "codex-review-gate",
    }
    if payload:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {url}: {exc.code} {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API {method} {url} unavailable") from exc
    return json.loads(body) if body else {}


def step_summary(text: str) -> None:
    p = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def create_head_check(token: str, repo: str, pr_number: int, head_sha: str) -> int:
    payload: dict[str, Any] = {
        "name": HEAD_CHECK_NAME,
        "head_sha": head_sha,
        "status": "in_progress",
        "external_id": f"codex-review-gate:{repo}:{pr_number}:{env('GITHUB_RUN_ID')}",
    }
    run_id = env("GITHUB_RUN_ID")
    if run_id:
        server = env("GITHUB_SERVER_URL", "https://github.com")
        payload["details_url"] = f"{server}/{repo}/actions/runs/{run_id}"
    result = github_request(token, "POST", f"/repos/{repo}/check-runs", payload)
    check_id = result.get("id") if isinstance(result, dict) else None
    if type(check_id) is not int or check_id <= 0:
        raise RuntimeError("GitHub Checks API did not return a valid check id")
    return check_id


def complete_head_check(
    token: str,
    repo: str,
    check_id: int,
    conclusion: str,
    summary: str,
) -> None:
    github_request(
        token,
        "PATCH",
        f"/repos/{repo}/check-runs/{check_id}",
        {
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": f"Static gate {conclusion}",
                "summary": summary,
            },
        },
    )


def run_static_guard(token: str, repo: str, pr_number: int) -> int:
    """Return 0 if clean, 1 if blocked."""
    policy = load_policy(POLICY_PATH)
    files: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = github_request(token, "GET",
            f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    diff_text = ""
    try:
        req = urllib.request.Request(
            f"{API_BASE}/repos/{repo}/pulls/{pr_number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3.diff",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "codex-review-gate",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            diff_text = resp.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("Failed to fetch PR diff") from exc

    issues = collect_static_gate_issues(files, diff_text, policy)
    if not issues:
        return 0

    print(f"STATIC → BLOCKED: {len(issues)} issue(s)")
    for i in issues:
        print(f"  • {i}")
    step_summary(f"## Merge blocked: {len(issues)} static issue(s)\n\n" +
                 "\n".join(f"- {i}" for i in issues))
    return 1


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    token = env("GH_TOKEN") or env("GITHUB_TOKEN")
    repo = env("GITHUB_REPOSITORY")
    if not token or not repo:
        print("::error::GH_TOKEN + GITHUB_REPOSITORY required", file=sys.stderr)
        return 1

    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    if not event_path.exists():
        print("::error::GITHUB_EVENT_PATH missing", file=sys.stderr)
        return 1

    event = json.loads(event_path.read_text(encoding="utf-8"))
    pr = event.get("pull_request") or {}
    pr_number = pr.get("number")
    head_sha = (pr.get("head") or {}).get("sha")
    if not pr_number or not head_sha:
        print("::error::Cannot resolve PR context", file=sys.stderr)
        return 1

    print(f"PR #{pr_number}  sha={head_sha[:12]}")
    try:
        check_id = create_head_check(token, repo, pr_number, head_sha)
    except RuntimeError as exc:
        print(f"::error::Cannot publish head gate: {exc}", file=sys.stderr)
        return 1

    try:
        rc = run_static_guard(token, repo, pr_number)
    except RuntimeError as exc:
        print(f"::error::Static guard unavailable: {exc}", file=sys.stderr)
        try:
            complete_head_check(token, repo, check_id, "failure", "Static guard unavailable.")
        except RuntimeError as update_exc:
            print(f"::error::Cannot complete head gate: {update_exc}", file=sys.stderr)
        return 1
    conclusion = "success" if rc == 0 else "failure"
    summary = (
        "Static policy checks passed."
        if rc == 0
        else "Static policy checks blocked this PR."
    )
    try:
        complete_head_check(token, repo, check_id, conclusion, summary)
    except RuntimeError as exc:
        print(f"::error::Cannot complete head gate: {exc}", file=sys.stderr)
        return 1
    if rc == 0:
        print("STATIC → clean")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
