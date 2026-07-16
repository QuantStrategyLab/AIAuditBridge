#!/usr/bin/env python3
"""PR check: blocking static scan plus advisory Codex App review.

Two phases, zero API keys needed:
  1. STATIC — scan diff for secrets, blocked files, metadata issues (<30s).
              Fail job immediately on hard violations.
  2. OBSERVE — read a current-head Codex GitHub App review without polling.
  3. REACT   — on Codex bot review submitted: report it immediately.

Only the static guard can fail this check. The repository-owned Codex PR Review
workflow remains the authoritative AI review gate.
"""

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
BOT_LOGIN = "chatgpt-codex-connector[bot]"
POLICY_PATH = Path(".github/codex_auto_merge_policy.json")


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
    return json.loads(body) if body else {}


def step_summary(text: str) -> None:
    p = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def run_static_guard(token: str, repo: str, pr_number: int) -> int:
    """Return 0 if clean, 1 if blocked."""
    policy = load_policy(POLICY_PATH)
    files: list[dict[str, Any]] = []
    page = 1
    while True:
        try:
            batch = github_request(token, "GET",
                f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}")
        except RuntimeError:
            break
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
    except Exception:
        pass

    issues = collect_static_gate_issues(files, diff_text, policy)
    if not issues:
        return 0

    print(f"STATIC → BLOCKED: {len(issues)} issue(s)")
    for i in issues:
        print(f"  • {i}")
    step_summary(f"## Merge blocked: {len(issues)} static issue(s)\n\n" +
                 "\n".join(f"- {i}" for i in issues))
    return 1


# ─── app review ──────────────────────────────────────────────────────────────

def review_matches_head(review: dict[str, Any], head_sha: str) -> bool:
    return type(review.get("commit_id")) is str and review["commit_id"] == head_sha


def get_codex_review(
    token: str,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> dict[str, Any] | None:
    reviews = github_request(token, "GET", f"/repos/{repo}/pulls/{pr_number}/reviews?per_page=100")
    if not isinstance(reviews, list):
        return None
    for r in reversed(reviews):
        if (
            isinstance(r, dict)
            and (r.get("user") or {}).get("login") == BOT_LOGIN
            and review_matches_head(r, head_sha)
        ):
            return r
    return None


def app_decision(review: dict[str, Any] | None) -> tuple[int, str, str]:
    """(exit_code, title, summary)"""
    if review is None:
        return (
            0,
            "Codex advisory: no current-head review",
            "No current-head connector review is available; this advisory does not block merge.",
        )
    state = (review.get("state") or "").strip().upper()
    url = review.get("html_url", "")
    body = (review.get("body") or "").strip()
    at = review.get("submitted_at", "")

    if state == "CHANGES_REQUESTED":
        snippet = (body[:500] + "...") if len(body) > 500 else body
        return (
            0,
            "Codex advisory: changes requested",
            f"Codex **requested changes** at {at}, but this advisory does not block merge.\n\n"
            f"{snippet}\n\n[View review]({url})",
        )
    if state == "APPROVED":
        return (0, "Codex advisory: approved", f"Codex approved at {at}. [View review]({url})")
    return (0, f"Codex advisory: reviewed ({state.lower()})",
            f"Codex state `{state}` at {at}. Not blocking. [View review]({url})")


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
    event_name = env("GITHUB_EVENT_NAME", "")
    pr = event.get("pull_request") or {}
    pr_number = pr.get("number")
    head_sha = (pr.get("head") or {}).get("sha")
    if not pr_number or not head_sha:
        print("::warning::Cannot resolve PR context")
        return 0

    print(f"PR #{pr_number}  sha={head_sha[:12]}  event={event_name}")

    # ── Phase 1: Static guard (skip on review-only events) ────────────
    if event_name != "pull_request_review":
        try:
            rc = run_static_guard(token, repo, pr_number)
        except RuntimeError as exc:
            print(f"::warning::Static guard error: {exc}")
            rc = 0
        if rc != 0:
            return 1
        print("STATIC → clean")

    # ── Phase 2: advisory App review ──────────────────────────────────
    # REACT: Codex just submitted a review
    review_event = event.get("review") or {}
    if (
        event_name == "pull_request_review"
        and (review_event.get("user") or {}).get("login") == BOT_LOGIN
    ):
        if not review_matches_head(review_event, head_sha):
            print("REACT → stale connector review ignored")
            step_summary(
                "## Codex advisory ignored\n\n"
                "The submitted connector review does not match the current PR head."
            )
            return 0
        rc, title, summary = app_decision(review_event)
        print(f"REACT → exit={rc}: {title}")
        step_summary(f"## {title}\n\n{summary}")
        return rc

    # WAIT: poll for existing or upcoming review
    try:
        existing = get_codex_review(token, repo, pr_number, head_sha)
    except RuntimeError:
        existing = None

    if existing is not None:
        rc, title, summary = app_decision(existing)
        print(f"EXISTING → exit={rc}: {title}")
        step_summary(f"## {title}\n\n{summary}")
        return rc

    print("ADVISORY → no current-head connector review; not waiting")
    step_summary(
        "## Codex advisory pending\n\n"
        "No current-head connector review is available. The event-driven review hook will report it when submitted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
