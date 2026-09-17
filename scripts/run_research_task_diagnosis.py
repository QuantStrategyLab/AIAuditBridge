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
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.config import GatewayConfig  # noqa: E402
from client.gateway_client import AiGatewayClient  # noqa: E402
from service.provider_scenarios import (  # noqa: E402
    SCENARIO_RESEARCH_TASK_DIAGNOSIS,
    resolve_execute_kwargs,
)
from service.research_diagnosis import (  # noqa: E402
    build_research_diagnosis_prompt,
    build_research_diagnosis_request,
    format_research_diagnosis_comment,
    marker_for_research_diagnosis,
)


MAX_AUTOMATIC_DIAGNOSES = 1
_REPOSITORY = "QuantStrategyLab"
_ATTEMPT_MARKER_PREFIX = "qsl-research-diagnosis-attempt:v1"
_DEFERRED_MARKER_PREFIX = "qsl-research-diagnosis-deferred:v1"
_DEFERRED_MARKER_RE = re.compile(
    r"<!--\s*" + re.escape(_DEFERRED_MARKER_PREFIX)
    + r":(?P<task_id>watcher-[0-9a-f]{12}):(?P<task_sha256>[0-9a-f]{64}):(?P<claim_comment_id>[1-9][0-9]*):(?P<retry_at>[0-9]+(?:\.[0-9]+)?)\s*-->"
)


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


def issue_is_open_and_undiagnosed(repository: str, issue_url: str, marker: str, *, now: float | None = None) -> bool:
    """Return true only for an open Issue with no terminal or parked state."""
    app_id = os.environ.get("SOURCE_GITHUB_APP_ID", "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", app_id):
        return False
    try:
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/issues/([1-9][0-9]*)", issue_url
        )
        if match is None:
            return False
        issue = subprocess.run(
            ["gh", "api", f"repos/{repository}/issues/{match.group(1)}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        issue_payload = json.loads(issue.stdout)
        if not isinstance(issue_payload, Mapping) or issue_payload.get("state") != "open":
            return False
        comments_result = subprocess.run(
            ["gh", "api", "--paginate", f"repos/{repository}/issues/{match.group(1)}/comments?per_page=100"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        decoder = json.JSONDecoder()
        comments: list[Any] = []
        cursor = 0
        while cursor < len(comments_result.stdout):
            while cursor < len(comments_result.stdout) and comments_result.stdout[cursor].isspace():
                cursor += 1
            if cursor == len(comments_result.stdout):
                break
            page, cursor = decoder.raw_decode(comments_result.stdout, cursor)
            if not isinstance(page, list):
                return False
            comments.extend(page)
        if not comments_result.stdout.strip():
            return False
        state = _issue_comment_state(comments, marker, now=time.time() if now is None else now)
        return state in {"none", "due"}
    except (OSError, TypeError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def issue_has_diagnosis_marker(repository: str, issue_url: str, marker: str) -> bool:
    """Any closed Issue or retrieval error is treated as already handled."""
    return not issue_is_open_and_undiagnosed(repository, issue_url, marker)


def attempt_marker_for_research_diagnosis(request: Mapping[str, Any]) -> str:
    """Return the durable pre-AI claim marker for one verified task."""
    return marker_for_research_diagnosis(request).replace(
        "<!-- qsl-research-diagnosis:v1:",
        f"<!-- {_ATTEMPT_MARKER_PREFIX}:",
        1,
    )


def format_research_diagnosis_attempt_comment(request: Mapping[str, Any]) -> str:
    """Build a visible durable claim without presenting it as diagnosis output."""
    return (
        f"{attempt_marker_for_research_diagnosis(request)}\n"
        "本任务已开始自动诊断；若没有后续结果，保持暂停，避免重复执行。"
    )


def deferred_marker_for_research_diagnosis(request: Mapping[str, Any], retry_at: object, claim_comment_id: str) -> str:
    """Return a retry marker bound to the same task as the durable claim."""
    if not isinstance(retry_at, (int, float)) or isinstance(retry_at, bool) or not math.isfinite(retry_at) or retry_at <= 0:
        raise ValueError("research diagnosis retry_at is invalid")
    if not re.fullmatch(r"[1-9][0-9]*", claim_comment_id):
        raise ValueError("research diagnosis claim comment ID is invalid")
    return (
        f"<!-- {_DEFERRED_MARKER_PREFIX}:{request['task_id']}:{request['task_sha256']}:{claim_comment_id}:{retry_at} -->"
    )


def format_research_diagnosis_deferred_comment(request: Mapping[str, Any], retry_at: object, claim_comment_id: str) -> str:
    """Build a visible, non-diagnostic marker for a trusted future retry."""
    return (
        f"{deferred_marker_for_research_diagnosis(request, retry_at, claim_comment_id)}\n"
        "网关明确额度暂缓；达到记录的恢复时间前保持暂停，之后仅重新领取一次。"
    )


def _comment_id_from_url(value: object) -> str | None:
    match = re.search(r"(?:issuecomment-|/comments/)([1-9][0-9]*)", str(value or ""))
    return match.group(1) if match else None


def _recoverable_deferred_retry_at(ai_result: Any, config: GatewayConfig, *, now: float) -> float | None:
    """Accept only the gateway's explicit pre-execution quota deferral."""
    raw = getattr(ai_result, "raw", None)
    if not (
        getattr(ai_result, "success", True) is False
        and getattr(ai_result, "provider", "") in config.research_providers
        and isinstance(raw, Mapping)
        and raw.get("status") == "deferred"
        and raw.get("execution_started") is False
        and raw.get("failure_category") == "quota_or_capacity_failure"
    ):
        return None
    retry_at = raw.get("retry_at")
    if not isinstance(retry_at, (int, float)) or isinstance(retry_at, bool) or not math.isfinite(retry_at):
        return None
    if retry_at <= now:
        return None
    return float(retry_at)


def _comment_is_trusted_application(comment: Mapping[str, Any]) -> bool:
    """Accept only bot/App-authored state comments from the Issue history."""
    app = comment.get("performed_via_github_app")
    expected_app = os.environ.get("SOURCE_GITHUB_APP_ID", "").strip()
    return (
        bool(re.fullmatch(r"[1-9][0-9]*", expected_app))
        and isinstance(app, Mapping)
        and str(app.get("id") or "") == expected_app
    )


def _comment_created_at(comment: Mapping[str, Any]) -> float | None:
    value = comment.get("createdAt") or comment.get("created_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _issue_comment_state(comments: list[Any], marker: str, *, now: float) -> str:
    """Resolve the latest trusted state; malformed state conservatively parks."""
    last_id = 0
    last_created_at: float | None = None
    for item in comments:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), int) or item["id"] <= last_id:
            return "unknown"
        created_at = _comment_created_at(item)
        if created_at is None or created_at > now or (last_created_at is not None and created_at < last_created_at):
            return "unknown"
        last_id = item["id"]
        last_created_at = created_at
    deferred_match = re.search(
        r"<!--\s*qsl-research-diagnosis:v1:watcher-(?P<event>[0-9a-f]{12}):(?P<sha>[0-9a-f]{64})\s*-->$",
        marker,
    )
    if deferred_match is None:
        return "unknown"
    attempt_marker = (
        f"<!-- {_ATTEMPT_MARKER_PREFIX}:watcher-{deferred_match.group('event')}:{deferred_match.group('sha')} -->"
    )
    attempt_seen = False
    attempt_id: str | None = None
    attempt_created_at: float | None = None
    state = "none"
    for item in comments:
        if not isinstance(item, Mapping):
            return "unknown"
        body = item.get("body")
        if not isinstance(body, str):
            continue
        deferred = _DEFERRED_MARKER_RE.search(body)
        deferred_matches_task = bool(
            deferred
            and deferred.group("task_id") == f"watcher-{deferred_match.group('event')}"
            and deferred.group("task_sha256") == deferred_match.group("sha")
        )
        has_marker = marker in body or attempt_marker in body or deferred_matches_task
        if not has_marker:
            continue
        if not _comment_is_trusted_application(item):
            return "unknown"
        created_at = _comment_created_at(item)
        if attempt_marker in body:
            current_id = item.get("id")
            if created_at is None or created_at > now or not isinstance(current_id, int) or current_id <= 0:
                return "unknown"
            attempt_seen = True
            attempt_id = str(current_id)
            attempt_created_at = created_at
            state = "started"
            continue
        if marker in body:
            return "success"
        if deferred is None or not deferred_matches_task:
            return "unknown"
        if not attempt_seen or attempt_id is None or attempt_created_at is None or created_at is None or created_at < attempt_created_at or created_at > now:
            return "unknown"
        try:
            deferred_id = str(deferred.group("claim_comment_id"))
            retry_at = float(deferred.group("retry_at"))
        except (TypeError, ValueError):
            return "unknown"
        if deferred_id != attempt_id or not math.isfinite(retry_at) or retry_at <= created_at:
            continue
        state = "deferred" if retry_at > now else "due"
    return state


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
    """Diagnose at most one issue after a durable claim; uncertain claims never call AI."""
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

    config: GatewayConfig | None = None
    client: AiGatewayClient | None = None
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
        if client is None:
            try:
                config = GatewayConfig.from_env()
            except ValueError:
                summary["diagnoses"].append(
                    {"status": "not_configured", "task_id": request["task_id"], "error": "ai_gateway_not_configured"}
                )
                continue
            client = client_factory(config)
        try:
            claim_url = create_comment(
                str(candidate["repository"]), str(candidate["issue_url"]),
                format_research_diagnosis_attempt_comment(request),
            )
            if not isinstance(claim_url, str) or not claim_url.strip():
                raise RuntimeError("claim_write_result_unknown")
        except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            summary["diagnoses"].append(
                {
                    "status": "claim_failed",
                    "task_id": request["task_id"],
                    "error": _error_summary(exc),
                }
            )
            continue
        try:
            ai_result = client.execute(
                prompt,
                **resolve_execute_kwargs(
                    SCENARIO_RESEARCH_TASK_DIAGNOSIS,
                    research_providers=config.research_providers,
                ),
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
        if isinstance(raw, dict) and raw.get("status") == "deferred":
            retry_at = _recoverable_deferred_retry_at(ai_result, config, now=time.time())
            if retry_at is None:
                summary["diagnoses"].append({
                    "status": "unavailable", "task_id": request["task_id"],
                    "error": "deferred_result_unavailable",
                })
                continue
            try:
                claim_comment_id = _comment_id_from_url(claim_url)
                if claim_comment_id is None:
                    raise RuntimeError("claim_comment_identity_unknown")
                deferred_url = create_comment(
                    str(candidate["repository"]), str(candidate["issue_url"]),
                    format_research_diagnosis_deferred_comment(request, retry_at, claim_comment_id),
                )
                if not isinstance(deferred_url, str) or not deferred_url.strip():
                    raise RuntimeError("deferred_comment_result_unknown")
            except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                summary["diagnoses"].append({
                    "status": "deferred_comment_failed", "task_id": request["task_id"],
                    "error": _error_summary(exc),
                })
                continue
            summary["diagnoses"].append({
                "status": "deferred", "task_id": request["task_id"], "retry_at": retry_at,
                "comment_url": deferred_url,
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
    if any(item.get("status") in {"unavailable", "comment_failed", "deferred_comment_failed", "claim_failed", "not_configured", "rejected"} for item in summary["diagnoses"]):
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
    # A missing service, failed diagnosis, or uncertain comment write must not
    # turn into a strategy action or break the primary watcher.  A durable
    # started marker parks the task until an explicit recovery decision.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
