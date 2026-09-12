#!/usr/bin/env python3
"""Turn one verified watcher task into one read-only AI diagnosis comment.

The watcher still owns issue creation and task construction.  This dispatcher
only consumes a task that is already cryptographically bound to P1/P2/P3
digests, asks the existing Codex execution endpoint for a read-only assessment, then adds one
idempotency-marked comment to the existing issue.  It cannot run an experiment
or alter a strategy.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.config import GatewayConfig  # noqa: E402
from client.gateway_client import AiGatewayClient  # noqa: E402
from service.research_diagnosis import (  # noqa: E402
    build_research_diagnosis_prompt,
    build_research_diagnosis_request,
    format_research_diagnosis_comment,
    marker_for_research_diagnosis,
)


MAX_AUTOMATIC_DIAGNOSES = 1
_REPOSITORY = "QuantStrategyLab"


def _clean_repo(value: object) -> str:
    repo = str(value or "").strip()
    if not repo.startswith(f"{_REPOSITORY}/") or "/" not in repo:
        return ""
    return repo


def load_watcher_result(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("watcher result must be a JSON object")
    return payload


def diagnosis_candidates(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Join watcher issue results to the exact current research-task IDs."""
    snapshot = result.get("research_task_source_snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("data_status") != "ready":
        return []
    raw_tasks = snapshot.get("tasks")
    raw_issues = result.get("issues")
    if not isinstance(raw_tasks, list) or not isinstance(raw_issues, list):
        return []
    tasks_by_id: dict[str, Mapping[str, Any]] = {}
    for task in raw_tasks:
        if isinstance(task, Mapping) and isinstance(task.get("task_id"), str):
            tasks_by_id[task["task_id"]] = task

    candidates: list[dict[str, Any]] = []
    for issue in raw_issues:
        if not isinstance(issue, Mapping):
            continue
        summary = issue.get("task")
        if not isinstance(summary, Mapping):
            continue
        event_key = str(summary.get("event_key") or "")
        task = tasks_by_id.get(f"watcher-{event_key}")
        if task is None:
            continue
        repo = _clean_repo(issue.get("repo"))
        issue_url = str(issue.get("url") or issue.get("existing_url") or "").strip()
        trigger = summary.get("trigger") if isinstance(summary.get("trigger"), Mapping) else {}
        if repo and issue_url:
            candidates.append(
                {
                    "repository": repo,
                    "issue_url": issue_url,
                    "task": task,
                    "trigger": trigger,
                    "issue": issue,
                }
            )
    return sorted(candidates, key=lambda item: (str(item["repository"]), str(item["issue_url"])))


def issue_is_open_and_undiagnosed(repository: str, issue_url: str, marker: str) -> bool:
    """Return true only for a readable open Issue without this exact marker."""
    try:
        completed = subprocess.run(
            ["gh", "issue", "view", issue_url, "--repo", repository, "--json", "state,comments"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, Mapping):
            return False
        comments = payload.get("comments")
        if payload.get("state") != "OPEN" or not isinstance(comments, list):
            return False
        return not any(isinstance(item, Mapping) and marker in str(item.get("body") or "") for item in comments)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def issue_has_diagnosis_marker(repository: str, issue_url: str, marker: str) -> bool:
    """Any closed Issue or retrieval error is treated as already handled."""
    return not issue_is_open_and_undiagnosed(repository, issue_url, marker)


def recover_pending_watcher_result(
    current: Mapping[str, Any],
    prior_results: list[Mapping[str, Any]],
    *,
    source_repository: str,
    issue_pending: Callable[[str, str, str], bool] = issue_is_open_and_undiagnosed,
) -> dict[str, Any]:
    """Carry one exact verified historical task into the current watcher handoff."""
    result = copy.deepcopy(dict(current))
    snapshot = result.get("research_task_source_snapshot")
    issues = result.get("issues")
    if (
        _clean_repo(source_repository) != source_repository
        or not isinstance(snapshot, dict)
        or snapshot.get("data_status") != "ready"
        or not isinstance(snapshot.get("tasks"), list)
        or not isinstance(issues, list)
    ):
        return result
    # A current verified task keeps priority. The SOXL handoff deliberately
    # accepts exactly one verified task.
    if snapshot["tasks"]:
        return result

    pending: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for prior in prior_results:
        for candidate in diagnosis_candidates(prior):
            if candidate["repository"] != source_repository:
                continue
            try:
                request = build_research_diagnosis_request(candidate["task"], trigger=candidate["trigger"])
                marker = marker_for_research_diagnosis(request)
            except (TypeError, ValueError):
                continue
            identity = (
                str(candidate["repository"]),
                str(candidate["issue_url"]),
                str(request["task_id"]),
                str(request["task_sha256"]),
            )
            if identity in seen:
                continue
            seen.add(identity)
            if issue_pending(identity[0], identity[1], marker):
                pending.append(candidate)

    if not pending:
        return result
    pending.sort(
        key=lambda item: (
            str(item["task"].get("created_at") or ""),
            str(item["repository"]),
            str(item["issue_url"]),
        )
    )
    recovered = pending[0]
    snapshot["tasks"].append(copy.deepcopy(recovered["task"]))
    issues.append(copy.deepcopy(recovered["issue"]))
    return result


def comment_issue(repository: str, issue_url: str, body: str) -> str:
    completed = subprocess.run(
        ["gh", "issue", "comment", issue_url, "--repo", repository, "--body", body],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _error_summary(value: object) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:300]


def run_diagnosis(
    result: Mapping[str, Any],
    *,
    dry_run: bool = False,
    max_per_run: int = MAX_AUTOMATIC_DIAGNOSES,
    marker_present: Callable[[str, str, str], bool] = issue_has_diagnosis_marker,
    create_comment: Callable[[str, str, str], str] = comment_issue,
    client_factory: Callable[[GatewayConfig], AiGatewayClient] = AiGatewayClient,
) -> dict[str, Any]:
    """Diagnose at most one not-yet-diagnosed issue; failures have no side effect."""
    candidates = diagnosis_candidates(result)
    if max_per_run < 1:
        raise ValueError("max_per_run must be positive")
    pending = [
        item
        for item in candidates
        if not marker_present(
            str(item["repository"]),
            str(item["issue_url"]),
            marker_for_research_diagnosis(build_research_diagnosis_request(item["task"], trigger=item["trigger"])),
        )
    ]
    summary: dict[str, Any] = {
        "schema_version": "qsl.research_diagnosis_dispatch.v1",
        "status": "ok",
        "candidate_count": len(candidates),
        "pending_count": len(pending),
        "max_per_run": max_per_run,
        "dry_run": dry_run,
        "diagnoses": [],
    }
    if not pending:
        summary["status"] = "skipped"
        summary["reason"] = "no_pending_verified_research_task"
        return summary

    try:
        config = GatewayConfig.from_env()
    except ValueError:
        summary["status"] = "not_configured"
        summary["reason"] = "ai_gateway_not_configured"
        return summary
    client = client_factory(config)
    for candidate in pending[:max_per_run]:
        task = candidate["task"]
        try:
            request = build_research_diagnosis_request(task, trigger=candidate["trigger"])
            prompt = build_research_diagnosis_prompt(request)
        except (TypeError, ValueError) as exc:
            summary["diagnoses"].append({"status": "rejected", "error": _error_summary(exc)})
            continue
        if dry_run:
            summary["diagnoses"].append(
                {
                    "status": "dry_run",
                    "task_id": request["task_id"],
                    "task_sha256": request["task_sha256"],
                    "repository": candidate["repository"],
                    "issue_url": candidate["issue_url"],
                    "prompt": prompt,
                }
            )
            continue
        try:
            ai_result = client.execute(
                prompt,
                mode="review_only",
                research_stage="drift_analysis",
                **({"allowed_providers": list(config.research_providers)} if config.research_providers != ("codex",) else {}),
                timeout=600,
                source_repository=str(request["target"]["repository"]),
                source_ref=str(request["target"]["strategy_revision"]),
            )
        except Exception:
            summary["diagnoses"].append({
                "status": "unavailable", "task_id": request["task_id"],
                "error": "codex_unavailable",
            })
            continue
        # Execution completion supplies research text, never promotion authority.
        # This lane has no analyze/review fallback, including quota failures.
        output = ai_result.output
        raw = getattr(ai_result, "raw", None)
        if (ai_result.provider in config.research_providers or (not ai_result.provider and "cursor" in config.research_providers)) and ai_result.success is False and isinstance(raw, dict) and raw.get("status") == "deferred":
            summary["diagnoses"].append({
                "status": "deferred", "task_id": request["task_id"], "retry_at": raw.get("retry_at"),
            })
            continue
        content_available = (
            ai_result.provider in config.research_providers and ai_result.success is True
            and isinstance(output, str) and bool(output.strip())
            and not ai_result.error and not getattr(ai_result, "note", "")
            and isinstance(raw, dict) and raw.get("status") == "succeeded"
            and raw.get("provider", "codex") == ai_result.provider
            and raw.get("output", output) == output
        )
        if not content_available:
            summary["diagnoses"].append({
                "status": "unavailable", "task_id": request["task_id"],
                "error": "codex_result_unavailable",
            })
            continue
        try:
            body = format_research_diagnosis_comment(
                request,
                output,
                provider=ai_result.provider,
                model=ai_result.model,
            )
            comment_url = create_comment(str(candidate["repository"]), str(candidate["issue_url"]), body)
        except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            summary["diagnoses"].append(
                {
                    "status": "comment_failed",
                    "task_id": request["task_id"],
                    "error": _error_summary(exc),
                }
            )
            continue
        summary["diagnoses"].append(
            {
                "status": "diagnosed",
                "task_id": request["task_id"],
                "task_sha256": request["task_sha256"],
                "repository": candidate["repository"],
                "issue_url": candidate["issue_url"],
                "comment_url": comment_url,
            }
        )
    if any(item.get("status") in {"unavailable", "comment_failed", "rejected"} for item in summary["diagnoses"]):
        summary["status"] = "partial_error"
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded AI diagnosis for verified research tasks.")
    parser.add_argument("--input", required=True, help="Watcher result JSON path")
    parser.add_argument("--prior-input", action="append", default=[], help="Prior successful watcher result JSON path")
    parser.add_argument("--source-repository", help="Current watcher source repository for historical recovery")
    parser.add_argument("--output-watcher-result", help="Write the recovered handoff result before diagnosis")
    parser.add_argument("--dry-run", action="store_true", help="Build the prompt but do not call AI or comment")
    parser.add_argument("--max-per-run", type=int, default=MAX_AUTOMATIC_DIAGNOSES)
    args = parser.parse_args(argv)
    try:
        watcher_result = load_watcher_result(args.input)
        marker_present: Callable[[str, str, str], bool] = issue_has_diagnosis_marker
        if args.prior_input:
            if not args.source_repository or not args.output_watcher_result:
                raise ValueError("historical recovery requires source repository and watcher result output")
            pending_cache: dict[tuple[str, str, str], bool] = {}

            def cached_issue_pending(repository: str, issue_url: str, marker: str) -> bool:
                key = (repository, issue_url, marker)
                if key not in pending_cache:
                    pending_cache[key] = issue_is_open_and_undiagnosed(repository, issue_url, marker)
                return pending_cache[key]

            prior_results: list[Mapping[str, Any]] = []
            for path in args.prior_input:
                try:
                    prior_results.append(load_watcher_result(path))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            watcher_result = recover_pending_watcher_result(
                watcher_result,
                prior_results,
                source_repository=args.source_repository,
                issue_pending=cached_issue_pending,
            )
            Path(args.output_watcher_result).write_text(
                json.dumps(watcher_result, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            def cached_marker_present(repository: str, issue_url: str, marker: str) -> bool:
                return not cached_issue_pending(repository, issue_url, marker)

            marker_present = cached_marker_present
        result = run_diagnosis(
            watcher_result,
            dry_run=args.dry_run,
            max_per_run=args.max_per_run,
            marker_present=marker_present,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": _error_summary(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # A missing service or failed text-only diagnosis must not turn into a
    # strategy action or break the primary watcher.  Its durable issue remains
    # the retry point for the next scheduled run.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
