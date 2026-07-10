#!/usr/bin/env python3
"""Run Codex review on a PR diff and block merge when serious issues are found.

Uses the existing Codex audit service backend (same as monthly reviews).
Evaluates findings against the repo's codex_auto_merge_policy.json.
Exits non-zero when blocked, which fails the GitHub Actions check run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from string import Template
from typing import Any

# ---------------------------------------------------------------------------
# Configuration (aligned with CodexAuditBridge)
# ---------------------------------------------------------------------------

API_BASE = "https://api.github.com"
BRIDGE_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("CODEX_PR_REVIEW_REPO_ROOT") or os.environ.get("GITHUB_WORKSPACE") or Path.cwd()).resolve()
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))
SOURCE_ROOT = BRIDGE_ROOT.parent / "source"
if SOURCE_ROOT.exists() and str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
from service.model_router import route_model  # noqa: E402

POLICY_PATH = ROOT / ".github" / "codex_auto_merge_policy.json"
PROMPT_TEMPLATE_PATH = BRIDGE_ROOT / "prompts" / "pr_review.md"
DEFAULT_SERVICE_AUDIENCE = "quant-codex-audit"
DEFAULT_TIMEOUT_MINUTES = 20
DEFAULT_MAX_CONTEXT_LINES = 800
TASK_COMPLEXITY_LOW = "low"
TASK_COMPLEXITY_MEDIUM = "medium"
TASK_COMPLEXITY_HIGH = "high"
TASK_COMPLEXITY_LEVELS = (TASK_COMPLEXITY_LOW, TASK_COMPLEXITY_MEDIUM, TASK_COMPLEXITY_HIGH)
CODEX_SERVICE_FALLBACK_SIGNALS = (
    "429",
    "too many requests",
    "rate limit",
    "quota",
    "codex exec failed",
)
NO_REVIEW_BACKEND_CONFIGURED = (
    "No Codex service URL or API key configured. "
    "Set CODEX_AUDIT_SERVICE_URL, ANTHROPIC_API_KEY, or OPENAI_API_KEY."
)

# Risk → block mapping
BLOCK_SEVERITIES = frozenset({"critical", "high"})
COMMENT_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
STREAK_MARKER_PREFIX = "<!-- codex-pr-review-streak:"
STREAK_MARKER_SUFFIX = " -->"
FINGERPRINT_MARKER_PREFIX = "<!-- codex-pr-review-fingerprint:"
FINGERPRINT_MARKER_SUFFIX = " -->"
FINGERPRINTS_MARKER_PREFIX = "<!-- codex-pr-review-fingerprints:"
FINGERPRINTS_MARKER_SUFFIX = " -->"
HEAD_SHA_MARKER_PREFIX = "<!-- codex-pr-review-head-sha:"
HEAD_SHA_MARKER_SUFFIX = " -->"
ARBITRATION_REPEAT_THRESHOLD = 2


class ReviewError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def github_request(
    token: str, method: str, path: str, payload: dict[str, Any] | None = None
) -> Any:
    url = path if path.startswith("https://") else f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-pr-review",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"GitHub API {method} {url} failed: {exc.code} {body[:600]}") from exc
    return json.loads(body) if body else {}


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


# ---------------------------------------------------------------------------
# Policy loading (reuses evaluate_codex_pr_merge.py logic)
# ---------------------------------------------------------------------------


def load_policy(token: str = "", repo: str = "", base_ref: str = "") -> dict[str, Any]:
    """Load the risk policy, falling back to safe defaults."""
    if token and repo and base_ref:
        return _load_policy_from_trusted_ref(token, repo, base_ref)

    if not POLICY_PATH.exists():
        return _default_policy()

    try:
        payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _fail_closed("invalid auto-merge policy JSON")

    return _validate_policy_payload(payload)


def _load_policy_from_trusted_ref(token: str, repo: str, ref: str) -> dict[str, Any]:
    path = urllib.parse.quote(".github/codex_auto_merge_policy.json", safe="")
    ref_query = urllib.parse.quote(ref, safe="")
    try:
        payload = github_request(token, "GET", f"/repos/{repo}/contents/{path}?ref={ref_query}")
    except ReviewError as exc:
        if "failed: 404" in str(exc):
            return _default_policy()
        return _fail_closed("could not load trusted auto-merge policy")
    if not isinstance(payload, dict):
        return _fail_closed("invalid trusted auto-merge policy response")
    try:
        encoded = str(payload.get("content") or "")
        raw = base64.b64decode(encoded).decode("utf-8")
        policy = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return _fail_closed("invalid trusted auto-merge policy JSON")
    return _validate_policy_payload(policy)


def _validate_policy_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _fail_closed("invalid auto-merge policy format")
    if payload.get("version") != 1:
        return _fail_closed("unsupported policy version")
    return payload


def _default_policy() -> dict[str, Any]:
    return {
        "version": 1,
        "blocked_path_patterns": [
            r"(^|/)(\.env|.*secret.*|.*credential.*|.*token.*|.*private.*|.*\.pem|.*\.key)$",
        ],
        "risk_policy": {
            "low": {
                "prefixes": ["docs/", "tests/"],
                "exact": ["README.md", "README.zh-CN.md"],
                "reason": "docs/tests/readme-only changes",
            },
            "high": {"reason": "source code changes require review"},
        },
        "max_changed_files": 30,
        "max_changed_lines": 2000,
    }


def _fail_closed(reason: str) -> dict[str, Any]:
    return {
        "policy_errors": [reason],
        "blocked_path_patterns": [r".*"],
        "risk_policy": {
            "low": {"prefixes": [], "exact": [], "reason": reason},
            "high": {"reason": reason},
        },
    }


# ---------------------------------------------------------------------------
# File risk classification
# ---------------------------------------------------------------------------


def classify_file_risk(
    file_path: str, policy: dict[str, Any]
) -> tuple[str, str]:
    """Return (risk_level, reason) for a single file path."""
    policy.get("policy_errors", [])

    # Blocked patterns (secrets, credentials, etc.)
    blocked_patterns = policy.get("blocked_path_patterns", [])
    for pattern in blocked_patterns:
        try:
            if re.search(pattern, file_path, re.IGNORECASE):
                return ("high", f"matches blocked path pattern: {pattern}")
        except re.error:
            continue

    risk_policy = policy.get("risk_policy", {})
    low = risk_policy.get("low", {})
    low_prefixes = low.get("prefixes", [])
    low_exact = set(low.get("exact", []))
    medium_exact = set(
        risk_policy.get("medium", {}).get("exact", [])
    )

    # Normalize path
    normalized = file_path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]

    if not normalized:
        return ("high", "empty path")

    if normalized in low_exact or any(
        normalized.startswith(prefix) for prefix in low_prefixes
    ):
        return ("low", "docs/test/readme change")

    if normalized in medium_exact:
        return ("medium", "monthly-review helper changed")

    return ("high", "source code change")


def changed_files_are_low_risk(paths: list[str], policy: dict[str, Any]) -> bool:
    """Return True when every changed path is low-risk under the policy."""
    return bool(paths) and all(classify_file_risk(path, policy)[0] == TASK_COMPLEXITY_LOW for path in paths)


# ---------------------------------------------------------------------------
# PR diff fetching
# ---------------------------------------------------------------------------


def fetch_pr_diff(token: str, repo: str, pr_number: int) -> str:
    """Fetch the unified diff for a PR."""
    diff_url = f"{API_BASE}/repos/{repo}/pulls/{pr_number}"
    req = urllib.request.Request(
        diff_url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-pr-review",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"Failed to fetch PR diff: {exc.code} {body[:600]}") from exc


def fetch_pr_files(token: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Fetch the list of changed files in a PR."""
    files: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = github_request(
            token,
            "GET",
            f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}",
        )
        if not isinstance(payload, list) or not payload:
            break
        files.extend(payload)
        if len(payload) < 100:
            break
        page += 1
    return files


# ---------------------------------------------------------------------------
# Review prompt
# ---------------------------------------------------------------------------


def build_review_prompt(diff: str, pr_title: str, pr_body: str, repo: str) -> str:
    """Build the Codex review prompt with the PR diff and structured output instructions."""
    diff_limited = _truncate_lines(diff, DEFAULT_MAX_CONTEXT_LINES * 3)

    template = Template(
        """You are reviewing a pull request for a production codebase. Your job is to find bugs, security issues, and logic errors that could cause real problems.

## PR Context

- Repository: ${REPO}
- PR Title: ${TITLE}

${BODY}

## Review Instructions

1. Focus on **security vulnerabilities, logic errors, data corruption, crash bugs, race conditions, and API compatibility breaks**.
2. Do NOT flag: code style, formatting, naming suggestions, minor refactoring preferences, or documentation issues.
3. For each finding, classify its severity:
   - **critical**: security vulnerability, data loss, production crash
   - **high**: logic error that produces wrong results, API break, memory/connection leak
   - **medium**: missing error handling, performance degradation, race condition
   - **low**: misleading comment, unclear variable name, redundant code

## Output Format

Return exactly one JSON object and no surrounding prose:

```json
{
  "summary": "Brief summary of the review (1-3 sentences)",
  "findings": [
    {
      "severity": "critical|high|medium|low",
      "category": "security|bug|performance|logic|reliability",
      "file": "relative/path/to/file.py",
      "line": 42,
      "description": "What the problem is",
      "suggestion": "How to fix it"
    }
  ]
}
```

If there are no findings, return an empty `findings` array.

## PR Diff

```diff
${DIFF}
```"""
    )

    return template.safe_substitute(
        REPO=repo,
        TITLE=pr_title,
        BODY=f"### PR Description\n\n{pr_body}" if pr_body.strip() else "",
        DIFF=diff_limited,
    )


def _truncate_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    half = max_lines // 2
    return (
        "\n".join(lines[:half])
        + f"\n\n... [{len(lines) - max_lines} lines truncated] ...\n\n"
        + "\n".join(lines[-half:])
    )


def _normalize_complexity(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in TASK_COMPLEXITY_LEVELS:
        return normalized
    return ""


def _estimate_review_complexity(
    diff: str,
    changed_files: list[str],
    *,
    title: str = "",
    body: str = "",
) -> str:
    diff_lines = len((diff or "").splitlines())
    file_count = len([f for f in changed_files if f])
    prompt_chars = len((diff or "")) + len((title or "")) + len((body or ""))

    if diff_lines >= 1800 or file_count >= 15 or prompt_chars >= 18000:
        return TASK_COMPLEXITY_HIGH
    if diff_lines >= 600 or file_count >= 6 or prompt_chars >= 7000:
        return TASK_COMPLEXITY_MEDIUM
    return TASK_COMPLEXITY_LOW


def _direct_api_model_for_complexity(provider: str, complexity: str) -> str:
    level = _normalize_complexity(complexity)
    if not level:
        return ""
    prefix = "ANTHROPIC" if provider == "anthropic" else "OPENAI"
    for name in (
        f"CODEX_AUDIT_{prefix}_{level.upper()}_COMPLEXITY_MODEL",
        f"{prefix}_{level.upper()}_COMPLEXITY_MODEL",
        f"{prefix}_MODEL_{level.upper()}",
    ):
        value = env_value(name)
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Codex service integration
# ---------------------------------------------------------------------------


def request_github_oidc_token(audience: str) -> str:
    request_url = env_value("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = env_value("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not request_url or not request_token:
        raise ReviewError(
            "GitHub OIDC environment unavailable. Set permissions: id-token: write."
        )
    separator = "&" if "?" in request_url else "?"
    url = f"{request_url}{separator}audience={urllib.parse.quote(audience)}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {request_token}",
            "Accept": "application/json",
            "User-Agent": "codex-pr-review-oidc",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ReviewError("GitHub OIDC token response missing token value")
    return token


def run_codex_service_review(prompt: str, timeout_minutes: int, complexity: str = "", changed_file_count: int = 0, changed_line_count: int = 0) -> str:
    """Submit a review job to the Codex audit service and wait for completion."""
    service_url = env_value("CODEX_AUDIT_SERVICE_URL")
    if not service_url:
        raise ReviewError("CODEX_AUDIT_SERVICE_URL is not configured")

    service_url = service_url.strip().rstrip("/")
    audience = env_value("CODEX_AUDIT_SERVICE_AUDIENCE", DEFAULT_SERVICE_AUDIENCE)

    # Submit job
    oidc_token = request_github_oidc_token(audience)
    payload = {
        "source_repository": env_value("GITHUB_REPOSITORY"),
        "source_ref": env_value("GITHUB_REF_NAME", "main"),
        "task": "pr_review",
        "mode": "review_only",
        "prompt": prompt,
        "model": route_model("pr_review").get("model", ""),
        "complexity": _normalize_complexity(complexity) or "auto",
        "changed_files": int(changed_file_count),
        "changed_lines": int(changed_line_count),
        "timeout_seconds": timeout_minutes * 60,
    }
    submit_resp = _service_request(
        "POST",
        f"{service_url}/v1/codex-audit/jobs",
        oidc_token,
        payload,
    )
    job_id = submit_resp.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ReviewError("Codex service did not return a job id")

    # Poll for completion
    deadline = time.time() + timeout_minutes * 60 + 120
    poll_interval = 5
    job_url = f"{service_url}/v1/codex-audit/jobs/{job_id}"
    while time.time() < deadline:
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 2, 30)
        job_payload = _service_request("GET", job_url, request_github_oidc_token(audience), None)
        status = job_payload.get("status")
        if status == "succeeded":
            output = job_payload.get("output")
            if not isinstance(output, str):
                raise ReviewError("Codex service response missing text output")
            return output.strip()
        if status == "failed":
            error = str(job_payload.get("error") or "unknown failure")
            raise ReviewError(f"Codex service job failed: {error[:600]}")
        if status not in {"queued", "running"}:
            raise ReviewError(f"Unexpected Codex service status: {status!r}")

    raise ReviewError("Codex service job timed out")


def _service_review_should_fallback(exc: ReviewError) -> bool:
    message = str(exc).lower()
    return any(signal in message for signal in CODEX_SERVICE_FALLBACK_SIGNALS)


def _review_backend_is_unconfigured(exc: ReviewError) -> bool:
    message = str(exc).strip()
    normalized = message.lower()
    return message == NO_REVIEW_BACKEND_CONFIGURED or "oidc repository is not allowed" in normalized


def _api_fallback_enabled() -> bool:
    return parse_bool(env_value("CODEX_PR_REVIEW_API_FALLBACK_ENABLED", "true"))


def _direct_api_primary_enabled() -> bool:
    return parse_bool(env_value("CODEX_PR_REVIEW_DIRECT_API_PRIMARY_ENABLED", "true"))


def run_codex_review_with_fallback(
    prompt: str,
    timeout_minutes: int,
    complexity: str = "",
    changed_file_count: int = 0,
    changed_line_count: int = 0,
) -> str:
    service_url = env_value("CODEX_AUDIT_SERVICE_URL")
    service_failure: Exception | None = None
    if service_url:
        try:
            print(f"Running Codex review via service: {service_url}")
            return run_codex_service_review(
                prompt,
                timeout_minutes,
                complexity=complexity,
                changed_file_count=changed_file_count,
                changed_line_count=changed_line_count,
            )
        except ReviewError as exc:
            if not _service_review_should_fallback(exc):
                raise
            service_failure = exc
            print(f"::warning::Codex service review failed: {exc}")
        except (json.JSONDecodeError, OSError, urllib.error.URLError) as exc:
            service_failure = exc
            print(f"::error::Codex service review failed: {exc}")

    if service_failure is not None and not _api_fallback_enabled():
        raise ReviewError(f"Codex service review failed and direct API fallback is disabled: {service_failure}")
    if not service_url and not _direct_api_primary_enabled():
        raise ReviewError(NO_REVIEW_BACKEND_CONFIGURED)

    print("Running Codex review via direct API")
    try:
        return run_direct_api_review(prompt, complexity=complexity)
    except ReviewError as exc:
        if service_failure is not None and _review_backend_is_unconfigured(exc):
            raise ReviewError(
                f"Codex service review failed and no direct API fallback is configured: {service_failure}"
            ) from exc
        raise


def _service_request(
    method: str, url: str, oidc_token: str, payload: dict[str, Any] | None
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {oidc_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "codex-pr-review-client",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"Codex service request failed: {exc.code} {detail[:600]}") from exc
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ReviewError("Codex service returned invalid JSON")
    return result


# ---------------------------------------------------------------------------
# Direct API review (fallback when service is unavailable)
# ---------------------------------------------------------------------------


def run_direct_api_review(prompt: str, complexity: str = "") -> str:
    """Run review directly via Anthropic or OpenAI API."""
    anthropic_key = env_value("ANTHROPIC_API_KEY")
    openai_key = env_value("OPENAI_API_KEY")

    provider_order = [
        "openai",
        "anthropic",
    ]
    normalized = _normalize_complexity(complexity)
    if normalized in (TASK_COMPLEXITY_HIGH, TASK_COMPLEXITY_MEDIUM):
        provider_order = ["anthropic", "openai"]

    for provider in provider_order:
        if provider == "anthropic" and anthropic_key:
            return _run_anthropic_review(
                prompt,
                anthropic_key,
                model=_direct_api_model_for_complexity(provider, normalized),
            )
        if provider == "openai" and openai_key:
            return _run_openai_review(
                prompt,
                openai_key,
                model=_direct_api_model_for_complexity(provider, normalized),
            )

    raise ReviewError(NO_REVIEW_BACKEND_CONFIGURED)


def _run_anthropic_review(prompt: str, api_key: str, model: str = "") -> str:
    model = env_value("ANTHROPIC_MODEL", "claude-sonnet-4-6") if not model else model
    system = "You are a careful code reviewer. Return only the JSON object as specified."
    payload = {
        "model": model,
        "max_tokens": 4000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "codex-pr-review",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"Anthropic API failed: {exc.code} {detail[:600]}") from exc

    content = body.get("content", [])
    if not isinstance(content, list):
        raise ReviewError("Unexpected Anthropic response format")
    text_parts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n\n".join(text_parts)


def _run_openai_review(prompt: str, api_key: str, model: str = "") -> str:
    model = env_value("OPENAI_MODEL", "gpt-5.4-mini") if not model else model
    system = "You are a careful code reviewer. Return only the JSON object as specified."
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        f"{env_value('OPENAI_API_BASE_URL', 'https://api.openai.com/v1').rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-pr-review",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"OpenAI API failed: {exc.code} {detail[:600]}") from exc

    choices = body.get("choices", [])
    if not isinstance(choices, list) or not choices:
        raise ReviewError("Unexpected OpenAI response format")
    message = choices[0].get("message", {})
    return str(message.get("content", ""))


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_review_output(text: str, *, require_findings: bool = True) -> dict[str, Any]:
    """Extract the JSON review result from Codex/API output."""
    stripped = text.strip()

    # Try to extract from markdown code fence
    fence_match = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE
    )
    if fence_match:
        stripped = fence_match.group(1).strip()

    # Try to find JSON object boundaries
    if not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            stripped = stripped[start : end + 1]

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            payload, _end = json.JSONDecoder().raw_decode(stripped.lstrip())
        except json.JSONDecodeError:
            raise ReviewError(f"Failed to parse Codex review output as JSON: {stripped[:500]}")

    if not isinstance(payload, dict):
        raise ReviewError("Review output is not a JSON object")
    if require_findings and not isinstance(payload.get("findings"), list):
        raise ReviewError("Review output findings must be a JSON array")

    return payload


def parse_arbitration_output(text: str) -> dict[str, str]:
    """Parse the independent arbiter's constrained verdict."""
    payload = parse_review_output(text, require_findings=False)
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"clear", "block", "ambiguous"}:
        raise ReviewError("Arbitration output verdict must be clear, block, or ambiguous")
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise ReviewError("Arbitration output reason is required")
    return {"verdict": verdict, "reason": reason}


def blocking_finding_fingerprint(findings: list[dict[str, Any]]) -> str:
    """Return a stable arbitration-candidate identifier despite wording drift."""
    normalized: list[dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        normalized.append(
            {
                "category": str(finding.get("category") or "").strip().lower(),
                "file": str(finding.get("file") or "").strip(),
                "severity": str(finding.get("severity") or "").strip().lower(),
            }
        )
    if not normalized:
        return ""
    payload = json.dumps(sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True)), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def blocking_finding_fingerprints(findings: list[dict[str, Any]]) -> tuple[str, ...]:
    """Return per-finding keys so unrelated findings do not reset arbitration state."""
    return tuple(
        sorted(
            {
                fingerprint
                for finding in findings
                if isinstance(finding, dict)
                for fingerprint in [blocking_finding_fingerprint([finding])]
                if fingerprint
            }
        )
    )


def build_arbitration_prompt(
    *,
    repo: str,
    pr_title: str,
    diff: str,
    findings: list[dict[str, Any]],
) -> str:
    """Ask an independent Codex pass to adjudicate repeated primary findings."""
    diff_limited = _truncate_lines(diff, DEFAULT_MAX_CONTEXT_LINES * 3)
    findings_json = json.dumps(findings, ensure_ascii=False, indent=2)
    return f"""You are the independent Codex review arbiter for a production quantitative codebase.

The primary reviewer repeatedly raised the blocking findings below. Decide whether every blocking finding remains valid against the current PR diff. Do not defer to the primary reviewer. Require concrete evidence in the changed code or test contract.

Repository: {repo}
PR title: {pr_title}

## Primary blocking findings
{findings_json}

## Current PR diff
{diff_limited}

Return exactly one JSON object:
{{
  "verdict": "clear" | "block" | "ambiguous",
  "reason": "Concrete evidence for the verdict."
}}

Use `clear` only when all blocking findings are false positives, obsolete, or demonstrably fixed. Use `block` when any blocking finding remains valid. Use `ambiguous` if evidence is insufficient. Do not discuss style or test coverage.
"""


# ---------------------------------------------------------------------------
# Findings evaluation
# ---------------------------------------------------------------------------


def evaluate_findings(
    findings: list[dict[str, Any]],
    changed_files: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate Codex findings against the risk policy.

    Returns a decision dict with:
    - blocked: whether merge should be blocked
    - blocking_findings: findings that cause blocking
    - non_blocking_findings: findings that are reported but don't block
    - risk_summary: human-readable summary
    """
    blocking: list[dict[str, Any]] = []
    non_blocking: list[dict[str, Any]] = []
    file_risk_cache: dict[str, tuple[str, str]] = {}

    # Build a set of changed file paths
    changed_paths: set[str] = set()
    file_statuses: dict[str, str] = {}
    for f in changed_files:
        path = f.get("filename", "").strip()
        if path:
            changed_paths.add(path)
            file_statuses[path] = f.get("status", "")

    for finding in findings:
        if not isinstance(finding, dict):
            continue

        severity = str(finding.get("severity", "")).strip().lower()
        file_path = str(finding.get("file", "")).strip()

        # Classify the file's risk level
        if file_path not in file_risk_cache:
            file_risk_cache[file_path] = classify_file_risk(file_path, policy)
        file_risk, file_risk_reason = file_risk_cache[file_path]

        # Determine if this finding should block
        should_block = (
            severity in BLOCK_SEVERITIES
            and file_risk == "high"
            and file_path in changed_paths  # only block on actually changed files
        )

        enriched = {
            **finding,
            "file_risk": file_risk,
            "file_risk_reason": file_risk_reason,
        }

        if should_block:
            blocking.append(enriched)
        else:
            non_blocking.append(enriched)

    blocked = len(blocking) > 0

    # Build summary
    all_findings = blocking + non_blocking
    summary_parts = []
    if blocked:
        summary_parts.append(
            f"🚫 **Merge blocked**: {len(blocking)} serious issue(s) found in high-risk files"
        )
    elif all_findings:
        total = len(all_findings)
        summary_parts.append(
            f"✅ **Merge allowed**: {total} finding(s) reported but none are blocking"
        )
    else:
        summary_parts.append("✅ **Merge allowed**: No issues found")

    return {
        "blocked": blocked,
        "blocking_findings": blocking,
        "non_blocking_findings": non_blocking,
        "total_findings": len(all_findings),
        "summary": "\n\n".join(summary_parts),
    }


# ---------------------------------------------------------------------------
# PR comment
# ---------------------------------------------------------------------------


def build_pr_comment(
    decision: dict[str, Any],
    pr_url: str,
    *,
    blocking_streak: int = 0,
    finding_fingerprint: str = "",
    finding_fingerprints: tuple[str, ...] = (),
    reviewed_head_sha: str = "",
    arbitration: dict[str, str] | None = None,
) -> str:
    """Build a markdown comment to post on the PR."""
    lines = [
        "<!-- codex-pr-review -->",
        f"{STREAK_MARKER_PREFIX}{int(blocking_streak)}{STREAK_MARKER_SUFFIX}",
        f"{FINGERPRINT_MARKER_PREFIX}{finding_fingerprint}{FINGERPRINT_MARKER_SUFFIX}",
        f"{FINGERPRINTS_MARKER_PREFIX}{','.join(finding_fingerprints)}{FINGERPRINTS_MARKER_SUFFIX}",
        f"{HEAD_SHA_MARKER_PREFIX}{reviewed_head_sha}{HEAD_SHA_MARKER_SUFFIX}",
        "## 🤖 Codex PR Review",
        "",
        decision["summary"],
        "",
    ]

    if arbitration:
        verdict = arbitration.get("verdict", "")
        reason = arbitration.get("reason", "")
        emoji = "✅" if verdict == "clear" else "🚫" if verdict == "block" else "⚠️"
        lines.extend(
            [
                "### ⚖️ Codex Review Arbitration",
                "",
                f"{emoji} **{verdict or 'error'}**: {reason}",
                "",
            ]
        )
    blocking = decision["blocking_findings"]
    if blocking:
        lines.extend([
            "### 🚫 Blocking Issues",
            "",
            (
                "These primary findings were cleared by independent Codex arbitration:"
                if arbitration and arbitration.get("verdict") == "clear"
                else "These issues must be fixed before this PR can be merged:"
            ),
            "",
        ])
        for i, f in enumerate(blocking, 1):
            lines.extend(_format_finding(i, f))

    non_blocking = decision["non_blocking_findings"]
    if non_blocking:
        lines.extend([
            "### ℹ️ Other Findings",
            "",
        ])
        for i, f in enumerate(non_blocking, 1):
            lines.extend(_format_finding(i, f))

    lines.extend([
        "---",
        f"*Review by Codex PR Review bot • [PR]({pr_url})*",
    ])

    return "\n".join(lines)


def _format_finding(index: int, finding: dict[str, Any]) -> list[str]:
    severity = finding.get("severity", "unknown")
    category = finding.get("category", "general")
    file_path = finding.get("file", "?")
    line = finding.get("line")
    description = finding.get("description", "No description")
    suggestion = finding.get("suggestion", "")

    emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(severity, "⚪")

    lines = [
        f"#### {index}. {emoji} [{severity.upper()}] {category.title()} in `{file_path}`",
        "",
        f"> {description}",
    ]
    if line:
        lines[-1] += f" (line {line})"
    if suggestion:
        lines.extend(["", f"**Suggestion:** {suggestion}"])
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Existing comment management
# ---------------------------------------------------------------------------


def find_existing_review_comment(
    token: str, repo: str, pr_number: int
) -> tuple[int | None, str]:
    """Find an existing Codex review comment on the PR.

    Returns ``(comment_id, body)``. ``comment_id`` is ``None`` when absent.
    """
    marker = "<!-- codex-pr-review -->"
    page = 1
    while True:
        comments = github_request(
            token,
            "GET",
            f"/repos/{repo}/issues/{pr_number}/comments?per_page=100&page={page}&sort=created&direction=desc",
        )
        if not isinstance(comments, list):
            break
        for comment in comments:
            if _is_trusted_review_comment(comment) and marker in str(comment.get("body", "")):
                return comment.get("id"), str(comment.get("body") or "")
        if len(comments) < 100:
            break
        page += 1
    return None, ""


def _is_trusted_review_comment(comment: Any) -> bool:
    """Accept review state only from the GitHub Actions identity that writes it."""
    if not isinstance(comment, dict):
        return False
    user = comment.get("user")
    if not isinstance(user, dict):
        return False
    expected_login = env_value("CODEX_PR_REVIEW_COMMENT_AUTHOR", "github-actions[bot]").strip().casefold()
    actual_login = str(user.get("login") or "").strip().casefold()
    return bool(expected_login and actual_login == expected_login)


def parse_blocking_streak(body: str) -> int:
    """Read consecutive blocking-round counter from a prior review comment."""
    match = re.search(
        rf"{re.escape(STREAK_MARKER_PREFIX)}(\d+){re.escape(STREAK_MARKER_SUFFIX)}",
        body or "",
    )
    if not match:
        return 0
    try:
        return max(0, int(match.group(1)))
    except ValueError:
        return 0


def parse_blocking_fingerprint(body: str) -> str:
    """Read the primary finding fingerprint from a prior review comment."""
    match = re.search(
        rf"{re.escape(FINGERPRINT_MARKER_PREFIX)}([0-9a-f]*){re.escape(FINGERPRINT_MARKER_SUFFIX)}",
        body or "",
    )
    return match.group(1) if match else ""


def parse_blocking_fingerprints(body: str) -> tuple[str, ...]:
    """Read per-finding keys, including comments written before the new marker."""
    match = re.search(
        rf"{re.escape(FINGERPRINTS_MARKER_PREFIX)}([0-9a-f,]*){re.escape(FINGERPRINTS_MARKER_SUFFIX)}",
        body or "",
    )
    if match:
        return tuple(sorted({item for item in match.group(1).split(",") if re.fullmatch(r"[0-9a-f]{20}", item)}))

    legacy_findings: list[dict[str, str]] = []
    for severity, category, file_path in re.findall(
        r"^#### \d+\. .*?\[([A-Z]+)\] (.+?) in `([^`]+)`$",
        body or "",
        flags=re.MULTILINE,
    ):
        legacy_findings.append(
            {"severity": severity.lower(), "category": category.lower(), "file": file_path}
        )
    return blocking_finding_fingerprints(legacy_findings)


def parse_reviewed_head_sha(body: str) -> str:
    """Read the reviewed pull-request head SHA from a prior trusted comment."""
    match = re.search(
        rf"{re.escape(HEAD_SHA_MARKER_PREFIX)}([0-9a-f]{{7,64}}){re.escape(HEAD_SHA_MARKER_SUFFIX)}",
        body or "",
    )
    return match.group(1) if match else ""


def next_blocking_streak(
    previous_streak: int,
    *,
    blocked: bool,
    previous_fingerprint: str = "",
    current_fingerprint: str = "",
    previous_head_sha: str = "",
    current_head_sha: str = "",
) -> int:
    """Advance only when the same finding is reviewed on a new PR head."""
    if not blocked:
        return 0
    if previous_fingerprint and current_fingerprint and previous_fingerprint == current_fingerprint:
        if previous_head_sha and current_head_sha and previous_head_sha != current_head_sha:
            return previous_streak + 1
        return max(1, previous_streak)
    return 1


def should_arbitrate(*, blocked: bool, streak: int, repeated: bool, new_head: bool) -> bool:
    """Arbitrate only after the same finding survives a new author commit."""
    return bool(blocked and repeated and new_head and streak >= ARBITRATION_REPEAT_THRESHOLD)


def upsert_pr_comment(
    token: str, repo: str, pr_number: int, body: str
) -> None:
    """Create or update the Codex review comment on the PR."""
    existing_id, _existing_body = find_existing_review_comment(token, repo, pr_number)
    if existing_id:
        github_request(
            token,
            "PATCH",
            f"/repos/{repo}/issues/comments/{existing_id}",
            {"body": body},
        )
        print(f"Updated existing review comment #{existing_id}")
    else:
        github_request(
            token,
            "POST",
            f"/repos/{repo}/issues/{pr_number}/comments",
            {"body": body},
        )
        print("Posted new review comment")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    token = env_value("GH_TOKEN") or env_value("GITHUB_TOKEN")
    if not token:
        print("::error::GH_TOKEN or GITHUB_TOKEN is required", file=sys.stderr)
        return 1

    repo = env_value("GITHUB_REPOSITORY")
    if not repo:
        print("::error::GITHUB_REPOSITORY is not set", file=sys.stderr)
        return 1

    # Get PR context from the event
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    if not event_path.exists():
        print("::error::GITHUB_EVENT_PATH not found", file=sys.stderr)
        return 1

    event = json.loads(event_path.read_text(encoding="utf-8"))
    pr = event.get("pull_request") or {}
    pr_number = pr.get("number")
    if not pr_number:
        print("::error::No pull request number in event", file=sys.stderr)
        return 1

    pr_title = str(pr.get("title", ""))
    pr_body = str(pr.get("body", ""))
    pr_url = str(pr.get("html_url", ""))
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    current_head_sha = str(head.get("sha") or "").strip().lower()

    print(f"Reviewing PR #{pr_number}: {pr_title}")

    # Fetch changed files for risk classification
    changed_files = fetch_pr_files(token, repo, pr_number)
    changed_paths = [f.get("filename", "") for f in changed_files]
    print(f"Changed files ({len(changed_paths)}): {', '.join(changed_paths[:10])}"
          + (f" and {len(changed_paths) - 10} more..." if len(changed_paths) > 10 else ""))

    # Load policy from the trusted base ref. The PR head checkout is untrusted
    # and may include policy changes that should be reviewed as data, not used
    # as live guardrail configuration for this same review.
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    policy = load_policy(
        token,
        str(base_repo.get("full_name") or repo),
        str(base.get("sha") or ""),
    )
    if policy.get("policy_errors"):
        print(f"::warning::Policy errors: {policy['policy_errors']}")

    try:
        _existing_id, previous_comment = find_existing_review_comment(token, repo, pr_number)
    except ReviewError as exc:
        print(f"::warning::Failed to fetch prior review comment: {exc}")
        previous_comment = ""
    previous_streak = parse_blocking_streak(previous_comment)
    previous_fingerprint = parse_blocking_fingerprint(previous_comment)
    previous_fingerprints = parse_blocking_fingerprints(previous_comment)
    previous_head_sha = parse_reviewed_head_sha(previous_comment)

    # First pass: classify files. If all files are low-risk, skip review.
    all_low_risk = changed_files_are_low_risk(changed_paths, policy)
    if all_low_risk and changed_paths:
        print("All changed files are low-risk (docs/tests). Skipping Codex review.")
        decision = {
            "blocked": False,
            "blocking_findings": [],
            "non_blocking_findings": [],
            "total_findings": 0,
            "summary": "✅ **Merge allowed**: All changes are in docs/tests — Codex review skipped.",
        }
        upsert_pr_comment(
            token,
            repo,
            pr_number,
            build_pr_comment(
                decision,
                pr_url,
                blocking_streak=0,
                reviewed_head_sha=current_head_sha,
            ),
        )
        return 0

    # Fetch PR diff
    diff = fetch_pr_diff(token, repo, pr_number)
    print(f"Fetched diff: {len(diff)} chars, {len(diff.splitlines())} lines")

    # Build review prompt
    prompt = build_review_prompt(diff, pr_title, pr_body, repo)
    print(f"Built review prompt: {len(prompt)} chars")

    # Run Codex review
    try:
        complexity = _estimate_review_complexity(diff, changed_paths, title=pr_title, body=pr_body)
        output = run_codex_review_with_fallback(
            prompt,
            DEFAULT_TIMEOUT_MINUTES,
            complexity=complexity,
            changed_file_count=len(changed_paths),
            changed_line_count=len(diff.splitlines()),
        )
    except ReviewError as exc:
        print(f"::error::Codex review failed: {exc}", file=sys.stderr)
        warning_body = (
            "<!-- codex-pr-review -->\n"
            "## 🤖 Codex PR Review\n\n"
            "🚫 **Merge blocked**: The Codex review could not be completed.\n\n"
            f"```\n{exc}\n```\n\n"
            "The review check fails closed until a valid Codex review is available.\n"
        )
        upsert_pr_comment(token, repo, pr_number, warning_body)
        return 1

    print(f"Codex output: {len(output)} chars")

    # Parse review output
    try:
        review = parse_review_output(output)
    except ReviewError as exc:
        print(f"::warning::Failed to parse review output: {exc}")
        # Post raw output as comment
        raw_body = (
            "<!-- codex-pr-review -->\n"
            "## 🤖 Codex PR Review\n\n"
            "⚠️ **Review completed** but output could not be parsed for automated blocking.\n\n"
            "<details><summary>Raw review output</summary>\n\n"
            f"{output[:8000]}\n\n"
            "</details>\n"
        )
        upsert_pr_comment(token, repo, pr_number, raw_body)
        return 1

    findings = review.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    print(f"Found {len(findings)} issue(s)")

    # Evaluate findings
    decision = evaluate_findings(findings, changed_files, policy)
    finding_fingerprint = blocking_finding_fingerprint(decision["blocking_findings"])
    finding_fingerprints = blocking_finding_fingerprints(decision["blocking_findings"])
    repeated_fingerprints = tuple(sorted(set(finding_fingerprints).intersection(previous_fingerprints)))
    repeated_finding = bool(
        decision["blocked"]
        and repeated_fingerprints
    )
    new_head = bool(previous_head_sha and current_head_sha and previous_head_sha != current_head_sha)
    blocking_streak = next_blocking_streak(
        previous_streak,
        blocked=bool(decision["blocked"]),
        previous_fingerprint=repeated_fingerprints[0] if repeated_fingerprints else previous_fingerprint,
        current_fingerprint=repeated_fingerprints[0] if repeated_fingerprints else finding_fingerprint,
        previous_head_sha=previous_head_sha,
        current_head_sha=current_head_sha,
    )
    arbitration: dict[str, str] | None = None
    if should_arbitrate(
        blocked=bool(decision["blocked"]),
        streak=blocking_streak,
        repeated=repeated_finding,
        new_head=new_head,
    ):
        arbitration_prompt = build_arbitration_prompt(
            repo=repo,
            pr_title=pr_title,
            diff=diff,
            findings=decision["blocking_findings"],
        )
        try:
            arbitration = parse_arbitration_output(
                run_codex_review_with_fallback(
                    arbitration_prompt,
                    DEFAULT_TIMEOUT_MINUTES,
                    complexity=TASK_COMPLEXITY_HIGH,
                    changed_file_count=len(changed_paths),
                    changed_line_count=len(diff.splitlines()),
                )
            )
        except ReviewError as exc:
            arbitration = {"verdict": "ambiguous", "reason": f"Arbitration failed closed: {exc}"}
        if arbitration.get("verdict") == "clear":
            decision["blocked"] = False
            blocking_streak = 0
            decision["summary"] = "✅ **Merge allowed**: repeated primary findings were cleared by independent Codex arbitration"
    # Post comment
    comment_body = build_pr_comment(
        decision,
        pr_url,
        blocking_streak=blocking_streak,
        finding_fingerprint=finding_fingerprint,
        finding_fingerprints=finding_fingerprints,
        reviewed_head_sha=current_head_sha,
        arbitration=arbitration,
    )
    upsert_pr_comment(token, repo, pr_number, comment_body)

    # Write decision for downstream use
    output_dir = Path("data/output/codex_pr_review")
    output_dir.mkdir(parents=True, exist_ok=True)
    decision_payload = {
        **decision,
        "blocking_streak": blocking_streak,
        "finding_fingerprint": finding_fingerprint,
        "finding_fingerprints": finding_fingerprints,
        "repeated_fingerprints": repeated_fingerprints,
        "repeated_finding": repeated_finding,
        "previous_head_sha": previous_head_sha,
        "current_head_sha": current_head_sha,
        "new_head": new_head,
        "arbitration": arbitration,
    }
    (output_dir / "decision.json").write_text(
        json.dumps(decision_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Output for GitHub Actions
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"blocked={'true' if decision['blocked'] else 'false'}\n")
            f.write(f"total_findings={decision['total_findings']}\n")
            f.write(f"blocking_count={len(decision['blocking_findings'])}\n")
            f.write(f"blocking_streak={blocking_streak}\n")
            f.write(f"repeated_finding={'true' if repeated_finding else 'false'}\n")
            f.write(f"new_head={'true' if new_head else 'false'}\n")
            f.write(f"arbitration_verdict={arbitration.get('verdict', '') if arbitration else ''}\n")

    if decision["blocked"]:
        print(
            f"::error::Merge blocked: serious issues found "
            f"(repeated-finding streak {blocking_streak})"
        )
        return 1

    print("Review passed: no blocking issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
