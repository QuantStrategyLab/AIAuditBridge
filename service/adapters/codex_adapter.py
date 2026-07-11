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
_LOCAL_CODEX_FAILURE_MARKERS = (
    "authentication",
    "bootstrap",
    "command not found",
    "configuration",
    "invalid option",
    "invalid sandbox",
    "no such file",
    "not logged in",
    "permission denied",
    "unrecognized option",
    "unknown option",
    "unsupported",
)


@dataclass(frozen=True)
class CodexResult:
    success: bool
    output: str = ""
    error: str = ""
    dispatch_started: bool = False
    dispatch_uncertain: bool = False


def _codex_dispatch_is_uncertain(detail: str) -> bool:
    """Classify known local CLI failures as definitely not dispatched."""
    text = detail.lower()
    return not any(marker in text for marker in _LOCAL_CODEX_FAILURE_MARKERS)


def _codex_env() -> dict[str, str]:
    """Strip secrets from the environment before passing to codex subprocess."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODEX_AUDIT_SERVICE_")
        and not any(marker in key.upper() for marker in SECRET_ENV_MARKERS)
    }


def _codex_command(
    output_last_message: Path,
    *,
    sandbox: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    output_schema: Path | None = None,
    cwd: Path | None = None,
    images: list[Path] | None = None,
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
    selected_model = model or os.environ.get("CODEX_AUDIT_SERVICE_MODEL", "").strip()
    if selected_model:
        command.extend(["--model", selected_model])
    selected_reasoning_effort = (reasoning_effort or os.environ.get("CODEX_AUDIT_SERVICE_REASONING_EFFORT", "")).strip().lower()
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

        with tempfile.TemporaryDirectory() as tmp:
            output_last_message = Path(tmp) / "codex-final-message.md"
            try:
                command = _codex_command(
                    output_last_message,
                    sandbox=sandbox,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    output_schema=output_schema,
                    cwd=cwd,
                    images=images,
                )
            except (RuntimeError, ValueError) as exc:
                return CodexResult(success=False, error=f"codex command configuration failed: {exc}")
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=timeout,
                    env=_codex_env(),
                )
            except subprocess.TimeoutExpired as exc:
                return CodexResult(
                    success=False,
                    error=f"codex exec timed out after {timeout}s: {exc}",
                    dispatch_uncertain=True,
                )
            except FileNotFoundError as exc:
                return CodexResult(success=False, error=f"codex command not found: {exc}")

            if completed.returncode != 0:
                detail = (completed.stdout[-4000:] + completed.stderr[-4000:]).strip()
                return CodexResult(
                    success=False,
                    error=f"codex exec failed (rc={completed.returncode})" + (f":\n{detail}" if detail else ""),
                    dispatch_uncertain=_codex_dispatch_is_uncertain(detail),
                )

            if output_last_message.exists() and output_last_message.read_text(encoding="utf-8").strip():
                return CodexResult(success=True, output=output_last_message.read_text(encoding="utf-8"), dispatch_started=True)
            return CodexResult(success=True, output=completed.stdout, dispatch_started=True)
