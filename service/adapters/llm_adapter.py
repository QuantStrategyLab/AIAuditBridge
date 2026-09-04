"""LlmAdapter — unified Claude (Anthropic) / GPT (OpenAI) API adapter.

API keys live only on the VPS. Callers never see them.

Consumed by:
- POST /v1/ai/analyze  (sync, single-model)
- POST /v1/ai/review    (sync, parallel multi-model — see LlmAdapter.parallel_review())
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Sequence

_logger = logging.getLogger(__name__)

PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4000
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE = 1.0

# Patterns that may appear in upstream error responses and must be scrubbed.
_API_KEY_SCRUB_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[a-zA-Z0-9]{32,}", re.IGNORECASE),
    re.compile(r"sk-ant-[a-zA-Z0-9_\-]{32,}", re.IGNORECASE),
    re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.=]{20,}", re.IGNORECASE),
    re.compile(r"x-api-key:\s*[^\s,;]{20,}", re.IGNORECASE),
]

SECRET_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "CREDENTIAL", "API_KEY")


@dataclass(frozen=True)
class LlmResult:
    """Result from a single LLM completion."""

    provider: str  # "openai" | "anthropic"
    model: str
    output: str
    success: bool = True
    error: str = ""
    latency_seconds: float = 0.0
    actual_provider: str = ""
    actual_model: str = ""
    tokens_input: int | None = None
    tokens_output: int | None = None
    usage_complete: bool = False


@dataclass(frozen=True)
class ProviderCompletion:
    """Provider response fields needed to attest the actual model identity."""

    provider: str
    model: str
    output: str
    tokens_input: int | None = None
    tokens_output: int | None = None
    usage_complete: bool = False


class LlmAdapterError(RuntimeError):
    """Raised when an LLM API call fails, retaining any reported token usage."""

    def __init__(self, message: str, *, tokens_input: int | None = None,
                 tokens_output: int | None = None, usage_complete: bool = False) -> None:
        super().__init__(message)
        self.tokens_input = tokens_input
        self.tokens_output = tokens_output
        self.usage_complete = usage_complete


# ── helpers ────────────────────────────────────────────────────────────


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _scrub_api_keys(text: str) -> str:
    for pattern in _API_KEY_SCRUB_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _should_retry(status_code: int | None) -> bool:
    return status_code is not None and (status_code == 429 or status_code >= 500)


def _reported_usage(payload: object, provider: str) -> dict:
    """Keep only nonnegative provider-reported counts, never character estimates."""
    raw = payload.get("usage") if isinstance(payload, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    input_key, output_key = ("prompt_tokens", "completion_tokens") if provider == PROVIDER_OPENAI else ("input_tokens", "output_tokens")

    def count(value):
        return value if type(value) is int and value >= 0 else None

    tokens_input, tokens_output = count(raw.get(input_key)), count(raw.get(output_key))
    if provider == PROVIDER_ANTHROPIC and tokens_input is not None:
        # Anthropic reports cache input separately; this is a token total, not
        # cache-tier billing. Omitted optional cache counters contribute nothing.
        for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if key in raw:
                cached = count(raw[key])
                if cached is None:
                    tokens_input = None
                    break
                tokens_input += cached
    return {"tokens_input": tokens_input, "tokens_output": tokens_output,
            "usage_complete": tokens_input is not None and tokens_output is not None}


def _http_error_detail(exc: urllib.error.HTTPError, provider: str) -> tuple[str, dict]:
    body = exc.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    return _scrub_api_keys(body[:500]), _reported_usage(payload, provider)


def _retry_with_backoff(fn, *, max_retries: int = DEFAULT_MAX_RETRIES, base_seconds: float = DEFAULT_BACKOFF_BASE):
    tokens_input = tokens_output = None
    usage_complete = True

    def accumulate(result):
        nonlocal tokens_input, tokens_output, usage_complete
        reported_input = getattr(result, "tokens_input", None)
        reported_output = getattr(result, "tokens_output", None)
        if reported_input is not None:
            tokens_input = (tokens_input or 0) + reported_input
        if reported_output is not None:
            tokens_output = (tokens_output or 0) + reported_output
        usage_complete = usage_complete and getattr(result, "usage_complete", False)
        return {"tokens_input": tokens_input, "tokens_output": tokens_output, "usage_complete": usage_complete}

    for attempt in range(max_retries + 1):
        try:
            result = fn()
            return replace(result, **accumulate(result))
        except (LlmAdapterError, urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            usage = accumulate(exc)
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            if not _should_retry(status) or attempt >= max_retries:
                raise LlmAdapterError(str(exc), **usage) from exc
            wait = base_seconds * (2**attempt)
            _logger.warning("llm_adapter attempt %d/%d failed (status=%s); retrying in %.1fs", attempt + 1, max_retries + 1, status, wait)
            time.sleep(wait)
    raise LlmAdapterError("LLM retry limit is invalid")


def resolve_model(model: str) -> tuple[str, str]:
    """Return (provider, resolved_model)."""
    m = model.strip().lower()
    if m.startswith("claude"):
        return PROVIDER_ANTHROPIC, model.strip() or DEFAULT_ANTHROPIC_MODEL
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3"):
        return PROVIDER_OPENAI, model.strip() or DEFAULT_OPENAI_MODEL
    # default to anthropic
    return PROVIDER_ANTHROPIC, DEFAULT_ANTHROPIC_MODEL


# ── OpenAI ─────────────────────────────────────────────────────────────


def _openai_chat_url() -> str:
    base = _env("OPENAI_API_BASE_URL", DEFAULT_OPENAI_BASE_URL).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _openai_completion(
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = DEFAULT_TIMEOUT,
) -> ProviderCompletion:
    api_key = _env("OPENAI_API_KEY")
    if not api_key:
        raise LlmAdapterError("OPENAI_API_KEY is not configured on the service host")

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        _openai_chat_url(),
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "AiGateway-LlmAdapter/1.0",
        },
    )

    def _call() -> ProviderCompletion:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail, usage = _http_error_detail(exc, PROVIDER_OPENAI)
            raise LlmAdapterError(f"OpenAI HTTP {exc.code}: {detail}", **usage) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LlmAdapterError(f"OpenAI network error: {exc}") from exc

        usage = _reported_usage(payload, PROVIDER_OPENAI)
        choices = payload.get("choices")
        if not choices:
            raise LlmAdapterError("OpenAI returned empty choices", **usage)
        message = choices[0].get("message", {}) if isinstance(choices, list) and isinstance(choices[0], dict) else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        if not isinstance(content, str) or not content.strip():
            raise LlmAdapterError("OpenAI returned empty content", **usage)
        actual_model = str(payload.get("model") or "").strip()
        return ProviderCompletion(
            provider=PROVIDER_OPENAI,
            model=actual_model,
            output=content.strip(),
            **usage,
        )

    return _retry_with_backoff(_call)


# ── Anthropic ──────────────────────────────────────────────────────────


def _anthropic_messages_url() -> str:
    base = _env("ANTHROPIC_API_BASE_URL", DEFAULT_ANTHROPIC_BASE_URL).rstrip("/")
    if base.endswith("/messages"):
        return base
    return f"{base}/messages"


def _anthropic_completion(
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = DEFAULT_TIMEOUT,
) -> ProviderCompletion:
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        raise LlmAdapterError("ANTHROPIC_API_KEY is not configured on the service host")

    api_version = _env("ANTHROPIC_VERSION", DEFAULT_ANTHROPIC_VERSION)
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        _anthropic_messages_url(),
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": api_version,
            "Content-Type": "application/json",
            "User-Agent": "AiGateway-LlmAdapter/1.0",
        },
    )

    def _call() -> ProviderCompletion:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail, usage = _http_error_detail(exc, PROVIDER_ANTHROPIC)
            raise LlmAdapterError(f"Anthropic HTTP {exc.code}: {detail}", **usage) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LlmAdapterError(f"Anthropic network error: {exc}") from exc

        usage = _reported_usage(payload, PROVIDER_ANTHROPIC)
        content = payload.get("content")
        if not isinstance(content, list):
            raise LlmAdapterError("Anthropic returned no content blocks", **usage)
        text_parts = [
            str(block.get("text", "")).strip()
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not text_parts:
            raise LlmAdapterError("Anthropic returned no text content", **usage)
        actual_model = str(payload.get("model") or "").strip()
        return ProviderCompletion(
            provider=PROVIDER_ANTHROPIC,
            model=actual_model,
            output="\n\n".join(text_parts),
            **usage,
        )

    return _retry_with_backoff(_call)


# ── Adapter ────────────────────────────────────────────────────────────


class LlmAdapter:
    """Unified adapter for Claude (Anthropic) and GPT (OpenAI) API calls.

    Usage::

        adapter = LlmAdapter()
        result = adapter.complete(model="claude-sonnet-4-6", system="...", user="...")
        # or
        results = adapter.parallel_review(
            reviewers=[("claude", "claude-sonnet-4-6"), ("gpt", "gpt-5.4-mini")],
            system="...",
            user="...",
        )
    """

    def complete(
        self,
        *,
        model: str,
        system: str = "",
        user: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> LlmResult:
        """Single-model synchronous completion.

        ``model`` is auto-routed: ``claude-*`` → Anthropic, ``gpt-*`` → OpenAI.
        """
        provider, resolved_model = resolve_model(model)
        started = time.time()
        try:
            if provider == PROVIDER_ANTHROPIC:
                completion = _anthropic_completion(resolved_model, system, user, max_tokens=max_tokens, timeout=timeout)
            else:
                completion = _openai_completion(resolved_model, system, user, max_tokens=max_tokens, timeout=timeout)
            return LlmResult(
                provider=provider,
                model=resolved_model,
                output=completion.output,
                latency_seconds=time.time() - started,
                actual_provider=completion.provider,
                actual_model=completion.model,
                tokens_input=completion.tokens_input,
                tokens_output=completion.tokens_output,
                usage_complete=completion.usage_complete,
            )
        except LlmAdapterError as exc:
            return LlmResult(
                provider=provider,
                model=resolved_model,
                output="",
                success=False,
                error=str(exc),
                tokens_input=exc.tokens_input,
                tokens_output=exc.tokens_output,
                usage_complete=exc.usage_complete,
                latency_seconds=time.time() - started,
            )

    def parallel_review(
        self,
        *,
        reviewers: Sequence[tuple[str, str]],  # [(provider_label, model), ...]
        system: str = "",
        user: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> list[LlmResult]:
        """Run multiple LLM completions concurrently (ThreadPoolExecutor).

        Used by ``POST /v1/ai/review`` for multi-model adversarial review.
        """
        import concurrent.futures

        results: list[LlmResult] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(reviewers), 4)) as pool:
            futures = {
                pool.submit(self.complete, model=model, system=system, user=user, max_tokens=max_tokens, timeout=timeout): (label, model)
                for label, model in reviewers
            }
            for f in concurrent.futures.as_completed(futures):
                try:
                    results.append(f.result())
                except Exception as exc:
                    _label, model = futures[f]
                    results.append(
                        LlmResult(
                            provider=resolve_model(model)[0],
                            model=resolve_model(model)[1],
                            output="",
                            success=False,
                            error=str(exc),
                        )
                    )
        return results
