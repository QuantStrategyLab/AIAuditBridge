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
DEFAULT_API_FALLBACK_ALLOWED_SOURCE_REPOS = ALLOWED_SOURCE_REPOS
DEFAULT_TASK = "monthly_snapshot_audit"
DEFAULT_MODE = "review_and_fix"
DEFAULT_PROVIDER = "auto"
SUPPORTED_PROVIDERS = frozenset({"api", "anthropic", "codex", "openai", "auto"})
DEFAULT_CODEX_BACKEND = "service"
SUPPORTED_CODEX_BACKENDS = frozenset({"service"})
DEFAULT_SERVICE_AUDIENCE = "quant-codex-audit"
DEFAULT_SERVICE_CONTEXT_MAX_BYTES = 700_000
DEFAULT_SERVICE_CONTEXT_MAX_FILE_BYTES = 80_000
DEFAULT_SERVICE_MAX_CHANGES = 20
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


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def split_csv_values(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\n,]", value) if item.strip()]


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


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
        return DEFAULT_API_FALLBACK_ALLOWED_SOURCE_REPOS
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
        raise BridgeError(f"GitHub API {method} {url} failed: {exc.code} {body[:600]}") from exc
    return json.loads(body) if body else {}


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
    comments_md = "\n\n".join(
        f"### Comment by {comment.get('user', {}).get('login', 'unknown')}\n\n{comment.get('body') or ''}"
        for comment in comments[:20]
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
            raise BridgeError(f"Codex audit service job failed: {error[:600]}")
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
        )
        final_message = output
        if mode == "review_and_fix":
            final_message, changes = parse_service_patch_response(output)
            apply_service_changes(repo_dir, changes, task=task)
        output_path = repo_dir / ".codex-audit" / "codex-final-message.md"
        output_path.write_text(final_message.rstrip() + "\n", encoding="utf-8")
        return 0, output, final_message.strip()
    except (BridgeError, OSError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return 1, str(exc), ""


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
    comments_md = "\n\n".join(
        f"### Comment by {comment.get('user', {}).get('login', 'unknown')}\n\n{comment.get('body') or ''}"
        for comment in comments[:20]
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


def run_openai_review(source_repo: str, source_ref: str, issue: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    api_key = env_value("OPENAI_API_KEY")
    if not api_key:
        raise BridgeError("OPENAI_API_KEY is required for OpenAI API review")
    model = env_value("OPENAI_MODEL", "gpt-5.4-mini")
    base_url = env_value("OPENAI_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a careful repository release reviewer. Return only markdown.",
            },
            {
                "role": "user",
                "content": build_api_review_prompt(
                    source_repo,
                    source_ref,
                    issue,
                    comments,
                    task=env_value("CODEX_AUDIT_TASK", DEFAULT_TASK),
                ),
            },
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


def run_anthropic_review(source_repo: str, source_ref: str, issue: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    api_key = env_value("ANTHROPIC_API_KEY")
    if not api_key:
        raise BridgeError("ANTHROPIC_API_KEY is required for CODEX_AUDIT_PROVIDER=anthropic or API fallback")
    model = env_value("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    base_url = env_value("ANTHROPIC_API_BASE_URL", "https://api.anthropic.com/v1").rstrip("/")
    api_version = env_value("ANTHROPIC_VERSION", "2023-06-01")
    payload = {
        "model": model,
        "max_tokens": int(env_value("ANTHROPIC_MAX_TOKENS", "4000")),
        "system": "You are a careful repository release reviewer. Return only markdown.",
        "messages": [
            {
                "role": "user",
                "content": build_api_review_prompt(
                    source_repo,
                    source_ref,
                    issue,
                    comments,
                    task=env_value("CODEX_AUDIT_TASK", DEFAULT_TASK),
                ),
            }
        ],
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


def run_auto_provider_fallback(
    *,
    token: str,
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    issue_number: int,
    reason: str,
    exit_code: int = 1,
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
) -> dict[str, Any]:
    issue_number = issue["number"]
    if task == "long_horizon_signal_shadow":
        title = f"codex: long-horizon shadow signal for issue #{issue_number}"
        marker = f"codex-long-horizon-signal:issue-{issue_number}"
    else:
        title = f"codex: monthly audit fixes for issue #{issue_number}"
        marker = f"codex-monthly-remediation:issue-{issue_number}"
    changed_list = "\n".join(f"- `{path}`" for path in paths) or "- None"
    trigger_label = "long-horizon signal issue" if task == "long_horizon_signal_shadow" else "monthly review issue"
    body_lines = [
        f"<!-- {marker} -->",
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


def enable_auto_merge(token: str, source_repo: str, pr_number: int) -> str:
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    result = run(
        [
            "gh",
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            source_repo,
            "--squash",
            "--auto",
        ],
        env=env,
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.returncode != 0:
        raise BridgeError("Unable to enable auto-merge for generated PR")
    return result.stdout or ""


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
    comments = github_request(token, "GET", f"/repos/{source_repo}/issues/{issue_number}/comments?per_page=20")
    if not isinstance(comments, list):
        comments = []

    if provider == "openai":
        validate_api_fallback_source_repo(source_repo)
        review_message = run_openai_review(source_repo, source_ref, issue, comments)
        post_issue_comment(token, source_repo, issue_number, truncate_markdown(review_message))
        return 0
    if provider == "anthropic":
        validate_api_fallback_source_repo(source_repo)
        review_message = run_anthropic_review(source_repo, source_ref, issue, comments)
        post_issue_comment(token, source_repo, issue_number, truncate_markdown(review_message))
        return 0
    if provider == "api":
        return run_auto_provider_fallback(
            token=token,
            source_repo=source_repo,
            source_ref=source_ref,
            issue=issue,
            comments=comments,
            issue_number=issue_number,
            reason="API provider was requested directly.",
        )

    try:
        with tempfile.TemporaryDirectory(prefix="codex-audit-bridge-") as tmp:
            work_root = Path(tmp)
            repo_dir = clone_source_repo(token, source_repo, source_ref, work_root)
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
            branch_prefix = "long-horizon-signal" if task == "long_horizon_signal_shadow" else "monthly-review"
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
            return_code, _codex_log, final_message = run_codex_backend(
                repo_dir,
                prompt,
                timeout_minutes,
                backend=codex_backend,
                source_repo=source_repo,
                source_ref=source_ref,
                task=task,
                mode=mode,
            )
            if return_code != 0:
                if provider == "auto":
                    return run_auto_provider_fallback(
                        token=token,
                        source_repo=source_repo,
                        source_ref=source_ref,
                        issue=issue,
                        comments=comments,
                        issue_number=issue_number,
                        reason=f"Codex service failed with exit code `{return_code}`.",
                        exit_code=return_code,
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

            status = git_status(repo_dir)
            paths = changed_paths(status)
            if mode == "review_only":
                review_message = format_codex_message(final_message, repo_dir, source_repo, source_ref)
                post_issue_comment(
                    token,
                    source_repo,
                    issue_number,
                    truncate_markdown(review_message or "Codex completed review_only mode without a final message."),
                )
                return 0

            if not paths:
                review_message = format_codex_message(final_message, repo_dir, source_repo, source_ref)
                post_issue_comment(
                    token,
                    source_repo,
                    issue_number,
                    truncate_markdown(review_message or "Codex found no safe code changes to make."),
                )
                return 0

            denied = blocked_paths(paths, task=task)
            if denied:
                denied_list = "\n".join(f"- `{path}`" for path in denied)
                review_message = format_codex_message(final_message, repo_dir, source_repo, source_ref)
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
                post_issue_comment(token, source_repo, issue_number, body)
                return 1

            run_checked(["git", "add", "-A"], cwd=repo_dir)
            run_checked(
                ["git", "commit", "-m", f"codex: {task.replace('_', ' ')} for issue #{issue_number}"],
                cwd=repo_dir,
            )
            git_with_token(repo_dir, token, ["push", "origin", f"HEAD:refs/heads/{branch_name}"])
            pr_message = format_codex_message(final_message, repo_dir, source_repo, branch_name)
            pr = create_pull_request(token, source_repo, issue, branch_name, source_ref, pr_message, paths, task=task)
            pr_url = pr.get("html_url", "")
            body_lines = [
                "## Codex Audit",
                "",
                truncate_markdown(
                    strip_audit_heading(pr_message)
                    or "Codex completed and produced a fix branch.",
                    9000,
                ),
                "",
                f"Created fix PR: {pr_url}",
            ]
            if auto_merge:
                try:
                    enable_auto_merge(token, source_repo, int(pr["number"]))
                    body_lines.append("")
                    body_lines.append("Auto-merge was requested and has been enabled for the PR.")
                except BridgeError as exc:
                    body_lines.append("")
                    body_lines.append(f"Auto-merge was requested but could not be enabled: `{exc}`")
            post_issue_comment(token, source_repo, issue_number, "\n".join(body_lines))
            return 0
    except (BridgeError, OSError, subprocess.SubprocessError) as exc:
        if provider == "auto":
            return run_auto_provider_fallback(
                token=token,
                source_repo=source_repo,
                source_ref=source_ref,
                issue=issue,
                comments=comments,
                issue_number=issue_number,
                reason=f"Codex service path failed before completion: `{exc}`.",
            )
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
