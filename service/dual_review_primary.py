"""Codex primary review for dual-review pipeline (task 11b)."""

from __future__ import annotations

import json
import os
import urllib.error
from pathlib import Path
from typing import Any

from service.dual_review import VERDICT_INVALID, VERDICT_UNAVAILABLE
from service.dual_review_secondary import parse_llm_review_output

_PRIMARY_SYSTEM = (
    "You are the primary Codex reviewer for quantitative strategy promotion and risk decisions. "
    "Respond with JSON only: "
    '{"verdict":"approve"|"reject","confidence":0.0-1.0,"summary":"..."}'
)


def build_primary_prompt(
    *,
    trigger: str,
    strategy_profile: str,
    context: dict[str, Any],
    evidence_path: Path | None = None,
) -> str:
    lines = [
        f"Strategy profile: {strategy_profile}",
        f"Trigger: {trigger}",
    ]
    for key in (
        "domain",
        "old_status",
        "new_status",
        "drift_sigma",
        "drift_score",
        "repository",
    ):
        value = context.get(key)
        if value not in (None, ""):
            lines.append(f"{key}: {value}")
    monthly = context.get("monthly_hit_rates")
    if isinstance(monthly, list) and monthly:
        lines.append(f"monthly_hit_rates: {monthly}")
    if evidence_path and evidence_path.is_file():
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if isinstance(evidence, dict):
                summary_bits = {
                    k: evidence.get(k)
                    for k in (
                        "strategy_profile",
                        "status",
                        "oos_sharpe",
                        "max_drawdown",
                        "hit_rate",
                        "evidence_version",
                    )
                    if evidence.get(k) not in (None, "")
                }
                if summary_bits:
                    lines.append(f"evidence_summary: {json.dumps(summary_bits, ensure_ascii=False)}")
        except (OSError, json.JSONDecodeError):
            lines.append(f"evidence_file: {evidence_path}")
    lines.append(
        "Provide an independent primary review for whether this strategy change should proceed."
    )
    return "\n".join(lines)


def parse_primary_review_output(output: str) -> dict[str, Any]:
    review = parse_llm_review_output(output, provider="codex", model="codex-primary")
    review["source"] = "codex_primary"
    return review


def run_codex_primary_review(
    *,
    prompt: str,
    timeout_minutes: int | None = None,
) -> dict[str, Any]:
    """Call VPS Codex audit service for the primary review."""
    service_url = str(os.environ.get("CODEX_AUDIT_SERVICE_URL") or "").strip()
    if not service_url:
        raise RuntimeError("CODEX_AUDIT_SERVICE_URL is not configured")

    timeout = int(timeout_minutes or os.environ.get("DUAL_REVIEW_PRIMARY_TIMEOUT_MINUTES", "15"))
    from scripts.run_codex_pr_review import ReviewError, run_codex_service_review

    try:
        output = run_codex_service_review(
            prompt,
            timeout_minutes=timeout,
            complexity="high",
        )
    except json.JSONDecodeError as exc:
        return {
            "source": "codex_primary",
            "verdict": VERDICT_INVALID,
            "confidence": 0.0,
            "error": str(exc),
        }
    except ReviewError as exc:
        message = str(exc)
        unavailable_markers = (
            "daily budget exceeded",
            "quota",
            "429",
            "service job failed",
            "not configured",
            "timed out",
            "connection",
            "unavailable",
        )
        verdict = VERDICT_UNAVAILABLE if any(marker in message.lower() for marker in unavailable_markers) else VERDICT_INVALID
        return {
            "source": "codex_primary",
            "verdict": verdict,
            "confidence": 0.0,
            "error": message,
        }
    except (urllib.error.URLError, OSError) as exc:
        return {
            "source": "codex_primary",
            "verdict": VERDICT_UNAVAILABLE,
            "confidence": 0.0,
            "error": str(exc),
        }
    return parse_primary_review_output(output)


def primary_review_available() -> bool:
    return bool(str(os.environ.get("CODEX_AUDIT_SERVICE_URL") or "").strip())
