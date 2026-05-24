#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from string import Template
import tempfile
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


API_BASE = "https://api.github.com"
ROOT = Path(__file__).resolve().parents[1]
PROMPT_TEMPLATE = ROOT / "prompts" / "monthly_crypto_snapshot_audit.md"
DEFAULT_SOURCE_REPO = "QuantStrategyLab/CryptoSnapshotPipelines"
ALLOWED_SOURCE_REPOS = frozenset(
    {
        "QuantStrategyLab/CryptoSnapshotPipelines",
        "QuantStrategyLab/UsEquitySnapshotPipelines",
    }
)
DEFAULT_MODE = "review_and_fix"
DEFAULT_PROVIDER = "auto"
SUPPORTED_PROVIDERS = frozenset({"codex", "openai", "auto"})
BLOCKED_PATH_RE = re.compile(
    r"(^|/)(\.env|.*secret.*|.*credential.*|.*token.*|.*private.*|.*\.pem|.*\.key)$",
    re.IGNORECASE,
)
SECRET_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "CREDENTIAL")


class BridgeError(RuntimeError):
    pass


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


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


def validate_provider(provider: str) -> str:
    normalized = (provider or DEFAULT_PROVIDER).strip().lower()
    if normalized not in SUPPORTED_PROVIDERS:
        raise BridgeError(f"Unsupported CODEX_AUDIT_PROVIDER: {provider!r}")
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
            "User-Agent": "selfhosted-codex-audit-bridge",
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
        raise BridgeError(f"Command failed with exit code {result.returncode}: {command[0]}")
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


def codex_process_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in SECRET_ENV_MARKERS)
    }


def bootstrap_packages() -> list[str]:
    raw = env_value("CODEX_AUDIT_PYTHON_BOOTSTRAP_PACKAGES", "pandas")
    return shlex.split(raw)


def package_import_name(package_spec: str) -> str:
    package_name = re.split(r"[<>=!~\[]", package_spec, maxsplit=1)[0].strip()
    normalized = package_name.replace("-", "_").lower()
    if normalized == "pyyaml":
        return "yaml"
    return normalized


def python_path_for_venv(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def venv_bin_path(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def python_can_import(python: Path, import_name: str) -> bool:
    result = run([str(python), "-c", f"import {import_name}"], timeout=30)
    return result.returncode == 0


def bootstrap_python_environment() -> dict[str, str]:
    if not parse_bool(env_value("CODEX_AUDIT_INSTALL_PYTHON_DEPS", "true")):
        return {}

    packages = bootstrap_packages()
    if not packages:
        return {}

    venv_root = Path(env_value("CODEX_AUDIT_PYTHON_VENV", "~/.cache/crypto-codex-audit/python-venv")).expanduser()
    python = python_path_for_venv(venv_root)
    if not python.exists():
        venv_root.parent.mkdir(parents=True, exist_ok=True)
        run_checked([sys.executable, "-m", "venv", str(venv_root)], timeout=180)

    missing = [package for package in packages if not python_can_import(python, package_import_name(package))]
    if missing:
        install_timeout = int(env_value("CODEX_AUDIT_PIP_INSTALL_TIMEOUT_SECONDS", "600"))
        run_checked(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *missing,
            ],
            timeout=install_timeout,
        )

    path = os.environ.get("PATH", "")
    return {
        "VIRTUAL_ENV": str(venv_root),
        "PATH": f"{venv_bin_path(venv_root)}{os.pathsep}{path}" if path else str(venv_bin_path(venv_root)),
    }


def codex_environment(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = codex_process_env()
    if extra_env:
        env.update(extra_env)
    return env


def git_with_token(repo_dir: Path, token: str, args: list[str]) -> str:
    return run_checked(["git", *args], cwd=repo_dir, env=git_auth_env(token))


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
    source_repo: str,
    source_ref: str,
    issue: dict[str, Any],
    issue_path: Path,
    context_path: Path,
    mode: str,
) -> str:
    template = Template(PROMPT_TEMPLATE.read_text(encoding="utf-8"))
    return template.safe_substitute(
        SOURCE_REPO=source_repo,
        SOURCE_REF=source_ref,
        ISSUE_URL=issue.get("html_url", ""),
        ISSUE_NUMBER=str(issue.get("number", "")),
        MODE=mode,
        ISSUE_MARKDOWN_PATH=str(issue_path),
        CONTEXT_JSON_PATH=str(context_path),
    )


def run_codex(
    repo_dir: Path,
    prompt: str,
    timeout_minutes: int,
    *,
    env_overrides: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    output_path = repo_dir / ".codex-audit" / "codex-final-message.md"
    command = [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(repo_dir),
        "--output-last-message",
        str(output_path),
        "-",
    ]
    try:
        result = run(command, env=codex_environment(env_overrides), input_text=prompt, timeout=timeout_minutes * 60)
    except FileNotFoundError:
        return 127, "codex command was not found", ""
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    final_message = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    return result.returncode, result.stdout or "", final_message.strip()


def build_api_review_prompt(source_repo: str, source_ref: str, issue: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    comments_md = "\n\n".join(
        f"### Comment by {comment.get('user', {}).get('login', 'unknown')}\n\n{comment.get('body') or ''}"
        for comment in comments[:20]
    )
    return "\n".join(
        [
            "You are reviewing a monthly snapshot report issue for a QuantStrategyLab source repository.",
            "Return a concise bilingual markdown review. Do not claim to have changed files.",
            "Focus on release consistency, evidence gaps, downstream impact, and low-risk follow-up actions.",
            "Do not recommend production strategy changes from one monthly snapshot alone.",
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


def run_openai_review(source_repo: str, source_ref: str, issue: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    api_key = env_value("OPENAI_API_KEY")
    if not api_key:
        raise BridgeError("OPENAI_API_KEY is required for CODEX_AUDIT_PROVIDER=openai or auto fallback")
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
                "content": build_api_review_prompt(source_repo, source_ref, issue, comments),
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
            "User-Agent": "crypto-codex-audit-bridge-openai",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BridgeError(f"OpenAI API request failed: {exc.code} {detail[:600]}") from exc
    return extract_openai_text(json.loads(body))


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


def blocked_paths(paths: list[str]) -> list[str]:
    allow_data = parse_bool(env_value("CODEX_AUDIT_ALLOW_DATA_CHANGES"))
    blocked: list[str] = []
    for path in paths:
        normalized = path.strip()
        if not normalized:
            continue
        if normalized.startswith("data/") and not allow_data:
            blocked.append(normalized)
            continue
        if BLOCKED_PATH_RE.search(normalized):
            blocked.append(normalized)
    return blocked


def truncate_markdown(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n...[truncated by CryptoCodexAuditBridge]"


def strip_audit_heading(text: str) -> str:
    return re.sub(r"^## (?:Crypto|Self-hosted) Codex Audit\s*\n+", "", text.strip(), count=1)


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


def create_pull_request(
    token: str,
    source_repo: str,
    issue: dict[str, Any],
    branch_name: str,
    base_ref: str,
    final_message: str,
    paths: list[str],
) -> dict[str, Any]:
    issue_number = issue["number"]
    title = f"codex: monthly audit fixes for issue #{issue_number}"
    changed_list = "\n".join(f"- `{path}`" for path in paths) or "- None"
    body = "\n".join(
        [
            f"<!-- codex-monthly-remediation:issue-{issue_number} -->",
            "",
            f"Triggered by monthly review issue #{issue_number}: {issue.get('html_url', '')}",
            "",
            "## Changed Files",
            "",
            changed_list,
            "",
            "## Self-hosted Codex Result",
            "",
            truncate_markdown(strip_audit_heading(final_message), 6000)
            or "Codex edited files but did not return a final message.",
        ]
    )
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
    source_ref = env_value("SOURCE_REF", "main")
    mode = env_value("CODEX_AUDIT_MODE", DEFAULT_MODE)
    if mode not in {"review_only", "review_and_fix"}:
        raise BridgeError(f"Unsupported CODEX_AUDIT_MODE: {mode}")
    provider = validate_provider(env_value("CODEX_AUDIT_PROVIDER", DEFAULT_PROVIDER))
    issue_number_raw = env_value("ISSUE_NUMBER")
    if not issue_number_raw.isdigit():
        raise BridgeError("ISSUE_NUMBER must be provided as an integer")
    issue_number = int(issue_number_raw)
    token = env_value("CODEX_AUDIT_GH_TOKEN") or env_value("GH_TOKEN") or env_value("GITHUB_TOKEN")
    if not token:
        raise BridgeError("CODEX_AUDIT_GH_TOKEN or GITHUB_TOKEN is required")
    timeout_minutes = int(env_value("CODEX_AUDIT_TIMEOUT_MINUTES", "45"))
    auto_merge = parse_bool(env_value("CODEX_AUDIT_AUTO_MERGE"))

    print(f"Auditing {source_repo} issue #{issue_number} on {source_ref} in {mode} mode.")
    issue = github_request(token, "GET", f"/repos/{source_repo}/issues/{issue_number}")
    comments = github_request(token, "GET", f"/repos/{source_repo}/issues/{issue_number}/comments?per_page=20")
    if not isinstance(comments, list):
        comments = []

    if provider == "openai":
        review_message = run_openai_review(source_repo, source_ref, issue, comments)
        post_issue_comment(token, source_repo, issue_number, truncate_markdown(review_message))
        return 0

    with tempfile.TemporaryDirectory(prefix="selfhosted-codex-audit-") as tmp:
        work_root = Path(tmp)
        repo_dir = clone_source_repo(token, source_repo, source_ref, work_root)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
        branch_name = f"codex/monthly-review-issue-{issue_number}-{stamp}"
        run_checked(["git", "checkout", "-b", branch_name], cwd=repo_dir)
        run_checked(["git", "config", "user.name", "selfhosted-codex-audit[bot]"], cwd=repo_dir)
        run_checked(
            ["git", "config", "user.email", "selfhosted-codex-audit[bot]@users.noreply.github.com"],
            cwd=repo_dir,
        )

        issue_path, context_path = write_codex_context(repo_dir, source_repo, source_ref, issue, comments)
        prompt = build_prompt(
            source_repo=source_repo,
            source_ref=source_ref,
            issue=issue,
            issue_path=issue_path,
            context_path=context_path,
            mode=mode,
        )
        dependency_env = bootstrap_python_environment()
        return_code, _codex_log, final_message = run_codex(
            repo_dir,
            prompt,
            timeout_minutes,
            env_overrides=dependency_env,
        )
        if return_code != 0:
            if provider == "auto":
                if env_value("OPENAI_API_KEY"):
                    review_message = run_openai_review(source_repo, source_ref, issue, comments)
                    post_issue_comment(
                        token,
                        source_repo,
                        issue_number,
                        "\n".join(
                            [
                                "## API Monthly Review",
                                "",
                                f"Self-hosted Codex failed with exit code `{return_code}`; using the configured OpenAI fallback.",
                                "",
                                truncate_markdown(review_message, 10000),
                            ]
                        ),
                    )
                    return 0
                body = "\n".join(
                    [
                        "## Self-hosted Codex Audit",
                        "",
                        f"Codex execution failed with exit code `{return_code}`.",
                        "",
                        "OpenAI fallback was requested by provider `auto`, but `OPENAI_API_KEY` is not configured in the bridge repository.",
                        "",
                        "No files were pushed. Configure `OPENAI_API_KEY` or inspect the bridge workflow logs.",
                    ]
                )
                post_issue_comment(token, source_repo, issue_number, body)
                return return_code
            body = "\n".join(
                [
                    "## Self-hosted Codex Audit",
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

        denied = blocked_paths(paths)
        if denied:
            denied_list = "\n".join(f"- `{path}`" for path in denied)
            review_message = format_codex_message(final_message, repo_dir, source_repo, source_ref)
            body = "\n".join(
                [
                    "## Self-hosted Codex Audit",
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
            ["git", "commit", "-m", f"codex: monthly audit fixes for issue #{issue_number}"],
            cwd=repo_dir,
        )
        git_with_token(repo_dir, token, ["push", "origin", f"HEAD:refs/heads/{branch_name}"])
        pr_message = format_codex_message(final_message, repo_dir, source_repo, branch_name)
        pr = create_pull_request(token, source_repo, issue, branch_name, source_ref, pr_message, paths)
        pr_url = pr.get("html_url", "")
        body_lines = [
            "## Self-hosted Codex Audit",
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


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
