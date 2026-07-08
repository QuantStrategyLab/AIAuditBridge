#!/usr/bin/env python3
"""End-to-end dual-review pipeline: Codex primary → GPT+Claude secondary → dispatch."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from service.dual_review import VERDICT_DISAGREEMENT, VERDICT_FAIL
from service.dual_review_dispatch import dispatch_dual_review_result
from service.dual_review_orchestrator import orchestrate_from_payload
from service.dual_review_primary import (
    build_primary_prompt,
    primary_review_available,
    run_codex_primary_review,
)
from service.dual_review_triggers import resolve_trigger


def _load_json(value: str) -> dict[str, Any]:
    path = Path(value)
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
    else:
        loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("expected JSON object")
    return loaded


def _profile_from_evidence(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            profile = str(data.get("strategy_profile") or data.get("profile") or "").strip()
            if profile:
                return profile
    except (OSError, json.JSONDecodeError):
        pass
    return path.stem


def _build_payload(
    *,
    trigger: str,
    strategy_profile: str,
    context: dict[str, Any],
    primary_review: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trigger": trigger,
        "strategy_profile": strategy_profile,
        "primary_review": primary_review,
    }
    payload.update(context)
    return payload


def _pipeline_enabled() -> bool:
    if str(os.environ.get("DUAL_REVIEW_GATE_SKIP", "")).strip().lower() in {"1", "true", "yes"}:
        return False
    return True


def run_pipeline(
    *,
    trigger: str,
    strategy_profile: str,
    context: dict[str, Any],
    primary_review: dict[str, Any] | None = None,
    evidence_path: Path | None = None,
    dispatch: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not _pipeline_enabled():
        return {"ok": True, "skipped": ["dual_review_gate_disabled"]}

    if primary_review is None:
        if not primary_review_available():
            return {"ok": True, "skipped": ["codex_primary_unconfigured"]}
        prompt = build_primary_prompt(
            trigger=trigger,
            strategy_profile=strategy_profile,
            context=context,
            evidence_path=evidence_path,
        )
        primary_review = run_codex_primary_review(prompt=prompt)

    payload = _build_payload(
        trigger=trigger,
        strategy_profile=strategy_profile,
        context=context,
        primary_review=primary_review,
    )
    if resolve_trigger(payload) is None:
        return {"ok": False, "error": "invalid_trigger", "payload": payload}

    outcome = orchestrate_from_payload(payload)
    if outcome is None:
        return {"ok": False, "error": "orchestration_failed", "payload": payload}

    result = outcome.to_dict()
    if dispatch:
        result["dispatch"] = dispatch_dual_review_result(outcome, dry_run=dry_run)
    result["ok"] = True
    return result


def _exit_code(result: dict[str, Any]) -> int:
    if not result.get("ok"):
        return 1
    if result.get("skipped"):
        return 0
    outcome = str(result.get("outcome") or "")
    if outcome in {VERDICT_DISAGREEMENT, VERDICT_FAIL}:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Codex primary + dual API secondary review pipeline.")
    parser.add_argument("--trigger", required=True, choices=("promotion", "hit_rate", "drift"))
    parser.add_argument("--strategy-profile", required=True)
    parser.add_argument("--context-json", default="{}", help="Inline JSON or file path for trigger context")
    parser.add_argument("--evidence-file", help="Evidence package path (promotion)")
    parser.add_argument("--primary-review", help="Precomputed primary review JSON (skip Codex)")
    parser.add_argument("--dispatch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--from-evidence",
        help="Shorthand: promotion review for evidence package (sets trigger=promotion)",
    )
    args = parser.parse_args(argv)

    context = _load_json(args.context_json)
    evidence_path = Path(args.evidence_file) if args.evidence_file else None
    trigger = args.trigger
    profile = args.strategy_profile

    if args.from_evidence:
        evidence_path = Path(args.from_evidence)
        trigger = "promotion"
        profile = _profile_from_evidence(evidence_path)
        context.setdefault("repository", os.environ.get("GITHUB_REPOSITORY", ""))
        context.setdefault("old_status", "shadow_candidate")
        context.setdefault("new_status", "live_candidate")

    primary = _load_json(args.primary_review) if args.primary_review else None
    result = run_pipeline(
        trigger=trigger,
        strategy_profile=profile,
        context=context,
        primary_review=primary,
        evidence_path=evidence_path,
        dispatch=args.dispatch,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
