"""CodexAdapter — wraps ``codex exec`` subprocess on the VPS.

Extracted from the original ``_run_codex()`` in codex_audit_service.py.

Consumed by:
- POST /v1/ai/execute/jobs  (async job submission)
- POST /v1/ai/review          (optional Codex verification step)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SECRET_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "CREDENTIAL", "API_KEY", "ADMIN_KEY")
CODEX_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})
_AUTO_MODEL_TOKENS = frozenset({"", "auto", "tier:auto"})
# Exact ids only — auto path must never forward these retired legacy models to CLI.
_RETIRED_LEGACY_MODELS = frozenset({"gpt-5.4", "gpt-5.4-mini"})
_EFFORT_TO_TIER = {
    "minimal": "fast",
    "low": "fast",
    "medium": "standard",
    "high": "capable",
    "xhigh": "flagship",
}
_TIER_FALLBACK_ORDER = ("flagship", "capable", "standard", "fast", "nano")


@dataclass(frozen=True)
class CodexResult:
    success: bool
    output: str = ""
    error: str = ""


def _codex_env() -> dict[str, str]:
    """Strip secrets from the environment before passing to codex subprocess."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODEX_AUDIT_SERVICE_")
        and not any(marker in key.upper() for marker in SECRET_ENV_MARKERS)
    }


def _is_auto_model(value: str | None) -> bool:
    return str(value or "").strip().lower() in _AUTO_MODEL_TOKENS


def _is_retired_legacy_model(value: str | None) -> bool:
    return str(value or "").strip() in _RETIRED_LEGACY_MODELS


def _catalog_model_usable(model_id: str, catalog) -> bool:
    mid = str(model_id or "").strip()
    if not mid or _is_auto_model(mid) or _is_retired_legacy_model(mid):
        return False
    if mid in set(catalog.deprecated):
        return False
    return mid in catalog.models


def _select_non_retired_catalog_model(catalog, preferred_tier: str) -> str:
    """Pick a concrete non-retired model for auto; never return gpt-5.4 family/auto."""
    preferred = str(preferred_tier or "standard").strip() or "standard"
    assignment = catalog.tiers.get(preferred)
    if assignment is not None and _catalog_model_usable(assignment.model, catalog):
        return assignment.model

    for tier_name in _TIER_FALLBACK_ORDER:
        if tier_name == preferred:
            continue
        candidate = catalog.tiers.get(tier_name)
        if candidate is not None and _catalog_model_usable(candidate.model, catalog):
            return candidate.model

    scored = [
        record
        for model_id, record in catalog.models.items()
        if _catalog_model_usable(model_id, catalog)
    ]
    if not scored:
        raise RuntimeError("model catalog has no usable non-retired Codex model for auto")
    scored.sort(key=lambda record: float(record.capability_score), reverse=True)
    return scored[0].model_id


def _resolve_codex_model(model: str | None, reasoning_effort: str) -> str:
    """Pick a concrete CLI model; never forward auto/tier:auto to codex."""
    request = str(model or "").strip()
    env_model = os.environ.get("CODEX_AUDIT_SERVICE_MODEL", "").strip()
    if not _is_auto_model(request):
        return request
    if not _is_auto_model(env_model):
        return env_model

    from service.model_catalog import catalog_path, load_catalog

    effort = reasoning_effort if reasoning_effort in CODEX_REASONING_EFFORTS else "medium"
    tier = _EFFORT_TO_TIER.get(effort, "standard")
    # Read the live catalog path each call so on-disk updates apply without adapter caching.
    catalog = load_catalog(catalog_path())
    resolved = _select_non_retired_catalog_model(catalog, tier)
    if not resolved or _is_auto_model(resolved) or _is_retired_legacy_model(resolved):
        raise RuntimeError("model catalog did not resolve a concrete non-retired Codex model")
    return resolved


def _codex_command(
    output_last_message: Path,
    *,
    sandbox: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    output_schema: Path | None = None,
    cwd: Path | None = None,
    images: list[Path] | None = None,
    shell_tool_enabled: bool = True,
    tools_disabled: bool = False,
) -> list[str]:
    codex = shutil.which(os.environ.get("CODEX_AUDIT_SERVICE_CODEX_BIN", "codex"))
    if not codex:
        raise RuntimeError("codex CLI was not found on the service host")

    command = [
        codex,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox or os.environ.get("CODEX_AUDIT_SERVICE_SANDBOX", "read-only").strip() or "read-only",
        "--output-last-message",
        str(output_last_message),
    ]
    if not shell_tool_enabled:
        command.extend(["--disable", "shell_tool"])
    if tools_disabled:
        command.extend([
            "--disable", "plugins",
            "--disable", "apps",
            "--disable", "shell_tool",
            "--ignore-user-config", "--ephemeral",
            "-c", 'web_search="disabled"',
        ])
    selected_reasoning_effort = (reasoning_effort or os.environ.get("CODEX_AUDIT_SERVICE_REASONING_EFFORT", "")).strip().lower()
    selected_model = _resolve_codex_model(model, selected_reasoning_effort)
    if selected_model:
        command.extend(["--model", selected_model])
    if selected_reasoning_effort and selected_reasoning_effort != "auto":
        if selected_reasoning_effort not in CODEX_REASONING_EFFORTS:
            raise ValueError(
                f"reasoning_effort must be one of auto,{','.join(sorted(CODEX_REASONING_EFFORTS))}"
            )
        command.extend(["-c", f"model_reasoning_effort={selected_reasoning_effort}"])
    if cwd:
        command.extend(["-C", str(cwd)])
    if output_schema:
        command.extend(["--output-schema", str(output_schema)])
    for image in images or []:
        command.extend(["-i", str(image)])
    command.append("-")
    return command


class CodexAdapter:
    """Adapter for running ``codex exec`` as a subprocess on the VPS.

    Usage::

        adapter = CodexAdapter()
        result = adapter.execute(
            prompt="Review these files and fix any issues.",
            sandbox="read-only",
            timeout=2700,
        )
    """

    def execute(
        self,
        *,
        prompt: str,
        sandbox: str = "read-only",
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout: int = 2700,
        output_schema: Path | None = None,
        cwd: Path | None = None,
        images: list[Path] | None = None,
        shell_tool_enabled: bool = True,
        tools_disabled: bool = False,
    ) -> CodexResult:
        """Run ``codex exec`` synchronously and return the result.

        This is a long-running call — for HTTP services, wrap in a background
        thread (see ``_submit_job`` in ai_gateway_service.py).
        """
        # CODEX_AUDIT_SERVICE_FAKE_OUTPUT is only allowed in non-production environments.
        # In production, it is ignored to prevent bypass of AI execution.
        if os.environ.get("CODEX_AUDIT_SERVICE_ENV", "").strip().lower() not in {"production", "prod"}:
            fake_output = os.environ.get("CODEX_AUDIT_SERVICE_FAKE_OUTPUT")
            if fake_output is not None:
                return CodexResult(success=True, output=fake_output)
        elif os.environ.get("CODEX_AUDIT_SERVICE_FAKE_OUTPUT") is not None:
            import sys
            print("[codex-adapter] WARNING: CODEX_AUDIT_SERVICE_FAKE_OUTPUT ignored in production", file=sys.stderr)

        with tempfile.TemporaryDirectory(prefix="codex-tools-disabled-") as tmp:
            root = Path(tmp)
            output_last_message = root / "codex-final-message.md"
            isolated_cwd = root / "cwd"
            isolated_cwd.mkdir()
            command_cwd = isolated_cwd if tools_disabled else cwd
            command = _codex_command(
                output_last_message,
                sandbox=sandbox,
                model=model,
                reasoning_effort=reasoning_effort,
                output_schema=output_schema,
                cwd=command_cwd,
                images=images,
                shell_tool_enabled=shell_tool_enabled,
                tools_disabled=tools_disabled,
            )
            env = _codex_env()
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=timeout,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                return CodexResult(success=False, error=f"codex exec timed out after {timeout}s")
            except FileNotFoundError:
                return CodexResult(success=False, error="codex command not found")

            if completed.returncode != 0:
                detail = (completed.stdout + completed.stderr).lower()
                category = "quota_or_capacity_failure" if any(
                    word in detail for word in ("quota", "rate limit", "too many active", "budget")
                ) else "unknown_failure"
                return CodexResult(success=False, error=f"codex exec failed [{category}] (rc={completed.returncode})")

            if output_last_message.exists() and output_last_message.read_text(encoding="utf-8").strip():
                return CodexResult(success=True, output=output_last_message.read_text(encoding="utf-8"))
            return CodexResult(success=True, output=completed.stdout)
