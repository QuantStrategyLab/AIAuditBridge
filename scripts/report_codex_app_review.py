#!/usr/bin/env python3
"""Report the official Codex connector review as a current-head advisory."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


BOT_LOGIN = "chatgpt-codex-connector[bot]"


def evaluate_event(event: dict[str, Any]) -> tuple[str, str]:
    pr = event.get("pull_request") if isinstance(event.get("pull_request"), dict) else {}
    review = event.get("review") if isinstance(event.get("review"), dict) else {}
    actor = review.get("user") if isinstance(review.get("user"), dict) else {}
    if actor.get("login") != BOT_LOGIN:
        return ("ignored_actor", "Review was not submitted by the Codex connector.")
    head_sha = (pr.get("head") or {}).get("sha") if isinstance(pr.get("head"), dict) else None
    if type(head_sha) is not str or review.get("commit_id") != head_sha:
        return ("ignored_stale", "Connector review does not match the current PR head.")
    state = str(review.get("state") or "COMMENTED").strip().upper()
    submitted_at = str(review.get("submitted_at") or "unknown time").strip()
    return (
        "reported",
        f"Current-head connector review state: `{state}` at {submitted_at}. Advisory only.",
    )


def main() -> int:
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    if not event_path.is_file():
        print("::error::GITHUB_EVENT_PATH missing", file=sys.stderr)
        return 1
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"::error::Invalid GitHub event: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not isinstance(event, dict):
        print("::error::GitHub event must be an object", file=sys.stderr)
        return 1
    outcome, summary = evaluate_event(event)
    print(f"ADVISORY → {outcome}: {summary}")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(f"## Codex Review Advisory\n\n{summary}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
