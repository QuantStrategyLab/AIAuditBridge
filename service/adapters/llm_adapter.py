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
from dataclasses import dataclass
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
    dispatch_started: bool = False
    dispatch_uncertain: bool = False


class LlmAdapterError(RuntimeError):
    """Raised when an LLM API call fails after all retries."""

    def __init__(
        self,
        message: str,
        *,
        dispatch_started: bool = False,
        dispatch_uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.dispatch_started = dispatch_started
        self.dispatch_uncertain = dispatch_uncertain


# ── helpers ────────────────────────────────────────────────────────────


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _scrub_api_keys(text: str) -> str:
    for pattern in _API_KEY_SCRUB_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _should_retry(status_code: int | None) -> bool:
    return status_code is not None and (status_code == 429 or status_code >= 500)


def _retry_with_backoff(fn, *, max_retries: int = DEFAULT_MAX_RETRIES, base_seconds: float = DEFAULT_BACKOFF_BASE):
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except (LlmAdapterError, urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            last_exc = exc
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            if not _should_retry(status) or attempt >= max_retries:
                if isinstance(exc, LlmAdapterError):
                    raise exc
                raise LlmAdapterError(str(exc)) from exc
            wait = base_seconds * (2**attempt)
            _logger.warning("llm_adapter attempt %d/%d failed (status=%s); retrying in %.1fs", attempt + 1, max_retries + 1, status, wait)
            time.sleep(wait)
    raise LlmAdapterError(str(last_exc)) from last_exc


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
) -> str:
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

    def _call() -> str:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _scrub_api_keys(exc.read().decode("utf-8", errors="replace")[:500])
            raise LlmAdapterError(
                f"OpenAI HTTP {exc.code}: {detail}", dispatch_started=True
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LlmAdapterError(
                f"OpenAI network error: {exc}", dispatch_uncertain=True
            ) from exc
        except ValueError as exc:
            raise LlmAdapterError(f"OpenAI request configuration error: {exc}") from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LlmAdapterError("OpenAI returned an invalid response body", dispatch_started=True) from exc
        if not isinstance(payload, dict):
            raise LlmAdapterError("OpenAI returned an invalid response shape", dispatch_started=True)

        choices = payload.get("choices")
        if not choices:
            raise LlmAdapterError("OpenAI returned empty choices", dispatch_started=True)
        message = choices[0].get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else ""
        if not content.strip():
            raise LlmAdapterError("OpenAI returned empty content", dispatch_started=True)
        return content.strip()

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
) -> str:
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

    def _call() -> str:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _scrub_api_keys(exc.read().decode("utf-8", errors="replace")[:500])
            raise LlmAdapterError(
                f"Anthropic HTTP {exc.code}: {detail}", dispatch_started=True
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LlmAdapterError(
                f"Anthropic network error: {exc}", dispatch_uncertain=True
            ) from exc
        except ValueError as exc:
            raise LlmAdapterError(f"Anthropic request configuration error: {exc}") from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LlmAdapterError("Anthropic returned an invalid response body", dispatch_started=True) from exc
        if not isinstance(payload, dict):
            raise LlmAdapterError("Anthropic returned an invalid response shape", dispatch_started=True)

        content = payload.get("content")
        if not isinstance(content, list):
            raise LlmAdapterError("Anthropic returned no content blocks", dispatch_started=True)
        text_parts = [
            str(block.get("text", "")).strip()
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not text_parts:
            raise LlmAdapterError("Anthropic returned no text content", dispatch_started=True)
        return "\n\n".join(text_parts)

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
                output = _anthropic_completion(resolved_model, system, user, max_tokens=max_tokens, timeout=timeout)
            else:
                output = _openai_completion(resolved_model, system, user, max_tokens=max_tokens, timeout=timeout)
            return LlmResult(
                provider=provider,
                model=resolved_model,
                output=output,
                latency_seconds=time.time() - started,
                dispatch_started=True,
            )
        except LlmAdapterError as exc:
            return LlmResult(
                provider=provider,
                model=resolved_model,
                output="",
                success=False,
                error=str(exc),
                latency_seconds=time.time() - started,
                dispatch_started=exc.dispatch_started,
                dispatch_uncertain=exc.dispatch_uncertain,
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

        results: list[LlmResult | None] = [None] * len(reviewers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(reviewers), 4)) as pool:
            futures = {
                pool.submit(self.complete, model=model, system=system, user=user, max_tokens=max_tokens, timeout=timeout): (index, label, model)
                for index, (label, model) in enumerate(reviewers)
            }
            for f in concurrent.futures.as_completed(futures):
                index, label, model = futures[f]
                try:
                    results[index] = f.result()
                except Exception as exc:
                    results[index] = (
                        LlmResult(
                            provider=label,
                            model=model,
                            output="",
                            success=False,
                            error=str(exc),
                        )
                    )
        return [result for result in results if result is not None]
