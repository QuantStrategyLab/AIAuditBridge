#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
from string import Template
import tempfile
import time
from dataclasses import dataclass
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


API_BASE = "https://api.github.com"
ROOT = Path(__file__).resolve().parents[1]
PROMPT_TEMPLATES = {
    "monthly_snapshot_audit": ROOT / "prompts" / "monthly_crypto_snapshot_audit.md",
    "long_horizon_signal_shadow": ROOT / "prompts" / "long_horizon_signal_shadow.md",
}
DEFAULT_SOURCE_REPO = "QuantStrategyLab/CryptoLivePoolPipelines"
SOURCE_REPO_TASKS = {
    "QuantStrategyLab/CryptoLivePoolPipelines": frozenset({"monthly_snapshot_audit"}),
    "QuantStrategyLab/HkEquitySnapshotPipelines": frozenset({"monthly_snapshot_audit"}),
    "QuantStrategyLab/ResearchSignalContextPipelines": frozenset({"long_horizon_signal_shadow"}),
    "QuantStrategyLab/UsEquitySnapshotPipelines": frozenset({"monthly_snapshot_audit"}),
}
ALLOWED_SOURCE_REPOS = frozenset(SOURCE_REPO_TASKS)
REPO_TASKS = SOURCE_REPO_TASKS
DEFAULT_TASK = "monthly_snapshot_audit"
DEFAULT_MODE = "review_and_fix"
DEFAULT_PROVIDER = "auto"
API_PATCH_SYSTEM_PROMPT = (
    "You are CodexAuditBridge's API fallback patch provider. "
    "Return exactly one JSON object that matches the service patch contract. "
    "Do not wrap the JSON in markdown fences or add surrounding prose."
)
SUPPORTED_PROVIDERS = frozenset({"api", "anthropic", "codex", "openai", "auto"})
DEFAULT_CODEX_BACKEND = "service"
SUPPORTED_CODEX_BACKENDS = frozenset({"service"})
GUARDED_AUTO_MERGE_LABEL = "auto-merge-ok"
HUMAN_REVIEW_LABEL = "human-review-required"
GUARDED_AUTO_MERGE_LOW_RISK_PREFIXES = (
    "docs/",
    "tests/",
)
GUARDED_AUTO_MERGE_LOW_RISK_EXACT = {
    "README.md",
    "README.zh-CN.md",
}
GUARDED_AUTO_MERGE_MEDIUM_RISK_EXACT = {
    "scripts/build_monthly_live_strategy_health_reports.py",
    "scripts/run_monthly_report_bundle.py",
    "scripts/post_monthly_ai_review_issue.py",
    "scripts/post_codex_auto_merge_preflight_comment.py",
    "scripts/plan_codex_auto_merge_enablement.py",
}
DEFAULT_GUARDED_AUTO_MERGE_MAX_CHANGED_FILES = 20
DEFAULT_GUARDED_AUTO_MERGE_MAX_CHANGED_LINES = 1200
GUARDED_AUTO_MERGE_CONTROL_PLANE_EXACT = (
    ".github/codex_auto_merge_policy.json",
    "scripts/check_codex_auto_merge_readiness.py",
    "scripts/evaluate_codex_pr_merge.py",
    "scripts/post_codex_auto_merge_decision_comment.py",
    "scripts/sync_codex_auto_merge_labels.py",
)
GUARDED_AUTO_MERGE_CONTROL_PLANE_PREFIXES = (".github/workflows/",)
DEFAULT_GUARDED_AUTO_MERGE_POLICY = {
    "version": 1,
    "auto_merge_label": GUARDED_AUTO_MERGE_LABEL,
    "human_review_label": HUMAN_REVIEW_LABEL,
    "monthly_marker_prefix": "<!-- codex-monthly-remediation:issue-",
    "max_changed_files": DEFAULT_GUARDED_AUTO_MERGE_MAX_CHANGED_FILES,
    "max_changed_lines": DEFAULT_GUARDED_AUTO_MERGE_MAX_CHANGED_LINES,
    "blocked_path_patterns": [
        r"(^|/)(\.env|.*secret.*|.*credential.*|.*token.*|.*private.*|.*\.pem|.*\.key)$",
    ],
    "risk_policy": {
        "low": {
            "prefixes": list(GUARDED_AUTO_MERGE_LOW_RISK_PREFIXES),
            "exact": sorted(GUARDED_AUTO_MERGE_LOW_RISK_EXACT),
            "reason": "docs/tests/readme-only monthly-review surface",
        },
        "medium": {
            "exact": sorted(GUARDED_AUTO_MERGE_MEDIUM_RISK_EXACT),
            "reason": "monthly-review evidence/reporting helper changed",
        },
        "high": {"reason": "blocked/high-risk files require human review"},
    }
}
DEFAULT_SERVICE_AUDIENCE = "quant-codex-audit"
DEFAULT_SERVICE_CONTEXT_MAX_BYTES = 700_000
DEFAULT_SERVICE_CONTEXT_MAX_FILE_BYTES = 80_000
DEFAULT_SERVICE_MAX_CHANGES = 20
SERVICE_INFRA_FAILURE_EXIT_CODE = 75
SERVICE_INFRA_FAILURE_CATEGORIES = frozenset(
    {
        "auth_or_config_failure",
        "quota_or_capacity_failure",
        "transient_service_failure",
    }
)
SERVICE_CONTEXT_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".css",
        ".csv",
        ".gitignore",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsonl",
        ".md",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
SERVICE_CONTEXT_TEXT_NAMES = frozenset(
    {
        ".gitignore",
        "Dockerfile",
        "LICENSE",
        "Makefile",
        "README",
        "requirements.txt",
    }
)
SERVICE_CONTEXT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "dist",
        "node_modules",
        "venv",
    }
)
LONG_HORIZON_SIGNAL_OUTPUT_PREFIXES = (
    "data/output/latest_signal.",
    "data/output/signal_history/",
)
BLOCKED_PATH_RE = re.compile(
    r"(^|/)(\.env|.*secret.*|.*credential.*|.*token.*|.*private.*|.*\.pem|.*\.key)$",
    re.IGNORECASE,
)
class BridgeError(RuntimeError):
    pass


def classify_service_failure(error: str) -> str:
    text = error.lower()
    if any(word in text for word in ("permission", "unauth", "forbidden", "oidc", "token", "allow", "secret")):
        return "auth_or_config_failure"
    if any(word in text for word in ("quota", "rate limit", "too many active", "budget")):
        return "quota_or_capacity_failure"
    if any(word in text for word in ("timeout", "timed out", "temporarily", "unavailable", "connection", "network")):
        return "transient_service_failure"
    if any(word in text for word in ("json", "contract", "parse", "patch")):
        return "patch_contract_failure"
    return "unknown_failure"


def service_failure_category(message: str) -> str:
    match = re.search(r"\[([a-z_]+_failure)\]", message)
    if match:
        return match.group(1)
    return classify_service_failure(message)


def is_service_infrastructure_failure(message: str) -> bool:
    return service_failure_category(message) in SERVICE_INFRA_FAILURE_CATEGORIES


def service_infrastructure_failure_comment(reason: str) -> str:
    category = service_failure_category(reason)
    return "\n".join(
        [
            "## Codex Audit",
            "",
            "CodexAuditBridge stopped before making repository changes because the service backend failed outside the source PR/test surface.",
            "",
            f"- Failure category: `{category}`",
            f"- Detail: `{reason[:600]}`",
            "",
            "No files were pushed and no PR feedback retry is needed for this run.",
        ]
    )


class GitHubRequestError(BridgeError):
    def __init__(self, method: str, url: str, status_code: int, response_body: str) -> None:
        self.method = method
        self.url = url
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"GitHub API {method} {url} failed: {status_code} {response_body[:600]}")


def fail_closed_guarded_auto_merge_policy(reason: str) -> dict[str, Any]:
    return {
        "policy_errors": [reason],
        "blocked_path_patterns": [r".*"],
        "risk_policy": {
            "low": {"prefixes": [], "exact": [], "reason": reason},
            "medium": {"exact": [], "reason": reason},
            "high": {"reason": reason},
        },
    }


def valid_policy_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\n" not in value and "\r" not in value


def valid_policy_string_list(value: Any, *, allow_empty: bool = True) -> bool:
    return isinstance(value, list) and (allow_empty or bool(value)) and all(isinstance(item, str) for item in value)


def guarded_auto_merge_control_plane_matches(payload: dict[str, Any]) -> list[str]:
    risk_policy = payload["risk_policy"]
    low_policy = risk_policy["low"]
    medium_policy = risk_policy["medium"]
    auto_merge_matches: list[str] = []
    for raw_path in [*low_policy.get("exact", []), *medium_policy.get("exact", [])]:
        path = normalize_changed_path(raw_path)
        if path in GUARDED_AUTO_MERGE_CONTROL_PLANE_EXACT:
            auto_merge_matches.append(path)
        for prefix in GUARDED_AUTO_MERGE_CONTROL_PLANE_PREFIXES:
            if path.startswith(prefix):
                auto_merge_matches.append(f"{prefix}*")
    for raw_prefix in low_policy.get("prefixes", []):
        candidate_prefix = normalize_changed_path(raw_prefix)
        for path in GUARDED_AUTO_MERGE_CONTROL_PLANE_EXACT:
            if path.startswith(candidate_prefix):
                auto_merge_matches.append(path)
        for prefix in GUARDED_AUTO_MERGE_CONTROL_PLANE_PREFIXES:
            if prefix.startswith(candidate_prefix) or candidate_prefix.startswith(prefix):
                auto_merge_matches.append(f"{prefix}*")
    return sorted(set(auto_merge_matches))


def guarded_auto_merge_policy_schema_error(payload: dict[str, Any]) -> str | None:
    if "version" not in payload:
        return "invalid auto-merge policy schema requires human review"
    if payload.get("version") != 1:
        return "unsupported auto-merge policy version requires human review"
    if not valid_policy_string(payload.get("auto_merge_label")):
        return "invalid auto-merge policy schema requires human review"
    if not valid_policy_string(payload.get("human_review_label")):
        return "invalid auto-merge policy schema requires human review"
    if payload["auto_merge_label"].strip() == payload["human_review_label"].strip():
        return "auto-merge and human-review labels must be distinct requires human review"
    if not valid_policy_string(payload.get("monthly_marker_prefix")):
        return "invalid auto-merge policy schema requires human review"
    if type(payload.get("max_changed_files")) is not int or payload["max_changed_files"] < 1:
        return "invalid auto-merge policy schema requires human review"
    if type(payload.get("max_changed_lines")) is not int or payload["max_changed_lines"] < 1:
        return "invalid auto-merge policy schema requires human review"
    if not valid_policy_string_list(payload.get("blocked_path_patterns"), allow_empty=False):
        return "invalid auto-merge policy schema requires human review"
    risk_policy = payload.get("risk_policy")
    if not isinstance(risk_policy, dict):
        return "invalid auto-merge policy schema requires human review"
    low_policy = risk_policy.get("low")
    medium_policy = risk_policy.get("medium")
    high_policy = risk_policy.get("high")
    if not isinstance(low_policy, dict) or not isinstance(medium_policy, dict) or not isinstance(high_policy, dict):
        return "invalid auto-merge policy schema requires human review"
    if not valid_policy_string_list(low_policy.get("prefixes")):
        return "invalid auto-merge policy schema requires human review"
    if not valid_policy_string_list(low_policy.get("exact")):
        return "invalid auto-merge policy schema requires human review"
    if not valid_policy_string_list(medium_policy.get("exact")):
        return "invalid auto-merge policy schema requires human review"
    if not valid_policy_string(high_policy.get("reason")):
        return "invalid auto-merge policy schema requires human review"
    control_plane_matches = guarded_auto_merge_control_plane_matches(payload)
    if control_plane_matches:
        matches = ", ".join(control_plane_matches)
        return f"auto-merge policy must keep control-plane paths high-risk: {matches}"
    return None


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def split_csv_values(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\n,]", value) if item.strip()]


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def api_fallback_allow_fix() -> bool:
    return parse_bool(env_value("CODEX_AUDIT_API_FALLBACK_ALLOW_FIX", "true"))


def api_fallback_provider_order() -> list[str]:
    configured = env_value("CODEX_AUDIT_API_FALLBACK_PROVIDER_ORDER", "openai,anthropic")
    order = [item.strip().lower() for item in configured.replace("\n", ",").split(",") if item.strip()]
    return [provider for provider in order if provider in {"openai", "anthropic"}]


def validate_repo(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise BridgeError(f"Invalid repository name: {repo!r}")
    if repo not in ALLOWED_SOURCE_REPOS:
        raise BridgeError(f"Unsupported source repository: {repo!r}")
    return repo


def validate_task(task: str, source_repo: str) -> str:
    normalized = (task or DEFAULT_TASK).strip().lower().replace("-", "_")
    if normalized not in PROMPT_TEMPLATES:
        raise BridgeError(f"Unsupported CODEX_AUDIT_TASK: {task!r}")
    allowed = REPO_TASKS.get(source_repo, frozenset())
    if normalized not in allowed:
        raise BridgeError(f"Task {normalized!r} is not allowed for {source_repo}")
    return normalized


def validate_provider(provider: str) -> str:
    normalized = (provider or DEFAULT_PROVIDER).strip().lower()
    if normalized not in SUPPORTED_PROVIDERS:
        raise BridgeError(f"Unsupported CODEX_AUDIT_PROVIDER: {provider!r}")
    return normalized


def api_fallback_allowed_source_repos() -> frozenset[str]:
    configured = env_value("CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES")
    if not configured:
        raise BridgeError(
            "CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES must explicitly list "
            "source repositories approved for API fallback"
        )
    repos = frozenset(split_csv_values(configured))
    unsupported = sorted(repos - ALLOWED_SOURCE_REPOS)
    if unsupported:
        raise BridgeError(
            "CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES includes unsupported repositories: "
            + ", ".join(unsupported)
        )
    return repos


def validate_api_fallback_source_repo(source_repo: str) -> str:
    allowed = api_fallback_allowed_source_repos()
    if source_repo not in allowed:
        raise BridgeError(f"API fallback is not allowed for source repository: {source_repo}")
    return source_repo


def validate_codex_backend(backend: str) -> str:
    normalized = (backend or DEFAULT_CODEX_BACKEND).strip().lower()
    if normalized not in SUPPORTED_CODEX_BACKENDS:
        raise BridgeError(f"Unsupported CODEX_AUDIT_CODEX_BACKEND: {backend!r}")
    return normalized


def safe_branch_component(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    value = re.sub(r"-{2,}", "-", value).strip("-._")
    return value[:80] or "monthly-review"


def github_request(
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    url = path if path.startswith("https://") else f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-audit-bridge",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GitHubRequestError(method, url, exc.code, body) from exc
    return json.loads(body) if body else {}


def fetch_issue_comments(
    token: str,
    source_repo: str,
    issue_number: int,
    *,
    per_page: int = 100,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = github_request(
            token,
            "GET",
            f"/repos/{source_repo}/issues/{issue_number}/comments?per_page={per_page}&page={page}",
        )
        if not isinstance(payload, list):
            break
        comments.extend(comment for comment in payload if isinstance(comment, dict))
        if len(payload) < per_page:
            break
    return comments


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    printable = " ".join(shlex.quote(part) for part in command)
    print(f"+ {printable}", flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def run_checked(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int | None = None,
) -> str:
    result = run(command, cwd=cwd, env=env, input_text=input_text, timeout=timeout)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.returncode != 0:
        printable = " ".join(shlex.quote(part) for part in command)
        output = (result.stdout or "").strip()
        if len(output) > 1200:
            output = f"...\n{output[-1200:]}"
        detail = f"Command failed with exit code {result.returncode}: {printable}"
        if output:
            detail = f"{detail}\n{output}"
        raise BridgeError(detail)
    return result.stdout


def git_auth_env(token: str) -> dict[str, str]:
    encoded = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
        }
    )
    return env


def git_with_token(repo_dir: Path, token: str, args: list[str]) -> str:
    return run_checked(["git", *args], cwd=repo_dir, env=git_auth_env(token))


def resolve_source_repo_token(source_repo: str) -> str:
    token = env_value("CODEX_AUDIT_GH_TOKEN") or env_value("GH_TOKEN")
    if token:
        return token

    github_token = env_value("GITHUB_TOKEN")
    if github_token and env_value("GITHUB_REPOSITORY") == source_repo:
        return github_token

    raise BridgeError(
        "CODEX_AUDIT_GH_TOKEN or GH_TOKEN with access to the source repository is required. "
        "The workflow GITHUB_TOKEN is only valid when the bridge runs inside the source repository."
    )


def clone_source_repo(token: str, source_repo: str, source_ref: str, work_root: Path) -> Path:
    repo_dir = work_root / "source"
    clone_url = f"https://github.com/{source_repo}.git"
    run_checked(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            source_ref,
            clone_url,
            str(repo_dir),
        ],
        env=git_auth_env(token),
    )
    return repo_dir


def write_codex_context(
    repo_dir: Path,
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
) -> tuple[Path, Path]:
    context_dir = repo_dir / ".codex-audit"
    context_dir.mkdir(parents=True, exist_ok=True)
    exclude_path = repo_dir / ".git" / "info" / "exclude"
    with exclude_path.open("a", encoding="utf-8") as handle:
        handle.write("\n.codex-audit/\n")

    issue_path = context_dir / "monthly_issue.md"
    recent_comments = comments[-20:]
    comments_md = "\n\n".join(
        f"### Comment by {comment.get('user', {}).get('login', 'unknown')}\n\n{comment.get('body') or ''}"
        for comment in recent_comments
    )
    issue_path.write_text(
        "\n".join(
            [
                f"# {issue.get('title', 'Monthly review issue')}",
                "",
                f"- Repository: {source_repo}",
                f"- Source ref: {source_ref}",
                f"- Issue URL: {issue.get('html_url', '')}",
                "",
                "## Body",
                "",
                issue.get("body") or "",
                "",
                "## Existing Comments",
                "",
                comments_md or "None",
                "",
            ]
        ),
        encoding="utf-8",
    )
    context_path = context_dir / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "source_repo": source_repo,
                "source_ref": source_ref,
                "issue": {
                    "number": issue.get("number"),
                    "title": issue.get("title"),
                    "html_url": issue.get("html_url"),
                    "labels": [label.get("name") for label in issue.get("labels", [])],
                },
                "comment_count": len(comments),
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return issue_path, context_path


def build_prompt(
    *,
    task: str,
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    issue_path: Path,
    context_path: Path,
    mode: str,
) -> str:
    template_path = PROMPT_TEMPLATES[task]
    template = Template(template_path.read_text(encoding="utf-8"))
    return template.safe_substitute(
        TASK=task,
        SOURCE_REPO=source_repo,
        SOURCE_REF=source_ref,
        ISSUE_URL=issue.get("html_url", ""),
        ISSUE_NUMBER=str(issue.get("number", "")),
        MODE=mode,
        ISSUE_MARKDOWN_PATH=str(issue_path),
        CONTEXT_JSON_PATH=str(context_path),
    )


def int_env(name: str, default: int) -> int:
    raw = env_value(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise BridgeError(f"{name} must be an integer") from exc
    if value < 1:
        raise BridgeError(f"{name} must be positive")
    return value


def service_context_path_allowed(path: str, *, task: str) -> bool:
    if not path or "\\" in path:
        return False
    posix_path = PurePosixPath(path)
    if posix_path.is_absolute() or any(part in {"", ".", ".."} for part in posix_path.parts):
        return False
    if any(part in SERVICE_CONTEXT_EXCLUDED_DIRS for part in posix_path.parts):
        return False
    if BLOCKED_PATH_RE.search(path):
        return False
    if path.startswith("data/") and not (
        task == "long_horizon_signal_shadow" and long_horizon_signal_data_path_allowed(path)
    ):
        return False
    suffix = posix_path.suffix.lower()
    name = posix_path.name
    return suffix in SERVICE_CONTEXT_TEXT_SUFFIXES or name in SERVICE_CONTEXT_TEXT_NAMES


def service_context_file_paths(repo_dir: Path, *, task: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add_path(path: Path) -> None:
        if not path.is_file():
            return
        try:
            rel = path.relative_to(repo_dir).as_posix()
        except ValueError:
            return
        if rel not in seen and service_context_path_allowed(rel, task=task):
            seen.add(rel)
            candidates.append(rel)

    for path in sorted((repo_dir / ".codex-audit").glob("*")):
        add_path(path)

    result = run(["git", "ls-files", "-co", "--exclude-standard"], cwd=repo_dir, timeout=30)
    if result.returncode == 0:
        for rel in sorted(line.strip() for line in (result.stdout or "").splitlines() if line.strip()):
            add_path(repo_dir / rel)
    else:
        for path in sorted(item for item in repo_dir.rglob("*") if item.is_file()):
            add_path(path)

    return candidates


def build_service_repository_context(
    repo_dir: Path,
    *,
    task: str,
    max_bytes: int | None = None,
    max_file_bytes: int | None = None,
) -> str:
    max_bytes = max_bytes or int_env("CODEX_AUDIT_SERVICE_CONTEXT_MAX_BYTES", DEFAULT_SERVICE_CONTEXT_MAX_BYTES)
    max_file_bytes = max_file_bytes or int_env(
        "CODEX_AUDIT_SERVICE_CONTEXT_MAX_FILE_BYTES",
        DEFAULT_SERVICE_CONTEXT_MAX_FILE_BYTES,
    )
    parts = [
        "## Repository context snapshot",
        "",
        "The service backend cannot access the source checkout directly. The following bounded text snapshot is provided "
        "by CodexAuditBridge after path filtering.",
        "",
    ]
    total = len("\n".join(parts).encode("utf-8"))
    omitted = 0
    for rel in service_context_file_paths(repo_dir, task=task):
        path = repo_dir / rel
        try:
            content_bytes = path.read_bytes()
        except OSError:
            omitted += 1
            continue
        if len(content_bytes) > max_file_bytes or b"\x00" in content_bytes:
            omitted += 1
            continue
        content = content_bytes.decode("utf-8", errors="replace").rstrip()
        block = f'<context path="{rel}">\n{content}\n</context>\n\n'
        block_bytes = len(block.encode("utf-8"))
        if total + block_bytes > max_bytes:
            omitted += 1
            continue
        parts.append(block)
        total += block_bytes
    if omitted:
        note = f"\n{omitted} files were omitted by service context size, binary, or path filters.\n"
        if total + len(note.encode("utf-8")) <= max_bytes:
            parts.append(note)
    return "\n".join(parts).rstrip() + "\n"


def service_patch_contract_instructions() -> str:
    return "\n".join(
        [
            "## Service patch contract",
            "",
            "You are running behind CodexAuditBridge's service backend. You cannot edit the checkout directly.",
            "For review_and_fix mode, return exactly one JSON object and no surrounding prose:",
            "",
            "```json",
            "{",
            '  "final_message": "Markdown summary for the issue comment or PR body.",',
            '  "changes": [',
            '    {"path": "relative/file/path.py", "content": "complete UTF-8 file contents"}',
            "  ]",
            "}",
            "```",
            "",
            "Rules:",
            "- `path` must be a repository-relative POSIX path.",
            "- Return complete file contents, not a diff.",
            "- Do not include secrets, credentials, tokens, private keys, or .env files.",
            "- Do not write under `.git/`.",
            "- Avoid data/output edits unless the task explicitly asks for an allowed long-horizon shadow signal output.",
            "- If no safe edit is needed, return an empty `changes` array and explain why in `final_message`.",
        ]
    )


def build_service_prompt(repo_dir: Path, prompt: str, *, task: str, mode: str) -> str:
    parts = [
        prompt.rstrip(),
        "",
        build_service_repository_context(repo_dir, task=task).rstrip(),
    ]
    if mode == "review_and_fix":
        parts.extend(["", service_patch_contract_instructions()])
    return "\n".join(parts).rstrip() + "\n"


def normalize_codex_service_url(raw_url: str) -> str:
    raw_url = raw_url.strip().rstrip("/")
    if not raw_url:
        raise BridgeError("CODEX_AUDIT_SERVICE_URL is required when CODEX_AUDIT_CODEX_BACKEND=service")
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BridgeError("CODEX_AUDIT_SERVICE_URL must be an absolute HTTP(S) URL")
    if parsed.scheme != "https":
        host = parsed.hostname or ""
        local_host = host in {"127.0.0.1", "::1", "localhost"}
        if not (local_host and parse_bool(env_value("CODEX_AUDIT_ALLOW_INSECURE_SERVICE_URL"))):
            raise BridgeError("CODEX_AUDIT_SERVICE_URL must use HTTPS outside explicit local testing")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1/codex-audit"
    elif not path.endswith("/v1/codex-audit"):
        path = f"{path}/v1/codex-audit"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def codex_service_jobs_url(service_url: str) -> str:
    return service_url.rstrip("/") + "/jobs"


def codex_service_job_url(service_url: str, job_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,96}", job_id):
        raise BridgeError("Codex audit service returned an invalid job id")
    return service_url.rstrip("/") + f"/jobs/{job_id}"


def request_github_oidc_token(audience: str) -> str:
    request_url = env_value("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = env_value("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not request_url or not request_token:
        raise BridgeError(
            "GitHub Actions OIDC token request environment is unavailable. "
            "Set workflow `permissions: id-token: write` and run from an allowed workflow/ref."
        )
    separator = "&" if "?" in request_url else "?"
    url = f"{request_url}{separator}audience={urllib.parse.quote(audience)}"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {request_token}",
            "Accept": "application/json",
            "User-Agent": "codex-audit-bridge-oidc",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BridgeError(f"GitHub OIDC token request failed: {exc.code} {detail[:600]}") from exc
    payload = json.loads(body)
    token = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise BridgeError("GitHub OIDC token response did not include a token value")
    return token


def request_codex_service_json(
    *,
    method: str,
    url: str,
    audience: str,
    payload: dict[str, object] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    token = request_github_oidc_token(audience)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "codex-audit-bridge-service-client",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BridgeError(f"Codex audit service request failed: {exc.code} {detail[:600]}") from exc
    response_payload = json.loads(body)
    if not isinstance(response_payload, dict):
        raise BridgeError("Codex audit service returned an invalid JSON response")
    return response_payload


def request_codex_service(
    *,
    source_repo: str,
    source_ref: str,
    task: str,
    mode: str,
    prompt: str,
    timeout_minutes: int,
    issue_number: int | None = None,
) -> str:
    audience = env_value("CODEX_AUDIT_SERVICE_AUDIENCE", DEFAULT_SERVICE_AUDIENCE)
    service_url = normalize_codex_service_url(env_value("CODEX_AUDIT_SERVICE_URL"))
    payload = {
        "source_repository": source_repo,
        "source_ref": source_ref,
        "task": task,
        "mode": mode,
        "prompt": prompt,
        "timeout_seconds": timeout_minutes * 60,
    }
    if issue_number is not None:
        payload["issue_number"] = issue_number
    submit_payload = request_codex_service_json(
        method="POST",
        url=codex_service_jobs_url(service_url),
        audience=audience,
        payload=payload,
        timeout_seconds=60,
    )
    if submit_payload.get("status") not in {"queued", "running"}:
        raise BridgeError("Codex audit service did not accept the async job")
    job_id = submit_payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise BridgeError("Codex audit service did not return a job id")

    deadline = time.time() + timeout_minutes * 60 + 60
    poll_interval = max(2, int_env("CODEX_AUDIT_SERVICE_POLL_INTERVAL_SECONDS", 10))
    job_url = codex_service_job_url(service_url, job_id)
    while time.time() < deadline:
        time.sleep(poll_interval)
        job_payload = request_codex_service_json(
            method="GET",
            url=job_url,
            audience=audience,
            timeout_seconds=60,
        )
        status = job_payload.get("status")
        if status == "succeeded":
            output = job_payload.get("output")
            if not isinstance(output, str):
                raise BridgeError("Codex audit service job response did not include text output")
            return output.strip()
        if status == "failed":
            error = str(job_payload.get("error") or "unknown service job failure")
            category = str(job_payload.get("failure_category") or classify_service_failure(error))
            raise BridgeError(f"Codex audit service job failed [{category}]: {error[:600]}")
        if status not in {"queued", "running"}:
            raise BridgeError(f"Codex audit service job returned unexpected status: {status!r}")
    raise BridgeError("Codex audit service job timed out before completion")


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        stripped = fence.group(1).strip()
    if not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            stripped = stripped[start : end + 1]
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise BridgeError("Service patch response must be a JSON object")
    return payload


def parse_service_patch_response(text: str) -> tuple[str, list[dict[str, str]]]:
    payload = extract_json_object(text)
    final_message = payload.get("final_message", "")
    if final_message is None:
        final_message = ""
    if not isinstance(final_message, str):
        raise BridgeError("Service patch response `final_message` must be a string")
    changes_raw = payload.get("changes")
    if not isinstance(changes_raw, list):
        raise BridgeError("Service patch response `changes` must be a list")
    changes: list[dict[str, str]] = []
    for index, item in enumerate(changes_raw):
        if not isinstance(item, dict):
            raise BridgeError(f"Service patch response change #{index + 1} must be an object")
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not path.strip():
            raise BridgeError(f"Service patch response change #{index + 1} has an invalid path")
        if not isinstance(content, str):
            raise BridgeError(f"Service patch response change #{index + 1} content must be a string")
        changes.append({"path": path.strip(), "content": content})
    return final_message.strip(), changes


def validate_service_change_path(path: str) -> str:
    if "\\" in path:
        raise BridgeError(f"Service patch path must use POSIX separators: {path!r}")
    posix_path = PurePosixPath(path)
    if posix_path.is_absolute() or any(part in {"", ".", ".."} for part in posix_path.parts):
        raise BridgeError(f"Service patch path must be repository-relative: {path!r}")
    if any(part == ".git" for part in posix_path.parts):
        raise BridgeError(f"Service patch path may not target .git: {path!r}")
    return posix_path.as_posix()


def apply_service_changes(repo_dir: Path, changes: list[dict[str, str]], *, task: str) -> list[str]:
    max_changes = int_env("CODEX_AUDIT_SERVICE_MAX_CHANGES", DEFAULT_SERVICE_MAX_CHANGES)
    if len(changes) > max_changes:
        raise BridgeError(f"Service patch contains {len(changes)} changes; limit is {max_changes}")

    validated_paths = [validate_service_change_path(change["path"]) for change in changes]
    denied = blocked_paths(validated_paths, task=task)
    if denied:
        denied_list = ", ".join(denied)
        raise BridgeError(f"Service patch includes blocked paths: {denied_list}")

    repo_root = repo_dir.resolve()
    for change, rel_path in zip(changes, validated_paths, strict=True):
        target = (repo_root / rel_path).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError as exc:
            raise BridgeError(f"Service patch path escapes the repository: {rel_path!r}") from exc
        if target.exists() and target.is_dir():
            raise BridgeError(f"Service patch cannot replace a directory: {rel_path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change["content"], encoding="utf-8")
    return validated_paths


def run_codex_service(
    repo_dir: Path,
    prompt: str,
    timeout_minutes: int,
    *,
    source_repo: str,
    source_ref: str,
    task: str,
    mode: str,
    issue_number: int | None = None,
) -> tuple[int, str, str]:
    try:
        service_prompt = build_service_prompt(repo_dir, prompt, task=task, mode=mode)
        output = request_codex_service(
            source_repo=source_repo,
            source_ref=source_ref,
            task=task,
            mode=mode,
            prompt=service_prompt,
            timeout_minutes=timeout_minutes,
            issue_number=issue_number,
        )
        final_message = output
        if mode == "review_and_fix":
            final_message, changes = parse_service_patch_response(output)
            apply_service_changes(repo_dir, changes, task=task)
        output_path = repo_dir / ".codex-audit" / "codex-final-message.md"
        output_path.write_text(final_message.rstrip() + "\n", encoding="utf-8")
        return 0, output, final_message.strip()
    except (BridgeError, OSError, json.JSONDecodeError, urllib.error.URLError) as exc:
        message = str(exc)
        code = SERVICE_INFRA_FAILURE_EXIT_CODE if is_service_infrastructure_failure(message) else 1
        return code, message, ""


def run_codex_backend(
    repo_dir: Path,
    prompt: str,
    timeout_minutes: int,
    *,
    backend: str,
    source_repo: str,
    source_ref: str,
    task: str,
    mode: str,
    issue_number: int | None = None,
) -> tuple[int, str, str]:
    if backend == "service":
        return run_codex_service(
            repo_dir,
            prompt,
            timeout_minutes,
            source_repo=source_repo,
            source_ref=source_ref,
            task=task,
            mode=mode,
            issue_number=issue_number,
        )
    raise BridgeError(f"Unsupported Codex backend: {backend!r}")


def build_api_review_prompt(
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    *,
    task: str = DEFAULT_TASK,
) -> str:
    recent_comments = comments[-20:]
    comments_md = "\n\n".join(
        f"### Comment by {comment.get('user', {}).get('login', 'unknown')}\n\n{comment.get('body') or ''}"
        for comment in recent_comments
    )
    if task == "long_horizon_signal_shadow":
        return "\n".join(
            [
                "You are reviewing a long-horizon AI shadow signal issue for a QuantStrategyLab research repository.",
                "Return a concise markdown review plus a draft JSON signal block if the evidence is sufficient.",
                "Do not claim to have edited files or placed trades.",
                "The signal must be shadow-only, non-execution, and bounded by deterministic downstream policy.",
                "",
                "## Source",
                "",
                f"- Repository: {source_repo}",
                f"- Ref: {source_ref}",
                f"- Issue: {issue.get('html_url', '')}",
                "",
                "## Issue Title",
                "",
                issue.get("title") or "",
                "",
                "## Issue Body",
                "",
                truncate_markdown(issue.get("body") or "", 18000),
                "",
                "## Existing Comments",
                "",
                truncate_markdown(comments_md or "None", 6000),
                "",
                "## Output Format",
                "",
                "## API Long-Horizon Shadow Signal Review",
                "",
                "### Evidence Quality",
                "### Draft Shadow Signal JSON",
                "### Missing Data",
                "### Operator Action Items",
            ]
        )
    return "\n".join(
        [
            "You are reviewing a monthly snapshot report issue for a QuantStrategyLab source repository.",
            "Return a concise bilingual markdown review. Do not claim to have changed files.",
            "Focus on release consistency, evidence gaps, downstream impact, and low-risk follow-up actions.",
            "Do not recommend production strategy changes from one monthly snapshot alone.",
            "If the source repository is QuantStrategyLab/HkEquitySnapshotPipelines, pay special attention to "
            "HK snapshot promotion gates for hk_low_vol_dividend_quality, hk_shareholder_yield_quality, and "
            "hk_free_cash_flow_quality: point-in-time inputs, no look-ahead or survivorship bias, at least "
            "three OOS folds, max drawdown <= 30%, HK costs/slippage/lot-size/capacity, artifact provenance, "
            "dry-run order preview, bilingual notification evidence, and operator approval.",
            "",
            "## Source",
            "",
            f"- Repository: {source_repo}",
            f"- Ref: {source_ref}",
            f"- Issue: {issue.get('html_url', '')}",
            "",
            "## Issue Title",
            "",
            issue.get("title") or "",
            "",
            "## Issue Body",
            "",
            truncate_markdown(issue.get("body") or "", 18000),
            "",
            "## Existing Comments",
            "",
            truncate_markdown(comments_md or "None", 6000),
            "",
            "## Output Format",
            "",
            "## API Monthly Review",
            "",
            "### English",
            "",
            "#### Release Consistency",
            "#### Evidence Gaps",
            "#### Downstream Impact",
            "#### Operator Action Items",
            "",
            "### 中文",
            "",
            "#### 发布一致性",
            "#### 证据缺口",
            "#### 下游影响",
            "#### 操作员待办事项",
        ]
    )


def extract_openai_text(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise BridgeError("OpenAI response did not include choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    raise BridgeError("OpenAI response did not include text content")


def extract_anthropic_text(response: dict[str, Any]) -> str:
    content = response.get("content")
    if not isinstance(content, list):
        raise BridgeError("Anthropic response did not include content")
    text_parts = [
        str(block.get("text", "")).strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).strip()
    ]
    if text_parts:
        return "\n\n".join(text_parts)
    raise BridgeError("Anthropic response did not include text content")


def request_openai_completion(*, system: str, user: str) -> str:
    api_key = env_value("OPENAI_API_KEY")
    if not api_key:
        raise BridgeError("OPENAI_API_KEY is required for OpenAI API review")
    model = env_value("OPENAI_MODEL", "gpt-5.4-mini")
    base_url = env_value("OPENAI_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-audit-bridge-openai",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BridgeError(f"OpenAI API request failed: {exc.code} {detail[:600]}") from exc
    return extract_openai_text(json.loads(body))


def request_anthropic_completion(*, system: str, user: str) -> str:
    api_key = env_value("ANTHROPIC_API_KEY")
    if not api_key:
        raise BridgeError("ANTHROPIC_API_KEY is required for CODEX_AUDIT_PROVIDER=anthropic or API fallback")
    model = env_value("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    base_url = env_value("ANTHROPIC_API_BASE_URL", "https://api.anthropic.com/v1").rstrip("/")
    api_version = env_value("ANTHROPIC_VERSION", "2023-06-01")
    payload = {
        "model": model,
        "max_tokens": int(env_value("ANTHROPIC_MAX_TOKENS", "4000")),
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    request = urllib.request.Request(
        f"{base_url}/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": api_version,
            "Content-Type": "application/json",
            "User-Agent": "codex-audit-bridge-anthropic",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BridgeError(f"Anthropic API request failed: {exc.code} {detail[:600]}") from exc
    return extract_anthropic_text(json.loads(body))


def run_openai_review(source_repo: str, source_ref: str, issue: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    return request_openai_completion(
        system="You are a careful repository release reviewer. Return only markdown.",
        user=build_api_review_prompt(
            source_repo,
            source_ref,
            issue,
            comments,
            task=env_value("CODEX_AUDIT_TASK", DEFAULT_TASK),
        ),
    )


def run_anthropic_review(source_repo: str, source_ref: str, issue: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    return request_anthropic_completion(
        system="You are a careful repository release reviewer. Return only markdown.",
        user=build_api_review_prompt(
            source_repo,
            source_ref,
            issue,
            comments,
            task=env_value("CODEX_AUDIT_TASK", DEFAULT_TASK),
        ),
    )


def auto_fallback_missing_api_key_message(reason: str) -> str:
    return "\n".join(
        [
            "## Codex Audit",
            "",
            reason,
            "",
            "API review was requested, but no fallback API keys are configured in the bridge repository.",
            "",
            "No files were pushed. Configure `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY`, or inspect the bridge workflow logs.",
        ]
    )


def run_configured_api_reviews(
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
) -> tuple[list[tuple[str, str]], list[str]]:
    reviewers: list[tuple[str, str, Any]] = [
        ("OpenAI", "OPENAI_API_KEY", run_openai_review),
        ("Anthropic Claude", "ANTHROPIC_API_KEY", run_anthropic_review),
    ]
    reviews: list[tuple[str, str]] = []
    warnings: list[str] = []
    for label, secret_name, runner in reviewers:
        if not env_value(secret_name):
            warnings.append(f"{label} fallback skipped because `{secret_name}` is not configured.")
            continue
        try:
            reviews.append((label, runner(source_repo, source_ref, issue, comments)))
        except BridgeError as exc:
            warnings.append(f"{label} fallback failed: `{exc}`")
    return reviews, warnings


def format_api_review_comment(reason: str, reviews: list[tuple[str, str]], warnings: list[str]) -> str:
    lines = [
        "## API Monthly Review",
        "",
        reason,
    ]
    for label, review in reviews:
        lines.extend(
            [
                "",
                f"### {label} Review",
                "",
                truncate_markdown(review, 8000),
            ]
        )
    if warnings:
        lines.extend(["", "### Fallback Warnings"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


def run_api_patch_provider(
    repo_dir: Path,
    prompt: str,
    *,
    task: str,
    mode: str,
    provider: str,
) -> tuple[int, str, str]:
    if mode != "review_and_fix":
        raise BridgeError("API patch provider requires review_and_fix mode")
    if provider not in {"openai", "anthropic"}:
        raise BridgeError(f"Unsupported API patch provider: {provider!r}")
    service_prompt = build_service_prompt(repo_dir, prompt, task=task, mode=mode)
    try:
        if provider == "openai":
            output = request_openai_completion(system=API_PATCH_SYSTEM_PROMPT, user=service_prompt)
        else:
            output = request_anthropic_completion(system=API_PATCH_SYSTEM_PROMPT, user=service_prompt)
        final_message, changes = parse_service_patch_response(output)
        if changes:
            apply_service_changes(repo_dir, changes, task=task)
        output_path = repo_dir / ".codex-audit" / "api-patch-final-message.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(final_message.rstrip() + "\n", encoding="utf-8")
        return 0, output, final_message.strip()
    except (BridgeError, json.JSONDecodeError, OSError) as exc:
        return 1, str(exc), ""


@dataclass(frozen=True)
class RemediationWorkspace:
    repo_dir: Path
    branch_name: str
    baseline_auto_merge_policy: dict[str, Any]
    feedback_retry_pr: dict[str, Any] | None
    stale_auto_merge_label: str
    stale_auto_merge_label_skip_reason: str
    stale_auto_merge_label_removed: bool
    prompt: str


def stale_auto_merge_cleanup_failure_comment(
    feedback_retry_pr: dict[str, Any],
    stale_auto_merge_label: str,
    exc: Exception,
) -> str:
    return "\n".join(
        [
            "## Codex Audit",
            "",
            "The bridge refused to continue because it could not clear a stale guarded "
            "auto-merge label from the existing fix PR.",
            "",
            f"- PR: #{feedback_retry_pr['number']}",
            f"- Label: `{stale_auto_merge_label}`",
            f"- Error: `{exc}`",
            "",
            "No files were pushed. Please remove the stale label or inspect the PR manually.",
        ]
    )


def prepare_remediation_workspace(
    token: str,
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    issue_number: int,
    task: str,
    mode: str,
    work_root: Path,
) -> RemediationWorkspace:
    repo_dir = clone_source_repo(token, source_repo, source_ref, work_root)
    baseline_auto_merge_policy = load_guarded_auto_merge_policy(
        repo_dir / ".github" / "codex_auto_merge_policy.json"
    )
    feedback_retry_pr = resolve_feedback_retry_pr(
        token,
        source_repo,
        issue_number,
        source_ref,
        comments,
        task=task,
    )
    stale_auto_merge_label, stale_auto_merge_label_skip_reason = guarded_auto_merge_label_for_mutation(
        baseline_auto_merge_policy
    )
    stale_auto_merge_label_removed = False
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    branch_prefix = "long-horizon-signal" if task == "long_horizon_signal_shadow" else "monthly-review"
    if feedback_retry_pr:
        if stale_auto_merge_label:
            try:
                stale_auto_merge_label_removed = remove_issue_label_if_present(
                    token,
                    source_repo,
                    int(feedback_retry_pr["number"]),
                    stale_auto_merge_label,
                )
            except (BridgeError, GitHubRequestError) as exc:
                raise BridgeError(
                    stale_auto_merge_cleanup_failure_comment(feedback_retry_pr, stale_auto_merge_label, exc)
                ) from exc
        else:
            print(stale_auto_merge_label_skip_message(stale_auto_merge_label_skip_reason), flush=True)
        branch_name = str(feedback_retry_pr["head_ref"])
        checkout_feedback_retry_branch(repo_dir, branch_name)
        print(
            f"Updating existing Codex remediation PR #{feedback_retry_pr['number']} on branch {branch_name}.",
            flush=True,
        )
    else:
        branch_name = f"codex/{branch_prefix}-issue-{issue_number}-{stamp}"
        run_checked(["git", "checkout", "-b", branch_name], cwd=repo_dir)
    run_checked(["git", "config", "user.name", "codex-audit-bridge[bot]"], cwd=repo_dir)
    run_checked(
        ["git", "config", "user.email", "codex-audit-bridge[bot]@users.noreply.github.com"],
        cwd=repo_dir,
    )
    issue_path, context_path = write_codex_context(repo_dir, source_repo, source_ref, issue, comments)
    prompt = build_prompt(
        task=task,
        source_repo=source_repo,
        source_ref=source_ref,
        issue=issue,
        issue_path=issue_path,
        context_path=context_path,
        mode=mode,
    )
    return RemediationWorkspace(
        repo_dir=repo_dir,
        branch_name=branch_name,
        baseline_auto_merge_policy=baseline_auto_merge_policy,
        feedback_retry_pr=feedback_retry_pr,
        stale_auto_merge_label=stale_auto_merge_label,
        stale_auto_merge_label_skip_reason=stale_auto_merge_label_skip_reason,
        stale_auto_merge_label_removed=stale_auto_merge_label_removed,
        prompt=prompt,
    )


def publish_review_only_comment(
    token: str,
    source_repo: str,
    issue_number: int,
    workspace: RemediationWorkspace,
    final_message: str,
    *,
    source_ref: str,
) -> int:
    review_message = format_codex_message(final_message, workspace.repo_dir, source_repo, source_ref)
    body = truncate_markdown(review_message or "Codex completed review_only mode without a final message.")
    if workspace.stale_auto_merge_label_removed:
        body += (
            f"\n\nRemoved stale `{workspace.stale_auto_merge_label}` from the existing fix PR before posting this review."
        )
    elif workspace.feedback_retry_pr and workspace.stale_auto_merge_label_skip_reason:
        body += f"\n\n{stale_auto_merge_label_skip_message(workspace.stale_auto_merge_label_skip_reason)}."
    post_issue_comment(token, source_repo, issue_number, body)
    return 0


def publish_remediation(
    token: str,
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    issue_number: int,
    workspace: RemediationWorkspace,
    final_message: str,
    *,
    task: str,
    auto_merge: bool,
    commit_prefix: str = "codex",
    remediation_note: str = "",
) -> int:
    status = git_status(workspace.repo_dir)
    paths = changed_paths(status)
    if not paths:
        review_message = format_codex_message(final_message, workspace.repo_dir, source_repo, source_ref)
        body = truncate_markdown(review_message or "Codex found no safe code changes to make.")
        if workspace.stale_auto_merge_label_removed:
            body += (
                f"\n\nRemoved stale `{workspace.stale_auto_merge_label}` from the existing fix PR because this "
                "retry did not produce a verified replacement commit."
            )
        elif workspace.feedback_retry_pr and workspace.stale_auto_merge_label_skip_reason:
            body += f"\n\n{stale_auto_merge_label_skip_message(workspace.stale_auto_merge_label_skip_reason)}."
        post_issue_comment(token, source_repo, issue_number, body)
        return 0

    denied = blocked_paths(paths, task=task)
    if denied:
        denied_list = "\n".join(f"- `{path}`" for path in denied)
        review_message = format_codex_message(final_message, workspace.repo_dir, source_repo, source_ref)
        body = "\n".join(
            [
                "## Codex Audit",
                "",
                "Codex produced edits, but the bridge refused to push them because they touched blocked paths.",
                "",
                "Blocked paths:",
                denied_list,
                "",
                "Codex result:",
                "",
                truncate_markdown(review_message, 7000),
            ]
        )
        if workspace.stale_auto_merge_label_removed:
            body += (
                f"\n\nRemoved stale `{workspace.stale_auto_merge_label}` from the existing fix PR because this "
                "retry produced blocked edits."
            )
        elif workspace.feedback_retry_pr and workspace.stale_auto_merge_label_skip_reason:
            body += f"\n\n{stale_auto_merge_label_skip_message(workspace.stale_auto_merge_label_skip_reason)}."
        post_issue_comment(token, source_repo, issue_number, body)
        return 1

    run_checked(["git", "add", "-A"], cwd=workspace.repo_dir)
    diff_stats = git_diff_stats(workspace.repo_dir, cached=True)
    run_checked(
        ["git", "commit", "-m", f"{commit_prefix}: {task.replace('_', ' ')} for issue #{issue_number}"],
        cwd=workspace.repo_dir,
    )
    git_with_token(workspace.repo_dir, token, ["push", "origin", f"HEAD:refs/heads/{workspace.branch_name}"])
    pr_message = format_codex_message(final_message, workspace.repo_dir, source_repo, workspace.branch_name)
    if workspace.feedback_retry_pr:
        pr = workspace.feedback_retry_pr
    else:
        pr = create_pull_request(
            token,
            source_repo,
            issue,
            workspace.branch_name,
            source_ref,
            pr_message,
            paths,
            task=task,
            policy=workspace.baseline_auto_merge_policy,
        )
    pr_url = pr.get("html_url", "")
    guard_risk = classify_guarded_auto_merge_risk(
        paths,
        task=task,
        policy=workspace.baseline_auto_merge_policy,
        diff_stats=diff_stats,
    )
    stale_auto_merge_label_removed = workspace.stale_auto_merge_label_removed
    stale_auto_merge_label_error = ""
    if (
        workspace.feedback_retry_pr
        and workspace.stale_auto_merge_label
        and not stale_auto_merge_label_removed
        and (not auto_merge or not guard_risk["label_allowed"])
    ):
        try:
            stale_auto_merge_label_removed = remove_issue_label_if_present(
                token,
                source_repo,
                int(pr["number"]),
                workspace.stale_auto_merge_label,
            )
        except (BridgeError, GitHubRequestError) as exc:
            stale_auto_merge_label_error = str(exc)
    body_lines = [
        "## Codex Audit",
        "",
    ]
    if remediation_note:
        body_lines.extend([remediation_note, ""])
    body_lines.append(
        truncate_markdown(
            strip_audit_heading(pr_message) or "Codex completed and produced a fix branch.",
            9000,
        )
    )
    body_lines.extend(
        [
            "",
            f"{'Updated existing fix PR' if workspace.feedback_retry_pr else 'Created fix PR'}: {pr_url}",
        ]
    )
    if not guard_risk["label_allowed"]:
        body_lines.extend(
            [
                "",
                "This PR is high-risk for unattended merge and requires human review.",
                "",
                format_guarded_risk_details(guard_risk),
            ]
        )
        if stale_auto_merge_label_removed:
            body_lines.extend(
                [
                    "",
                    f"Removed stale `{workspace.stale_auto_merge_label}` from the PR before requesting review.",
                ]
            )
        if stale_auto_merge_label_error:
            body_lines.extend(
                [
                    "",
                    f"Could not remove stale `{workspace.stale_auto_merge_label}` from the PR: "
                    f"`{stale_auto_merge_label_error}`",
                ]
            )
        if workspace.feedback_retry_pr and workspace.stale_auto_merge_label_skip_reason:
            body_lines.extend(["", stale_auto_merge_label_skip_message(workspace.stale_auto_merge_label_skip_reason)])
        try:
            review = request_human_review(
                token,
                source_repo,
                int(pr["number"]),
                guard_risk,
            )
            body_lines.append("")
            body_lines.append(f"Added `{review['label']}` to the PR so operators can review it before merge.")
        except (BridgeError, GitHubRequestError) as exc:
            body_lines.append("")
            review_label = str(guard_risk.get("human_review_label") or HUMAN_REVIEW_LABEL)
            body_lines.append(f"Could not add `{review_label}` to the PR: `{exc}`")
        if auto_merge:
            body_lines.append("")
            body_lines.append(
                "Auto-merge was requested, but the bridge refused to add the guarded auto-merge label "
                "because this change set is high-risk."
            )
    elif auto_merge:
        try:
            guard = request_guarded_auto_merge(
                token,
                source_repo,
                int(pr["number"]),
                paths,
                task=task,
                policy=workspace.baseline_auto_merge_policy,
                diff_stats=diff_stats,
            )
            body_lines.append("")
            body_lines.append(
                f"Auto-merge was requested; added `{guard['label']}` for the source repository merge guard "
                f"after Bridge classified the change set as `{guard['risk_level']}` risk. "
                "CI and the source guard own the final merge decision."
            )
        except BridgeError as exc:
            body_lines.append("")
            body_lines.append(f"Auto-merge was requested but the guarded label could not be added: `{exc}`")
    elif stale_auto_merge_label_removed:
        body_lines.append("")
        body_lines.append(
            f"Auto-merge was not requested for this run; removed stale `{workspace.stale_auto_merge_label}` "
            "from the PR."
        )
    elif stale_auto_merge_label_error:
        body_lines.append("")
        body_lines.append(
            f"Auto-merge was not requested for this run, but the bridge could not remove stale "
            f"`{workspace.stale_auto_merge_label}` from the PR: `{stale_auto_merge_label_error}`"
        )
    elif workspace.feedback_retry_pr and workspace.stale_auto_merge_label_skip_reason:
        body_lines.append("")
        body_lines.append(stale_auto_merge_label_skip_message(workspace.stale_auto_merge_label_skip_reason))
    post_issue_comment(token, source_repo, issue_number, "\n".join(body_lines))
    return 0


def run_api_patch_remediation(
    token: str,
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    issue_number: int,
    *,
    task: str,
    mode: str,
    auto_merge: bool,
    provider_order: list[str],
    workspace: RemediationWorkspace | None = None,
    reason: str = "",
    exit_code: int = 1,
) -> int:
    warnings: list[str] = []

    def attempt(workspace_obj: RemediationWorkspace) -> int:
        for provider_name in provider_order:
            secret_name = "OPENAI_API_KEY" if provider_name == "openai" else "ANTHROPIC_API_KEY"
            if not env_value(secret_name):
                warnings.append(f"{provider_name} patch fallback skipped because `{secret_name}` is not configured.")
                continue
            return_code, log_text, final_message = run_api_patch_provider(
                workspace_obj.repo_dir,
                workspace_obj.prompt,
                task=task,
                mode=mode,
                provider=provider_name,
            )
            if return_code != 0:
                warnings.append(f"{provider_name} patch fallback failed: `{log_text}`")
                continue
            note = f"{reason} Applied via API fallback provider `{provider_name}`.".strip()
            return publish_remediation(
                token,
                source_repo,
                source_ref,
                issue,
                issue_number,
                workspace_obj,
                final_message,
                task=task,
                auto_merge=auto_merge,
                commit_prefix="api-fallback",
                remediation_note=note,
            )
        return -1

    if workspace is not None:
        result = attempt(workspace)
        if result >= 0:
            return result
    else:
        with tempfile.TemporaryDirectory(prefix="codex-audit-bridge-api-") as tmp:
            try:
                workspace_obj = prepare_remediation_workspace(
                    token,
                    source_repo,
                    source_ref,
                    issue,
                    comments,
                    issue_number,
                    task,
                    mode,
                    Path(tmp),
                )
            except BridgeError as exc:
                body = str(exc)
                if body.startswith("## Codex Audit"):
                    post_issue_comment(token, source_repo, issue_number, body)
                else:
                    post_issue_comment(
                        token,
                        source_repo,
                        issue_number,
                        "\n".join(
                            [
                                "## Codex Audit",
                                "",
                                reason or "API patch remediation failed before workspace preparation.",
                                "",
                                str(exc),
                            ]
                        ),
                    )
                return exit_code
            result = attempt(workspace_obj)
            if result >= 0:
                return result

    if not env_value("OPENAI_API_KEY") and not env_value("ANTHROPIC_API_KEY"):
        post_issue_comment(token, source_repo, issue_number, auto_fallback_missing_api_key_message(reason))
        return exit_code

    reviews, review_warnings = run_configured_api_reviews(source_repo, source_ref, issue, comments)
    warnings.extend(review_warnings)
    if not reviews:
        body = "\n".join(
            [
                "## Codex Audit",
                "",
                reason,
                "",
                "API patch fallback was configured but all patch providers failed.",
                "",
                *[f"- {warning}" for warning in warnings],
                "",
                "No files were pushed. Check the bridge workflow logs for details.",
            ]
        )
        post_issue_comment(token, source_repo, issue_number, body)
        return exit_code

    post_issue_comment(
        token,
        source_repo,
        issue_number,
        format_api_review_comment(
            f"{reason} Patch fallback failed; posting review-only API fallback comments instead.",
            reviews,
            warnings,
        ),
    )
    return 0


def run_auto_provider_fallback(
    *,
    token: str,
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    issue_number: int,
    reason: str,
    mode: str = DEFAULT_MODE,
    task: str = DEFAULT_TASK,
    auto_merge: bool = False,
    exit_code: int = 1,
    workspace: RemediationWorkspace | None = None,
) -> int:
    try:
        validate_api_fallback_source_repo(source_repo)
    except BridgeError as exc:
        body = "\n".join(
            [
                "## Codex Audit",
                "",
                reason,
                "",
                str(exc),
                "",
                "No API fallback review was run. Update `CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES` in the bridge repository if this source repo is approved for API fallback.",
            ]
        )
        post_issue_comment(token, source_repo, issue_number, body)
        return exit_code

    if mode == "review_and_fix" and api_fallback_allow_fix():
        return run_api_patch_remediation(
            token,
            source_repo,
            source_ref,
            issue,
            comments,
            issue_number,
            task=task,
            mode=mode,
            auto_merge=auto_merge,
            provider_order=api_fallback_provider_order(),
            workspace=workspace,
            reason=reason,
            exit_code=exit_code,
        )

    if not env_value("OPENAI_API_KEY") and not env_value("ANTHROPIC_API_KEY"):
        post_issue_comment(token, source_repo, issue_number, auto_fallback_missing_api_key_message(reason))
        return exit_code

    reviews, warnings = run_configured_api_reviews(source_repo, source_ref, issue, comments)
    if not reviews:
        body = "\n".join(
            [
                "## Codex Audit",
                "",
                reason,
                "",
                "API fallback was configured but all API reviewers failed.",
                "",
                *[f"- {warning}" for warning in warnings],
                "",
                "No files were pushed. Check the bridge workflow logs for details.",
            ]
        )
        post_issue_comment(token, source_repo, issue_number, body)
        return exit_code

    post_issue_comment(
        token,
        source_repo,
        issue_number,
        format_api_review_comment(
            f"{reason} Using the configured API fallback reviewers.",
            reviews,
            warnings,
        ),
    )
    return 0


def run_direct_api_provider(
    token: str,
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    issue_number: int,
    *,
    task: str,
    mode: str,
    auto_merge: bool,
    provider: str,
) -> int:
    validate_api_fallback_source_repo(source_repo)
    if mode == "review_only":
        review_message = (
            run_openai_review(source_repo, source_ref, issue, comments)
            if provider == "openai"
            else run_anthropic_review(source_repo, source_ref, issue, comments)
        )
        post_issue_comment(token, source_repo, issue_number, truncate_markdown(review_message))
        return 0
    return run_api_patch_remediation(
        token,
        source_repo,
        source_ref,
        issue,
        comments,
        issue_number,
        task=task,
        mode=mode,
        auto_merge=auto_merge,
        provider_order=[provider],
        reason=f"{provider} provider was requested directly.",
        exit_code=1,
    )


def git_status(repo_dir: Path) -> str:
    return run_checked(["git", "status", "--porcelain=v1"], cwd=repo_dir)


def changed_paths(status: str) -> list[str]:
    paths: list[str] = []
    for line in status.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def git_diff_stats(repo_dir: Path, *, cached: bool = False) -> dict[str, int]:
    args = ["git", "diff", "--numstat"]
    status_args = ["git", "diff", "--name-status"]
    if cached:
        args.append("--cached")
        status_args.append("--cached")
    output = run_checked(args, cwd=repo_dir)
    status_output = run_checked(status_args, cwd=repo_dir)
    additions = 0
    deletions = 0
    binary_files = 0
    deleted_files = 0
    renamed_files = 0
    copied_files = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            binary_files += 1
            continue
        raw_additions, raw_deletions = parts[0], parts[1]
        if raw_additions == "-" or raw_deletions == "-":
            binary_files += 1
            continue
        try:
            additions += int(raw_additions)
            deletions += int(raw_deletions)
        except ValueError:
            binary_files += 1
    for line in status_output.splitlines():
        if not line.strip():
            continue
        status = line.split("\t", 1)[0].strip().upper()
        if status == "D":
            deleted_files += 1
        elif status.startswith("R"):
            renamed_files += 1
        elif status.startswith("C"):
            copied_files += 1
    return {
        "additions": additions,
        "deletions": deletions,
        "binary_files": binary_files,
        "deleted_files": deleted_files,
        "renamed_files": renamed_files,
        "copied_files": copied_files,
    }


def normalize_changed_path(path: str) -> str:
    normalized = path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def latest_feedback_pr_number(comments: list[dict[str, Any]]) -> int | None:
    marker_re = re.compile(r"^<!--\s*codex-pr-feedback:(?:ci|review):(\d+)\s*-->")
    for comment in reversed(comments):
        body = str(comment.get("body") or "").lstrip() if isinstance(comment, dict) else ""
        match = marker_re.match(body)
        if match:
            return int(match.group(1))
    return None


def _valid_retry_branch_name(branch: str) -> bool:
    return (
        bool(branch)
        and not branch.startswith("-")
        and ".." not in branch
        and not any(ch in branch for ch in " ~^:?*[")
    )


def resolve_feedback_retry_pr(
    token: str,
    source_repo: str,
    issue_number: int,
    source_ref: str,
    comments: list[dict[str, Any]],
    *,
    task: str = DEFAULT_TASK,
) -> dict[str, Any] | None:
    if task != "monthly_snapshot_audit":
        return None
    pr_number = latest_feedback_pr_number(comments)
    if pr_number is None:
        return None
    pr = github_request(token, "GET", f"/repos/{source_repo}/pulls/{pr_number}")
    if not isinstance(pr, dict) or pr.get("state") != "open":
        return None
    head = pr.get("head")
    base = pr.get("base")
    head_repo = head.get("repo") if isinstance(head, dict) else {}
    head_repo_full_name = str(head_repo.get("full_name") or "") if isinstance(head_repo, dict) else ""
    head_ref = str(head.get("ref") or "") if isinstance(head, dict) else ""
    base_ref = str(base.get("ref") or "") if isinstance(base, dict) else ""
    expected_prefix = f"codex/monthly-review-issue-{issue_number}-"
    expected_marker = f"<!-- codex-monthly-remediation:issue-{issue_number} -->"
    body = str(pr.get("body") or "")
    if (
        head_repo_full_name != source_repo
        or base_ref != source_ref
        or not head_ref.startswith(expected_prefix)
        or not _valid_retry_branch_name(head_ref)
        or expected_marker not in body
    ):
        return None
    return {
        "number": int(pr.get("number") or pr_number),
        "html_url": str(pr.get("html_url") or ""),
        "head_ref": head_ref,
        "base_ref": base_ref,
    }


def checkout_feedback_retry_branch(repo_dir: Path, branch_name: str) -> None:
    run_checked(
        ["git", "fetch", "origin", f"refs/heads/{branch_name}:refs/remotes/origin/{branch_name}"],
        cwd=repo_dir,
    )
    run_checked(["git", "checkout", "-B", branch_name, f"refs/remotes/origin/{branch_name}"], cwd=repo_dir)


def load_guarded_auto_merge_policy(policy_path: Path) -> dict[str, Any]:
    if not policy_path.exists():
        return DEFAULT_GUARDED_AUTO_MERGE_POLICY
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fail_closed_guarded_auto_merge_policy("invalid auto-merge policy requires human review")
    if not isinstance(payload, dict):
        return fail_closed_guarded_auto_merge_policy("invalid auto-merge policy requires human review")
    schema_error = guarded_auto_merge_policy_schema_error(payload)
    if schema_error:
        return fail_closed_guarded_auto_merge_policy(schema_error)
    return payload


def guarded_policy_section(policy: dict[str, Any], name: str) -> dict[str, Any]:
    risk_policy = policy.get("risk_policy")
    if not isinstance(risk_policy, dict):
        risk_policy = DEFAULT_GUARDED_AUTO_MERGE_POLICY["risk_policy"]
    section = risk_policy.get(name)
    fallback = DEFAULT_GUARDED_AUTO_MERGE_POLICY["risk_policy"][name]
    return section if isinstance(section, dict) else fallback


def guarded_blocked_patterns(policy: dict[str, Any]) -> tuple[list[re.Pattern[str]], list[str]]:
    errors = [str(item) for item in policy.get("policy_errors", []) if str(item).strip()]
    raw_patterns = policy.get("blocked_path_patterns")
    if raw_patterns is None:
        raw_patterns = DEFAULT_GUARDED_AUTO_MERGE_POLICY["blocked_path_patterns"]
    elif not isinstance(raw_patterns, list):
        errors.append("invalid blocked_path_patterns list requires human review")
        return [re.compile(r".*")], errors
    patterns: list[re.Pattern[str]] = []
    for raw_pattern in raw_patterns:
        if not isinstance(raw_pattern, str):
            errors.append("invalid blocked_path_patterns list requires human review")
            return [re.compile(r".*")], errors
        pattern = str(raw_pattern)
        if not pattern.strip():
            continue
        try:
            patterns.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            errors.append("invalid blocked_path_patterns regex requires human review")
            return [re.compile(r".*")], errors
    return patterns, errors


def guarded_string_list(value: Any, field_name: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"invalid {field_name} list requires human review")
        return []
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            errors.append(f"invalid {field_name} list requires human review")
            return []
        if item.strip():
            items.append(item)
    return items


def guarded_policy_string(policy: dict[str, Any], field_name: str, fallback: str, errors: list[str]) -> str:
    value = policy.get(field_name, fallback)
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        errors.append(f"invalid {field_name} string requires human review")
        return fallback
    return value.strip()


def guarded_policy_positive_int(policy: dict[str, Any], field_name: str, fallback: int, errors: list[str]) -> int:
    value = policy.get(field_name, fallback)
    if type(value) is not int or value < 1:
        errors.append(f"invalid {field_name} integer requires human review")
        return fallback
    return value


def guarded_optional_non_negative_int(value: Any, field_name: str, errors: list[str]) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        errors.append(f"invalid {field_name} count requires human review")
        return None
    return value


def classify_guarded_auto_merge_risk(
    paths: list[str],
    *,
    task: str = DEFAULT_TASK,
    policy: dict[str, Any] | None = None,
    diff_stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    policy = policy or DEFAULT_GUARDED_AUTO_MERGE_POLICY
    low_policy = guarded_policy_section(policy, "low")
    medium_policy = guarded_policy_section(policy, "medium")
    high_policy = guarded_policy_section(policy, "high")
    blocked_patterns, policy_errors = guarded_blocked_patterns(policy)
    auto_merge_label = guarded_policy_string(
        policy,
        "auto_merge_label",
        GUARDED_AUTO_MERGE_LABEL,
        policy_errors,
    )
    human_review_label = guarded_policy_string(
        policy,
        "human_review_label",
        HUMAN_REVIEW_LABEL,
        policy_errors,
    )
    monthly_marker_prefix = guarded_policy_string(
        policy,
        "monthly_marker_prefix",
        DEFAULT_GUARDED_AUTO_MERGE_POLICY["monthly_marker_prefix"],
        policy_errors,
    )
    max_changed_files = guarded_policy_positive_int(
        policy,
        "max_changed_files",
        DEFAULT_GUARDED_AUTO_MERGE_MAX_CHANGED_FILES,
        policy_errors,
    )
    max_changed_lines = guarded_policy_positive_int(
        policy,
        "max_changed_lines",
        DEFAULT_GUARDED_AUTO_MERGE_MAX_CHANGED_LINES,
        policy_errors,
    )
    low_exact = set(guarded_string_list(low_policy.get("exact"), "risk_policy.low.exact", policy_errors))
    low_prefixes = tuple(guarded_string_list(low_policy.get("prefixes"), "risk_policy.low.prefixes", policy_errors))
    medium_exact = set(guarded_string_list(medium_policy.get("exact"), "risk_policy.medium.exact", policy_errors))
    high_risk: list[str] = []
    medium_risk: list[str] = []
    low_risk_count = 0
    normalized_paths = [normalize_changed_path(path) for path in paths if normalize_changed_path(path)]
    if len(normalized_paths) > max_changed_files:
        policy_errors.append(
            f"changed file count exceeds auto-merge limit requires human review: {len(normalized_paths)} > {max_changed_files}"
        )
    additions: int | None = None
    deletions: int | None = None
    changed_lines: int | None = None
    binary_files = 0
    deleted_files = 0
    renamed_files = 0
    copied_files = 0
    if diff_stats is not None:
        additions = guarded_optional_non_negative_int(diff_stats.get("additions"), "additions", policy_errors)
        deletions = guarded_optional_non_negative_int(diff_stats.get("deletions"), "deletions", policy_errors)
        binary_files = guarded_optional_non_negative_int(diff_stats.get("binary_files", 0), "binary files", policy_errors) or 0
        deleted_files = guarded_optional_non_negative_int(diff_stats.get("deleted_files", 0), "deleted files", policy_errors) or 0
        renamed_files = guarded_optional_non_negative_int(diff_stats.get("renamed_files", 0), "renamed files", policy_errors) or 0
        copied_files = guarded_optional_non_negative_int(diff_stats.get("copied_files", 0), "copied files", policy_errors) or 0
        if additions is not None and deletions is not None:
            changed_lines = additions + deletions
            if changed_lines > max_changed_lines:
                policy_errors.append(
                    f"changed line count exceeds auto-merge limit requires human review: {changed_lines} > {max_changed_lines}"
                )
        if binary_files:
            policy_errors.append("binary file changes require human review")
        if deleted_files:
            policy_errors.append("file deletions require human review")
        if renamed_files:
            policy_errors.append("file renames require human review")
        if copied_files:
            policy_errors.append("file copies require human review")
    for raw_path in paths:
        normalized = normalize_changed_path(raw_path)
        if not normalized:
            continue
        if policy_errors:
            high_risk.append(normalized)
            continue
        if any(pattern.search(normalized) for pattern in blocked_patterns):
            high_risk.append(normalized)
            continue
        if normalized in low_exact or any(normalized.startswith(prefix) for prefix in low_prefixes):
            low_risk_count += 1
            continue
        if task == "monthly_snapshot_audit" and normalized in medium_exact:
            medium_risk.append(normalized)
            continue
        high_risk.append(normalized)

    if high_risk:
        risk_level = "high"
        risk_reasons = policy_errors or [
            str(high_policy.get("reason") or DEFAULT_GUARDED_AUTO_MERGE_POLICY["risk_policy"]["high"]["reason"])
        ]
    elif medium_risk:
        risk_level = "medium"
        risk_reasons = [
            str(medium_policy.get("reason") or DEFAULT_GUARDED_AUTO_MERGE_POLICY["risk_policy"]["medium"]["reason"])
        ]
    else:
        risk_level = "low"
        risk_reasons = [
            str(low_policy.get("reason") or DEFAULT_GUARDED_AUTO_MERGE_POLICY["risk_policy"]["low"]["reason"])
        ]

    return {
        "label_allowed": not high_risk,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "policy_errors": policy_errors,
        "auto_merge_label": auto_merge_label,
        "human_review_label": human_review_label,
        "monthly_marker_prefix": monthly_marker_prefix,
        "high_risk_files": high_risk,
        "medium_risk_files": medium_risk,
        "low_risk_file_count": low_risk_count,
        "additions": additions,
        "deletions": deletions,
        "changed_lines": changed_lines,
        "binary_files": binary_files,
        "deleted_files": deleted_files,
        "renamed_files": renamed_files,
        "copied_files": copied_files,
    }


def long_horizon_signal_data_path_allowed(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in LONG_HORIZON_SIGNAL_OUTPUT_PREFIXES)


def blocked_paths(paths: list[str], *, task: str = DEFAULT_TASK) -> list[str]:
    allow_data = parse_bool(env_value("CODEX_AUDIT_ALLOW_DATA_CHANGES"))
    blocked: list[str] = []
    for path in paths:
        normalized = path.strip()
        if not normalized:
            continue
        task_allows_data = task == "long_horizon_signal_shadow" and long_horizon_signal_data_path_allowed(normalized)
        if normalized.startswith("data/") and not allow_data and not task_allows_data:
            blocked.append(normalized)
            continue
        if BLOCKED_PATH_RE.search(normalized):
            blocked.append(normalized)
    return blocked


def truncate_markdown(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n...[truncated by CodexAuditBridge]"


def strip_audit_heading(text: str) -> str:
    return re.sub(r"^## (?:Crypto )?Codex Audit\s*\n+", "", text.strip(), count=1)


def github_file_url(source_repo: str, ref: str, rel_path: Path, line: int | None = None) -> str:
    encoded_ref = urllib.parse.quote(ref, safe="/")
    encoded_path = "/".join(urllib.parse.quote(part) for part in rel_path.parts)
    url = f"https://github.com/{source_repo}/blob/{encoded_ref}/{encoded_path}"
    if line is not None:
        url += f"#L{line}"
    return url


def convert_local_markdown_links(text: str, repo_dir: Path, source_repo: str, ref: str) -> str:
    repo_root = repo_dir.resolve()

    def replace(match: re.Match[str]) -> str:
        label = match.group(1)
        target = match.group(2)
        path_text = target
        line: int | None = None
        line_match = re.fullmatch(r"(.+):(\d+)", target)
        if line_match:
            path_text = line_match.group(1)
            line = int(line_match.group(2))
        path = Path(path_text)
        if not path.is_absolute():
            return match.group(0)
        try:
            rel_path = path.resolve().relative_to(repo_root)
        except ValueError:
            return match.group(0)
        if not rel_path.parts or rel_path.parts[0] == ".git":
            return match.group(0)
        return f"[{label}]({github_file_url(source_repo, ref, rel_path, line)})"

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", replace, text)


def format_codex_message(final_message: str, repo_dir: Path, source_repo: str, ref: str) -> str:
    return convert_local_markdown_links(final_message, repo_dir, source_repo, ref).strip()


def post_issue_comment(token: str, source_repo: str, issue_number: int, body: str) -> None:
    if parse_bool(env_value("CODEX_AUDIT_SKIP_COMMENTS")):
        print("Skipping issue comment because CODEX_AUDIT_SKIP_COMMENTS is set.")
        return
    github_request(
        token,
        "POST",
        f"/repos/{source_repo}/issues/{issue_number}/comments",
        {"body": truncate_markdown(body)},
    )


def pr_closing_line(task: str, issue_number: int) -> str:
    if task == "long_horizon_signal_shadow":
        return f"Closes #{issue_number}"
    return ""


def create_pull_request(
    token: str,
    source_repo: str,
    issue: dict[str, Any],
    branch_name: str,
    base_ref: str,
    final_message: str,
    paths: list[str],
    *,
    task: str = DEFAULT_TASK,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue_number = issue["number"]
    if task == "long_horizon_signal_shadow":
        title = f"codex: long-horizon shadow signal for issue #{issue_number}"
        marker_line = f"<!-- codex-long-horizon-signal:issue-{issue_number} -->"
    else:
        title = f"codex: monthly audit fixes for issue #{issue_number}"
        marker_errors: list[str] = []
        marker_prefix = guarded_policy_string(
            policy or DEFAULT_GUARDED_AUTO_MERGE_POLICY,
            "monthly_marker_prefix",
            DEFAULT_GUARDED_AUTO_MERGE_POLICY["monthly_marker_prefix"],
            marker_errors,
        )
        marker_line = f"{marker_prefix}{issue_number} -->"
    changed_list = "\n".join(f"- `{path}`" for path in paths) or "- None"
    trigger_label = "long-horizon signal issue" if task == "long_horizon_signal_shadow" else "monthly review issue"
    body_lines = [
        marker_line,
        "",
        f"Triggered by {trigger_label} #{issue_number}: {issue.get('html_url', '')}",
    ]
    closing_line = pr_closing_line(task, int(issue_number))
    if closing_line:
        body_lines.extend(["", closing_line])
    body_lines.extend(
        [
            "",
            "## Changed Files",
            "",
            changed_list,
            "",
            "## Codex Result",
            "",
            truncate_markdown(strip_audit_heading(final_message), 6000)
            or "Codex edited files but did not return a final message.",
        ]
    )
    body = "\n".join(body_lines)
    return github_request(
        token,
        "POST",
        f"/repos/{source_repo}/pulls",
        {
            "title": title,
            "head": branch_name,
            "base": base_ref,
            "body": body,
            "maintainer_can_modify": True,
        },
    )


def ensure_repo_label(
    token: str,
    source_repo: str,
    label: str,
    *,
    color: str = "0E8A16",
    description: str = "Guarded Codex remediation PR may be auto-merged after source CI and merge guard pass",
) -> None:
    encoded_label = urllib.parse.quote(label, safe="")
    try:
        github_request(token, "GET", f"/repos/{source_repo}/labels/{encoded_label}")
    except GitHubRequestError as exc:
        if exc.status_code != 404:
            raise
        github_request(
            token,
            "POST",
            f"/repos/{source_repo}/labels",
            {"name": label, "color": color, "description": description},
        )


def request_guarded_auto_merge(
    token: str,
    source_repo: str,
    pr_number: int,
    paths: list[str],
    *,
    task: str = DEFAULT_TASK,
    policy: dict[str, Any] | None = None,
    diff_stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    risk = classify_guarded_auto_merge_risk(paths, task=task, policy=policy, diff_stats=diff_stats)
    if diff_stats is None and risk["label_allowed"]:
        raise BridgeError("Guarded auto-merge label refused because diff stats are unavailable")
    if not risk["label_allowed"]:
        high_risk_files = ", ".join(risk["high_risk_files"])
        raise BridgeError(f"Guarded auto-merge label refused for {risk['risk_level']} risk files: {high_risk_files}")
    label = str(risk["auto_merge_label"])
    human_review_label = str(risk.get("human_review_label") or HUMAN_REVIEW_LABEL)
    if issue_has_label(token, source_repo, pr_number, human_review_label):
        raise BridgeError(
            f"Guarded auto-merge label refused because `{human_review_label}` is present on the PR"
        )
    ensure_repo_label(token, source_repo, label)
    github_request(
        token,
        "POST",
        f"/repos/{source_repo}/issues/{pr_number}/labels",
        {"labels": [label]},
    )
    return {"label": label, **risk}


def guarded_auto_merge_label(policy: dict[str, Any] | None = None) -> str:
    errors: list[str] = []
    return guarded_policy_string(
        policy or DEFAULT_GUARDED_AUTO_MERGE_POLICY,
        "auto_merge_label",
        GUARDED_AUTO_MERGE_LABEL,
        errors,
    )


def guarded_auto_merge_label_for_mutation(policy: dict[str, Any] | None = None) -> tuple[str, str]:
    policy = policy or DEFAULT_GUARDED_AUTO_MERGE_POLICY
    policy_errors = [str(item).strip() for item in policy.get("policy_errors", []) if str(item).strip()]
    if policy_errors:
        return "", "; ".join(policy_errors)
    errors: list[str] = []
    auto_merge_label = guarded_policy_string(policy, "auto_merge_label", GUARDED_AUTO_MERGE_LABEL, errors)
    human_review_label = guarded_policy_string(policy, "human_review_label", HUMAN_REVIEW_LABEL, errors)
    if auto_merge_label == human_review_label:
        errors.append("auto-merge and human-review labels must be distinct requires human review")
    if errors:
        return "", "; ".join(errors)
    return auto_merge_label, ""


def stale_auto_merge_label_skip_message(reason: str) -> str:
    return (
        "Skipped stale guarded auto-merge label cleanup because the baseline auto-merge policy labels "
        f"are not safe for mutation: `{reason}`"
    )


def issue_has_label(token: str, source_repo: str, issue_number: int, label: str) -> bool:
    label = str(label or "").strip()
    if not label or "\n" in label or "\r" in label:
        raise BridgeError("Invalid issue label name")
    issue = github_request(token, "GET", f"/repos/{source_repo}/issues/{issue_number}")
    labels = issue.get("labels") if isinstance(issue, dict) else None
    if not isinstance(labels, list):
        raise BridgeError("GitHub issue labels response is malformed")
    for item in labels:
        name = item.get("name") if isinstance(item, dict) else item
        if str(name or "").strip() == label:
            return True
    return False


def remove_issue_label_if_present(token: str, source_repo: str, issue_number: int, label: str) -> bool:
    label = str(label or "").strip()
    if not label or "\n" in label or "\r" in label:
        raise BridgeError("Invalid issue label name")
    encoded_label = urllib.parse.quote(label, safe="")
    try:
        github_request(
            token,
            "DELETE",
            f"/repos/{source_repo}/issues/{issue_number}/labels/{encoded_label}",
        )
    except GitHubRequestError as exc:
        if exc.status_code == 404:
            return False
        raise
    return True


def format_guarded_risk_details(risk: dict[str, Any]) -> str:
    reasons = [str(item).strip() for item in risk.get("risk_reasons", []) if str(item).strip()]
    high_risk_files = [str(item).strip() for item in risk.get("high_risk_files", []) if str(item).strip()]
    lines = [
        f"- Risk level: `{risk.get('risk_level', 'unknown')}`",
    ]
    if risk.get("changed_lines") is not None:
        lines.append(f"- Changed lines: `{risk['changed_lines']}`")
    if reasons:
        lines.append("- Reasons:")
        lines.extend(f"  - {reason}" for reason in reasons)
    if high_risk_files:
        lines.append("- High-risk files:")
        lines.extend(f"  - `{path}`" for path in high_risk_files)
    return "\n".join(lines)


def request_human_review(
    token: str,
    source_repo: str,
    pr_number: int,
    risk: dict[str, Any],
    *,
    label: str | None = None,
) -> dict[str, Any]:
    label = str(label or risk.get("human_review_label") or HUMAN_REVIEW_LABEL)
    ensure_repo_label(
        token,
        source_repo,
        label,
        color="B60205",
        description="Codex remediation PR requires human review before merge",
    )
    github_request(
        token,
        "POST",
        f"/repos/{source_repo}/issues/{pr_number}/labels",
        {"labels": [label]},
    )
    return {"label": label, **risk}


def main() -> int:
    source_repo = validate_repo(env_value("SOURCE_REPO", DEFAULT_SOURCE_REPO))
    task = validate_task(env_value("CODEX_AUDIT_TASK", DEFAULT_TASK), source_repo)
    source_ref = env_value("SOURCE_REF", "main")
    mode = env_value("CODEX_AUDIT_MODE", DEFAULT_MODE)
    if mode not in {"review_only", "review_and_fix"}:
        raise BridgeError(f"Unsupported CODEX_AUDIT_MODE: {mode}")
    provider = validate_provider(env_value("CODEX_AUDIT_PROVIDER", DEFAULT_PROVIDER))
    codex_backend = validate_codex_backend(env_value("CODEX_AUDIT_CODEX_BACKEND", DEFAULT_CODEX_BACKEND))
    issue_number_raw = env_value("ISSUE_NUMBER")
    if not issue_number_raw.isdigit():
        raise BridgeError("ISSUE_NUMBER must be provided as an integer")
    issue_number = int(issue_number_raw)
    token = resolve_source_repo_token(source_repo)
    timeout_minutes = int(env_value("CODEX_AUDIT_TIMEOUT_MINUTES", "45"))
    auto_merge = parse_bool(env_value("CODEX_AUDIT_AUTO_MERGE"))

    print(
        f"Running {task} for {source_repo} issue #{issue_number} on {source_ref} in {mode} mode "
        f"with provider {provider} and Codex backend {codex_backend}."
    )
    issue = github_request(token, "GET", f"/repos/{source_repo}/issues/{issue_number}")
    comments = fetch_issue_comments(token, source_repo, issue_number)

    if provider == "openai":
        return run_direct_api_provider(
            token,
            source_repo,
            source_ref,
            issue,
            comments,
            issue_number,
            task=task,
            mode=mode,
            auto_merge=auto_merge,
            provider="openai",
        )
    if provider == "anthropic":
        return run_direct_api_provider(
            token,
            source_repo,
            source_ref,
            issue,
            comments,
            issue_number,
            task=task,
            mode=mode,
            auto_merge=auto_merge,
            provider="anthropic",
        )
    if provider == "api":
        return run_auto_provider_fallback(
            token=token,
            source_repo=source_repo,
            source_ref=source_ref,
            issue=issue,
            comments=comments,
            issue_number=issue_number,
            reason="API provider was requested directly.",
            mode=mode,
            task=task,
            auto_merge=auto_merge,
        )

    try:
        with tempfile.TemporaryDirectory(prefix="codex-audit-bridge-") as tmp:
            workspace = prepare_remediation_workspace(
                token,
                source_repo,
                source_ref,
                issue,
                comments,
                issue_number,
                task,
                mode,
                Path(tmp),
            )
            return_code, _codex_log, final_message = run_codex_backend(
                workspace.repo_dir,
                workspace.prompt,
                timeout_minutes,
                backend=codex_backend,
                source_repo=source_repo,
                source_ref=source_ref,
                task=task,
                mode=mode,
                issue_number=issue_number,
            )
            if return_code != 0:
                if return_code == SERVICE_INFRA_FAILURE_EXIT_CODE or is_service_infrastructure_failure(_codex_log):
                    post_issue_comment(token, source_repo, issue_number, service_infrastructure_failure_comment(_codex_log))
                    return 0
                if provider == "auto":
                    return run_auto_provider_fallback(
                        token=token,
                        source_repo=source_repo,
                        source_ref=source_ref,
                        issue=issue,
                        comments=comments,
                        issue_number=issue_number,
                        reason=f"Codex service failed with exit code `{return_code}`.",
                        mode=mode,
                        task=task,
                        auto_merge=auto_merge,
                        exit_code=return_code,
                        workspace=workspace,
                    )
                body = "\n".join(
                    [
                        "## Codex Audit",
                        "",
                        f"Codex execution failed with exit code `{return_code}`.",
                        "",
                        "No files were pushed. Check the bridge workflow logs for details.",
                    ]
                )
                post_issue_comment(token, source_repo, issue_number, body)
                return return_code

            if mode == "review_only":
                return publish_review_only_comment(
                    token,
                    source_repo,
                    issue_number,
                    workspace,
                    final_message,
                    source_ref=source_ref,
                )

            return publish_remediation(
                token,
                source_repo,
                source_ref,
                issue,
                issue_number,
                workspace,
                final_message,
                task=task,
                auto_merge=auto_merge,
            )
    except BridgeError as exc:
        if str(exc).startswith("## Codex Audit"):
            post_issue_comment(token, source_repo, issue_number, str(exc))
            return 1
        if provider == "auto":
            return run_auto_provider_fallback(
                token=token,
                source_repo=source_repo,
                source_ref=source_ref,
                issue=issue,
                comments=comments,
                issue_number=issue_number,
                reason=f"Codex service path failed before completion: `{exc}`.",
                mode=mode,
                task=task,
                auto_merge=auto_merge,
            )
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        if provider == "auto":
            return run_auto_provider_fallback(
                token=token,
                source_repo=source_repo,
                source_ref=source_ref,
                issue=issue,
                comments=comments,
                issue_number=issue_number,
                reason=f"Codex service path failed before completion: `{exc}`.",
                mode=mode,
                task=task,
                auto_merge=auto_merge,
            )
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
