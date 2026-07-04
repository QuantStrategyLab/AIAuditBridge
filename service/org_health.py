"""GitHub org health snapshots for AIAuditBridge dashboards."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_ORG = "QuantStrategyLab"
DEFAULT_REPOSITORIES = [
    "AIAuditBridge",
    "UsEquitySnapshotPipelines",
    "HkEquitySnapshotPipelines",
    "CryptoLivePoolPipelines",
    "ResearchSignalContextPipelines",
    "BinancePlatform",
    "InteractiveBrokersPlatform",
    "LongBridgePlatform",
    "CharlesSchwabPlatform",
    "FirstradePlatform",
    "IBKRGatewayManager",
    "QuantAdvisorResearch",
]

_IN_PROGRESS_STATUSES = {"queued", "in_progress", "requested", "waiting"}
_UNHEALTHY_CONCLUSIONS = {"failure", "timed_out", "action_required", "startup_failure"}
_IGNORED_CONCLUSIONS = {"neutral", "skipped"}
_DEGRADED_CONCLUSIONS = {"cancelled", "stale"}
CacheKey = tuple[tuple[str, ...], str, str, str]
_CACHE_LOCK = threading.Lock()
_CACHE: dict[CacheKey, tuple[float, dict[str, Any]]] = {}
_REFRESH_EVENTS: dict[CacheKey, threading.Event] = {}
DEFAULT_WORKER_LIMIT = 4
DEFAULT_RUNS_PER_PAGE = 5
DEFAULT_RUN_LOOKBACK_PAGES = 3
DEFAULT_WORKFLOW_LIMIT = 8
DEFAULT_REFRESH_DEADLINE_SECONDS = 12.0
DEFAULT_COLD_ASYNC_REPOSITORY_THRESHOLD = 4
DEFAULT_WORKFLOW_ALLOWLIST = (
    "Auto Merge Dependabot PR",
    "Check",
    "CI",
    "Codex PR Review",
    "Codex Review Gate",
    "Monthly Orchestrator",
    "Secret Scan",
    "VPS Codex Service Ops",
    "ci",
)


def _split_repo_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def _normalize_repository(value: str) -> str:
    item = value.strip()
    if not item:
        return ""
    return item if "/" in item else f"{DEFAULT_ORG}/{item}"


def _repository_targets() -> list[str]:
    configured = _split_repo_env("CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES")
    repos = configured or DEFAULT_REPOSITORIES
    seen: set[str] = set()
    targets: list[str] = []
    for repo in repos:
        full = _normalize_repository(repo)
        if not full or full in seen:
            continue
        seen.add(full)
        targets.append(full)
    return targets


def _github_token() -> tuple[str, str]:
    token = os.environ.get("CODEX_AUDIT_SERVICE_GITHUB_TOKEN", "").strip()
    if token:
        return token, "CODEX_AUDIT_SERVICE_GITHUB_TOKEN"
    return "", ""


def _remaining_timeout(timeout_seconds: float, deadline: float | None) -> float:
    if deadline is None:
        return timeout_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("org health collection deadline exceeded")
    return min(timeout_seconds, max(0.1, remaining))


def _github_request_json(path: str, token: str, timeout_seconds: float, deadline: float | None = None) -> dict[str, Any]:
    request = Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AIAuditBridge",
        },
    )
    with urlopen(request, timeout=_remaining_timeout(timeout_seconds, deadline)) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub API response was not an object")
    return payload


def _empty_run() -> dict[str, Any]:
    return {"name": "", "status": "", "conclusion": "", "created_at": "", "url": "", "branch": ""}


def _degraded_run_reason(run: dict[str, Any]) -> str:
    run_status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    if conclusion in _IGNORED_CONCLUSIONS:
        return ""
    if conclusion in _DEGRADED_CONCLUSIONS:
        return f"latest_run_conclusion_{conclusion}"
    if conclusion and conclusion != "success":
        return f"latest_run_conclusion_{conclusion}"
    if run_status == "completed" and not conclusion:
        return "latest_run_missing_conclusion"
    if not run_status:
        return "latest_run_missing_status"
    return ""


def _unknown_run_reason(run: dict[str, Any]) -> str:
    if run.get("lookback_exhausted"):
        return "latest_completed_run_not_found"
    return ""


def _list_workflows(repo_path: str, token: str, timeout_seconds: float, deadline: float | None) -> list[dict[str, Any]]:
    workflows: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urlencode({"per_page": "100", "page": str(page)})
        payload = _github_request_json(f"{repo_path}/actions/workflows?{query}", token, timeout_seconds, deadline)
        items = payload.get("workflows", [])
        if not isinstance(items, list):
            return workflows
        workflows.extend(item for item in items if isinstance(item, dict))
        if len(items) < 100:
            return workflows
        page += 1


def _branch_scope(default_branch: str) -> str:
    raw = os.environ.get("CODEX_AUDIT_SERVICE_ORG_HEALTH_BRANCH", "").strip()
    if not raw or raw.lower() == "all":
        return ""
    if raw.lower() == "default":
        return default_branch
    return raw


def _run_summary(run: dict[str, Any], default_branch: str) -> dict[str, Any]:
    return {
        "name": str(run.get("name") or ""),
        "status": str(run.get("status") or ""),
        "conclusion": str(run.get("conclusion") or ""),
        "created_at": str(run.get("created_at") or ""),
        "url": str(run.get("html_url") or run.get("url") or ""),
        "branch": str(run.get("head_branch") or default_branch),
    }


def _classify_repo(latest_run: dict[str, Any], *, has_runs: bool, error: str = "") -> tuple[str, list[str]]:
    reasons: list[str] = []
    status = "degraded"
    run_status = latest_run.get("status", "")
    conclusion = latest_run.get("conclusion", "")

    if error:
        return "degraded", [error]
    if not has_runs:
        return "degraded", ["no_workflow_runs"]
    if run_status in _IN_PROGRESS_STATUSES:
        return "healthy", []
    if conclusion == "success":
        return "healthy", []
    if conclusion in _IGNORED_CONCLUSIONS:
        return "healthy", []
    if conclusion in _UNHEALTHY_CONCLUSIONS:
        return "unhealthy", [f"latest_run_conclusion_{conclusion}"]
    if conclusion in _DEGRADED_CONCLUSIONS or (conclusion and conclusion != "success"):
        return "degraded", [f"latest_run_conclusion_{conclusion}"]
    if run_status == "completed":
        return "degraded", ["latest_run_missing_conclusion"]
    if run_status:
        reasons.append(f"latest_run_status_{run_status}")
    else:
        reasons.append("latest_run_missing_status")
    return status, reasons


def _latest_run_for_repo(repo: str, token: str, timeout_seconds: float, deadline: float | None = None) -> tuple[dict[str, Any], list[str], str]:
    owner, name = repo.split("/", 1)
    repo_path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    try:
        repo_payload = _github_request_json(repo_path, token, timeout_seconds, deadline)
        default_branch = str(repo_payload.get("default_branch") or "main").strip() or "main"
        workflows = _list_workflows(repo_path, token, timeout_seconds, deadline)
    except (HTTPError, OSError, TimeoutError, URLError, ValueError, json.JSONDecodeError):
        return _empty_run(), ["github_api_error"], "degraded"

    if not workflows:
        empty = _empty_run()
        empty["branch"] = default_branch
        return empty, ["no_workflows"], "degraded"

    current_runs: list[dict[str, Any]] = []
    health_runs: list[dict[str, Any]] = []
    workflow_errors = 0
    branch_scope = _branch_scope(default_branch)
    branch_label = branch_scope or "all_branches"
    max_run_pages = _run_lookback_pages()
    for workflow in _monitored_workflows(workflows):
        workflow_id = str(workflow.get("id") or "").strip()
        if not workflow_id:
            continue
        fallback_run: dict[str, Any] | None = None
        added_health_run = False
        lookback_exhausted = False
        for page in range(1, max_run_pages + 1):
            query_params = {"per_page": str(DEFAULT_RUNS_PER_PAGE)}
            if page > 1:
                query_params["page"] = str(page)
            if branch_scope:
                query_params["branch"] = branch_scope
            query = urlencode(query_params)
            try:
                payload = _github_request_json(f"{repo_path}/actions/workflows/{quote(workflow_id, safe='')}/runs?{query}", token, timeout_seconds, deadline)
            except (HTTPError, OSError, TimeoutError, URLError, ValueError, json.JSONDecodeError):
                workflow_errors += 1
                break
            runs = payload.get("workflow_runs", [])
            if not isinstance(runs, list) or not runs:
                break
            summaries = [_run_summary(run, default_branch) for run in runs if isinstance(run, dict)]
            if not summaries:
                break
            if page == 1:
                fallback_run = summaries[0]
                current_runs.append(summaries[0])
            completed_run = next((run for run in summaries if run.get("status") == "completed"), None)
            if completed_run:
                health_runs.append(completed_run)
                added_health_run = True
                break
            if len(runs) < DEFAULT_RUNS_PER_PAGE:
                break
            if page == max_run_pages:
                lookback_exhausted = True
        if fallback_run and not added_health_run:
            if lookback_exhausted:
                fallback_run = {**fallback_run, "lookback_exhausted": True}
            health_runs.append(fallback_run)

    if not current_runs:
        empty = _empty_run()
        empty["branch"] = default_branch
        if workflow_errors:
            return empty, ["workflow_runs_api_error"], "degraded"
        return empty, [f"no_workflow_runs_on_{branch_label}"], "degraded"

    current_runs.sort(key=lambda run: run.get("created_at") or "", reverse=True)
    latest_run = current_runs[0]
    monitored_runs = health_runs
    monitored_failed = sum(1 for run in monitored_runs if run.get("conclusion") in _UNHEALTHY_CONCLUSIONS)
    monitored_unknown = sum(1 for run in monitored_runs if _unknown_run_reason(run))
    monitored_degraded = sum(
        1
        for run in monitored_runs
        if run.get("conclusion") not in _UNHEALTHY_CONCLUSIONS and not _unknown_run_reason(run) and _degraded_run_reason(run)
    )
    monitored_in_progress = sum(1 for run in current_runs if run.get("status") in _IN_PROGRESS_STATUSES)
    problem_run = next((run for run in monitored_runs if run.get("conclusion") in _UNHEALTHY_CONCLUSIONS), None)
    if not problem_run:
        problem_run = next((run for run in monitored_runs if _unknown_run_reason(run)), None)
    if not problem_run:
        problem_run = next((run for run in monitored_runs if _degraded_run_reason(run)), None)
    if not problem_run:
        problem_run = next((run for run in current_runs if run.get("status") in _IN_PROGRESS_STATUSES), None)
    latest_run["monitoring"] = {
        "current_failed": monitored_failed,
        "current_unknown": monitored_unknown,
        "current_degraded": monitored_degraded,
        "current_in_progress": monitored_in_progress,
    }
    if problem_run:
        latest_run["problem_run"] = problem_run
    status, reasons = _classify_repo(latest_run, has_runs=True)
    if monitored_failed:
        return latest_run, [f"latest_monitored_workflow_failure_on_{branch_label}"], "unhealthy"
    if monitored_unknown:
        return latest_run, [f"latest_completed_workflow_unknown_on_{branch_label}"], "unknown"
    if monitored_degraded:
        return latest_run, [f"latest_monitored_workflow_degraded_on_{branch_label}"], "degraded"
    if workflow_errors:
        return latest_run, ["partial_workflow_runs_api_error"], "degraded"
    return latest_run, reasons, status


def _cache_ttl_seconds() -> float:
    raw = os.environ.get("CODEX_AUDIT_SERVICE_ORG_HEALTH_CACHE_SECONDS", "120").strip()
    try:
        value = float(raw)
    except ValueError:
        return 60.0
    return max(0.0, value)


def _worker_limit() -> int:
    raw = os.environ.get("CODEX_AUDIT_SERVICE_ORG_HEALTH_WORKERS", str(DEFAULT_WORKER_LIMIT)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WORKER_LIMIT
    return min(max(1, value), 8)


def _workflow_limit() -> int:
    raw = os.environ.get("CODEX_AUDIT_SERVICE_ORG_HEALTH_MAX_WORKFLOWS_PER_REPO", str(DEFAULT_WORKFLOW_LIMIT)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WORKFLOW_LIMIT
    return min(max(1, value), 25)


def _workflow_allowlist() -> set[str]:
    raw = os.environ.get("CODEX_AUDIT_SERVICE_ORG_HEALTH_WORKFLOWS")
    if raw is None:
        values = list(DEFAULT_WORKFLOW_ALLOWLIST)
    else:
        stripped = raw.strip()
        if stripped.lower() in {"", "*", "all"}:
            return set()
        values = [item.strip() for item in stripped.replace("\n", ",").split(",") if item.strip()]
    return {item.lower() for item in values}


def _workflow_matches_allowlist(workflow: dict[str, Any], allowlist: set[str]) -> bool:
    if not allowlist:
        return True
    name = str(workflow.get("name") or "").strip().lower()
    path = str(workflow.get("path") or "").strip().lower()
    filename = path.rsplit("/", 1)[-1]
    return name in allowlist or path in allowlist or filename in allowlist


def _monitored_workflows(workflows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = [
        workflow
        for workflow in workflows
        if isinstance(workflow, dict) and not str(workflow.get("state") or "").startswith("disabled")
    ]
    allowlist = _workflow_allowlist()
    selected = [workflow for workflow in active if _workflow_matches_allowlist(workflow, allowlist)]
    if not selected and allowlist:
        selected = active
    return selected[:_workflow_limit()]


def _refresh_deadline_seconds() -> float:
    raw = os.environ.get("CODEX_AUDIT_SERVICE_ORG_HEALTH_REFRESH_DEADLINE_SECONDS", str(DEFAULT_REFRESH_DEADLINE_SECONDS)).strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_REFRESH_DEADLINE_SECONDS
    return min(max(1.0, value), 60.0)


def _cold_async_repository_threshold() -> int:
    raw = os.environ.get("CODEX_AUDIT_SERVICE_ORG_HEALTH_COLD_ASYNC_REPOSITORIES", str(DEFAULT_COLD_ASYNC_REPOSITORY_THRESHOLD)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_COLD_ASYNC_REPOSITORY_THRESHOLD
    return max(1, value)


def _should_return_cold_placeholder(repos: list[str], token: str, ttl_seconds: float) -> bool:
    return bool(token and ttl_seconds and len(repos) > _cold_async_repository_threshold())


def _refreshing_snapshot(repos: list[str], token_source: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "provider": {
            "status": "refreshing",
            "reason": "cold_cache_refreshing",
            "source": "github_rest",
            "token_source": token_source,
        },
        "summary": {
            "total_repositories": len(repos),
            "unhealthy_repositories": 0,
            "unknown_repositories": len(repos),
            "degraded_repositories": 0,
            "failed_workflow_runs": 0,
            "in_progress_workflow_runs": 0,
        },
        "repositories": [],
    }


def _run_lookback_pages() -> int:
    raw = os.environ.get("CODEX_AUDIT_SERVICE_ORG_HEALTH_RUN_LOOKBACK_PAGES", str(DEFAULT_RUN_LOOKBACK_PAGES)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RUN_LOOKBACK_PAGES
    return min(max(1, value), 10)


def _store_cache(cache_key: CacheKey, result: dict[str, Any], ttl_seconds: float) -> None:
    if not ttl_seconds:
        return
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.time(), result)
        event = _REFRESH_EVENTS.pop(cache_key, None)
        if event:
            event.set()


def _discard_refresh(cache_key: CacheKey, ttl_seconds: float) -> None:
    if not ttl_seconds:
        return
    with _CACHE_LOCK:
        event = _REFRESH_EVENTS.pop(cache_key, None)
        if event:
            event.set()


def _build_org_health_snapshot(
    repos: list[str],
    token: str,
    token_source: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    summary = {
        "total_repositories": len(repos),
        "unhealthy_repositories": 0,
        "unknown_repositories": 0,
        "degraded_repositories": 0,
        "failed_workflow_runs": 0,
        "in_progress_workflow_runs": 0,
    }
    if not token:
        return {
            "status": "unavailable",
            "provider": {"status": "unavailable", "reason": "needs_token", "source": "github_rest"},
            "summary": summary,
            "repositories": [],
        }

    deadline = time.monotonic() + _refresh_deadline_seconds()

    def read_repo(repo: str) -> dict[str, Any]:
        latest_run, reasons, repo_status = _latest_run_for_repo(repo, token, timeout_seconds, deadline)
        monitoring = latest_run.get("monitoring", {})
        problem_run = latest_run.get("problem_run", {})
        if isinstance(monitoring, dict):
            signals = {
                "current_failed_workflow_runs": int(monitoring.get("current_failed") or 0),
                "current_unknown_workflow_runs": int(monitoring.get("current_unknown") or 0),
                "current_degraded_workflow_runs": int(monitoring.get("current_degraded") or 0),
                "current_in_progress_workflow_runs": int(monitoring.get("current_in_progress") or 0),
            }
        else:
            signals = {
                "current_failed_workflow_runs": 0,
                "current_unknown_workflow_runs": 0,
                "current_degraded_workflow_runs": 0,
                "current_in_progress_workflow_runs": 0,
            }
        latest_run.pop("monitoring", None)
        latest_run.pop("problem_run", None)
        return {
            "repo": repo,
            "status": repo_status,
            "latest_run": latest_run,
            "problem_run": problem_run if isinstance(problem_run, dict) else {},
            "reasons": reasons,
            "signals": signals,
        }

    with ThreadPoolExecutor(max_workers=min(_worker_limit(), max(1, len(repos)))) as executor:
        repositories = list(executor.map(read_repo, repos))

    for repo_result in repositories:
        latest_run = repo_result["latest_run"]
        repo_status = repo_result["status"]
        conclusion = latest_run.get("conclusion", "")
        run_status = latest_run.get("status", "")
        if repo_status == "unhealthy":
            summary["unhealthy_repositories"] += 1
        elif repo_status == "unknown":
            summary["unknown_repositories"] += 1
        elif repo_status == "degraded":
            summary["degraded_repositories"] += 1
        signals = repo_result.get("signals", {})
        if isinstance(signals, dict):
            summary["failed_workflow_runs"] += int(signals.get("current_failed_workflow_runs") or 0)
            summary["in_progress_workflow_runs"] += int(signals.get("current_in_progress_workflow_runs") or 0)
        else:
            if conclusion in _UNHEALTHY_CONCLUSIONS:
                summary["failed_workflow_runs"] += 1
            if run_status in _IN_PROGRESS_STATUSES:
                summary["in_progress_workflow_runs"] += 1

    if summary["unhealthy_repositories"]:
        status = "unhealthy"
    elif summary["degraded_repositories"]:
        status = "degraded"
    elif summary["unknown_repositories"]:
        status = "unknown"
    else:
        status = "ok"
    return {
        "status": status,
        "provider": {
            "status": "available",
            "source": "github_rest",
            "token_source": token_source,
            "monitored_workflows": sorted(_workflow_allowlist()) or ["all"],
            "max_workflows_per_repo": _workflow_limit(),
        },
        "summary": summary,
        "repositories": repositories,
    }


def _start_background_refresh(
    cache_key: CacheKey,
    repos: list[str],
    token: str,
    token_source: str,
    ttl_seconds: float,
    timeout_seconds: float,
) -> None:
    def refresh() -> None:
        try:
            result = _build_org_health_snapshot(repos, token, token_source, timeout_seconds)
            _store_cache(cache_key, result, ttl_seconds)
        except Exception:
            _discard_refresh(cache_key, ttl_seconds)

    thread = threading.Thread(target=refresh, name="org-health-refresh", daemon=True)
    thread.start()


def read_org_health(timeout_seconds: float = 3.0) -> dict[str, Any]:
    """Return a GitHub Actions health snapshot for the configured repositories."""
    repos = _repository_targets()
    token, token_source = _github_token()
    branch_cache_scope = os.environ.get("CODEX_AUDIT_SERVICE_ORG_HEALTH_BRANCH", "").strip() or "all"
    cache_key = (tuple(repos), token_source, "token" if token else "no-token", branch_cache_scope)
    ttl_seconds = _cache_ttl_seconds()
    now = time.time()
    wait_for_refresh: threading.Event | None = None
    if ttl_seconds:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            refresh_event = _REFRESH_EVENTS.get(cache_key)
            if cached and now - cached[0] < ttl_seconds:
                return cached[1]
            if cached:
                if not refresh_event:
                    _REFRESH_EVENTS[cache_key] = threading.Event()
                    _start_background_refresh(cache_key, repos, token, token_source, ttl_seconds, timeout_seconds)
                return cached[1]
            if refresh_event:
                wait_for_refresh = refresh_event
            else:
                _REFRESH_EVENTS[cache_key] = threading.Event()
                if _should_return_cold_placeholder(repos, token, ttl_seconds):
                    _start_background_refresh(cache_key, repos, token, token_source, ttl_seconds, timeout_seconds)
                    return _refreshing_snapshot(repos, token_source)
    if wait_for_refresh:
        if _should_return_cold_placeholder(repos, token, ttl_seconds):
            return _refreshing_snapshot(repos, token_source)
        wait_for_refresh.wait(timeout=max(5.0, timeout_seconds * 3))
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached:
                return cached[1]
    try:
        result = _build_org_health_snapshot(repos, token, token_source, timeout_seconds)
    except Exception:
        _discard_refresh(cache_key, ttl_seconds)
        raise
    _store_cache(cache_key, result, ttl_seconds)
    return result
