#!/usr/bin/env python3
"""Replay the reviewed UESP #489 watchdog repair in a temporary directory."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.config import GatewayConfig  # noqa: E402
from client.gateway_client import AiGatewayClient  # noqa: E402


EVIDENCE_KIND = "historical_repair_rehearsal"
SOURCE_REPOSITORY = "QuantStrategyLab/AIAuditBridge"
PRE_FIX_COMMIT = "b03ecbe4e0a7a0de22f298499f867a7039e4b60a"
APPROVED_COMMIT = "8f2b0c455833009806ccb63c341f8d0fb36bcfaf"
PRE_FIX_SHA256 = "5937c993710266420a21e881d906004b29213c72fcbf851f32f87fb76b40399c"
APPROVED_SHA256 = "5c9b23475e578593c356e498c90674d347f5784596a19f061b050af01242bb23"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "watchdog_repair"
PRE_FIX_FIXTURE = FIXTURE_DIR / "pre_fix_b03ecbe4.py"
APPROVED_FIXTURE = FIXTURE_DIR / "approved_fix_8f2b0c45.py"
TARGET_RELATIVE_PATH = Path(
    "src/us_equity_snapshot_pipelines/lifecycle/daily_research_schedule_watchdog.py"
)
_ALLOWED_EFFORTS = {"low", "medium", "high", "xhigh"}
_NON_OIDC_CREDENTIAL_ENV = (
    "CODEX_AUDIT_SERVICE_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CURSOR_API_KEY",
)


def _base_result() -> dict[str, object]:
    return {
        "evidence_kind": EVIDENCE_KIND,
        "input_kind": "synthetic_replay_of_historical_source",
        "ai_request_attempted": False,
        "production_changed": False,
        "publish_scope": "artifact_only",
        "source_identity": {
            "pre_fix_commit": PRE_FIX_COMMIT,
            "pre_fix_sha256": PRE_FIX_SHA256,
            "approved_commit": APPROVED_COMMIT,
            "approved_sha256": APPROVED_SHA256,
        },
    }


def _park(reason: str, **fields: object) -> dict[str, object]:
    return {**_base_result(), "status": "PARKED", "reason_code": reason, **fields}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trusted_fixtures_valid() -> bool:
    try:
        return (
            _sha256(PRE_FIX_FIXTURE) == PRE_FIX_SHA256
            and _sha256(APPROVED_FIXTURE) == APPROVED_SHA256
        )
    except OSError:
        return False


def _load_source(path: Path, label: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"watchdog_rehearsal_{label}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("trusted_source_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(conclusion: str = "success", *, created_at: str = "2026-08-21T11:20:00Z") -> dict[str, object]:
    return {
        "workflow_runs": [
            {
                "id": 489,
                "event": "schedule",
                "created_at": created_at,
                "status": "completed",
                "conclusion": conclusion,
            }
        ]
    }


def _summary(
    module: ModuleType,
    *,
    research_conclusion: str = "success",
    watchdog_conclusion: str | None = "failure",
) -> dict[str, object]:
    watchdog_response = (
        {"workflow_runs": []}
        if watchdog_conclusion is None
        else _response(watchdog_conclusion, created_at="2026-08-20T11:20:00Z")
    )
    additional = {
        "daily-research-schedule-watchdog": ("2026-08-20", watchdog_response)
    }
    return module.build_daily_research_schedule_watchdog_summary(
        expected_utc_date="2026-08-21",
        tqqq_workflow_runs_response=_response(),
        soxl_workflow_runs_response=_response(research_conclusion),
        additional_workflow_checks=additional,
    )


def _validate_repair(target: Path) -> dict[str, object]:
    before_module = _load_source(target, "before")
    before = _summary(before_module)
    before_workflows = before.get("workflows")
    before_watchdog = (
        before_workflows[-1]
        if isinstance(before_workflows, list) and before_workflows
        else {}
    )
    before_run = before_watchdog.get("run") if isinstance(before_watchdog, Mapping) else None
    if (
        before.get("status") != "PARKED"
        or not isinstance(before_run, Mapping)
        or before_run.get("conclusion") != "failure"
    ):
        return _park("PRE_FIX_REPLAY_MISMATCH")

    shutil.copyfile(APPROVED_FIXTURE, target)
    if _sha256(target) != APPROVED_SHA256:
        return _park("APPLIED_SOURCE_HASH_MISMATCH")
    after_module = _load_source(target, "after")
    after = _summary(after_module)
    workflows = after.get("workflows")
    watchdog = workflows[-1] if isinstance(workflows, list) and workflows else {}
    run = watchdog.get("run") if isinstance(watchdog, Mapping) else None
    if (
        after.get("status") != "OBSERVED"
        or not isinstance(run, Mapping)
        or run.get("conclusion") != "failure"
    ):
        return _park("APPROVED_FIX_REPLAY_MISMATCH")

    checks = {
        "research_failure": _summary(after_module, research_conclusion="failure")["status"],
        "watchdog_cancelled": _summary(after_module, watchdog_conclusion="cancelled")["status"],
        "watchdog_timed_out": _summary(after_module, watchdog_conclusion="timed_out")["status"],
        "watchdog_missing": _summary(after_module, watchdog_conclusion=None)["status"],
        "watchdog_success": _summary(after_module, watchdog_conclusion="success")["status"],
    }
    expected = {
        "research_failure": "PARKED",
        "watchdog_cancelled": "PARKED",
        "watchdog_timed_out": "PARKED",
        "watchdog_missing": "PARKED",
        "watchdog_success": "OBSERVED",
    }
    if checks != expected:
        return _park("APPROVED_FIX_BOUNDARY_MISMATCH")
    return {
        **_base_result(),
        "status": "OBSERVED",
        "action": "apply_watchdog_489",
        "before": before,
        "after": after,
        "checks": checks,
    }


def offline_check(*, workspace: Path | None = None) -> dict[str, object]:
    """Apply only the hash-pinned reviewed source and replay fixed scenarios."""
    if not _trusted_fixtures_valid():
        return _park("TRUSTED_SOURCE_HASH_MISMATCH")

    def run(root: Path) -> dict[str, object]:
        target = root / TARGET_RELATIVE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PRE_FIX_FIXTURE, target)
        if _sha256(target) != PRE_FIX_SHA256:
            return _park("COPIED_SOURCE_HASH_MISMATCH")
        return _validate_repair(target)

    if workspace is not None:
        return run(workspace)
    with tempfile.TemporaryDirectory(prefix="watchdog-489-rehearsal-") as temporary:
        return run(Path(temporary))


def _prompt() -> str:
    evidence = {
        "case": "UESP_489",
        "input_kind": "fixed_historical_source_audit",
        "symptom": "a prior completed watchdog failure recursively caused another alert",
        "old_logic_summary": "every completed non-success schedule run was parked",
        "approved_action_semantics": (
            "only a previous watchdog completed with failure becomes an observed heartbeat; "
            "its failure conclusion remains visible; research failure and a missing, cancelled, "
            "or timed-out watchdog remain parked"
        ),
        "approved_action": "apply_watchdog_489",
        "alternative_action": "escalate",
    }
    return (
        "This is a historical repair rehearsal. No natural production failure input was observed. "
        "Review only the fixed symptom and old-logic summary below. Return exactly one JSON object, "
        "either {\"action\":\"apply_watchdog_489\"} to select the pre-reviewed repair or "
        "{\"action\":\"escalate\"}. Do not return code, patches, file paths, or extra keys. "
        "The machine will ignore all content except the single allowed action and can only copy a "
        "hash-pinned reviewed source into a temporary directory.\n"
        + json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    )


def _route(result: object) -> dict[str, str] | None:
    raw = getattr(result, "raw", None)
    if not isinstance(raw, Mapping):
        return None
    route = {
        "job_id": raw.get("job_id"),
        "provider": raw.get("provider"),
        "research_stage": raw.get("research_stage"),
        "model": raw.get("model"),
        "reasoning_effort": raw.get("reasoning_effort"),
    }
    if (
        getattr(result, "success", None) is not True
        or raw.get("status") != "succeeded"
        or not isinstance(route["job_id"], str)
        or not route["job_id"]
        or route["provider"] != "codex"
        or getattr(result, "provider", None) != route["provider"]
        or route["research_stage"] != "drift_analysis"
        or not isinstance(route["model"], str)
        or not route["model"]
        or getattr(result, "model", None) != route["model"]
        or route["reasoning_effort"] not in _ALLOWED_EFFORTS
        or raw.get("output") != getattr(result, "output", None)
    ):
        return None
    return {key: str(value) for key, value in route.items()}


def run_rehearsal(
    *,
    environ: Mapping[str, str] = os.environ,
    workspace: Path | None = None,
    config_loader: Callable[[], GatewayConfig] = GatewayConfig.from_env,
    client_factory: Callable[[GatewayConfig], AiGatewayClient] = AiGatewayClient,
) -> dict[str, object]:
    """Ask Codex for one bounded action, then replay the reviewed source locally."""
    if (
        environ.get("GITHUB_REPOSITORY") != SOURCE_REPOSITORY
        or environ.get("GITHUB_REF") != "refs/heads/main"
        or environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
    ):
        return _park("INVALID_WORKFLOW_CONTEXT")
    if environ.get("GITHUB_RUN_ATTEMPT") != "1":
        return _park("INVALID_WORKFLOW_ATTEMPT")
    if not (
        environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
        and environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    ):
        return _park("GITHUB_OIDC_REQUIRED")
    if any(environ.get(name) for name in _NON_OIDC_CREDENTIAL_ENV):
        return _park("NON_OIDC_CREDENTIALS_REJECTED")
    if not _trusted_fixtures_valid():
        return _park("TRUSTED_SOURCE_HASH_MISMATCH")

    try:
        client = client_factory(config_loader())
    except Exception:
        return _park("AI_GATEWAY_NOT_CONFIGURED")
    try:
        result = client.execute(
            _prompt(),
            task="historical_watchdog_repair_rehearsal",
            mode="review_only",
            sandbox="read-only",
            research_stage="drift_analysis",
            allowed_providers=["codex"],
            source_repository=SOURCE_REPOSITORY,
            source_ref="main",
            timeout=600,
        )
    except Exception:
        return _park("AI_OUTCOME_UNKNOWN", ai_request_attempted=True)

    raw = getattr(result, "raw", None)
    if isinstance(raw, Mapping) and raw.get("status") == "deferred":
        return _park("AI_DEFERRED", ai_request_attempted=True)
    if getattr(result, "success", None) is not True:
        return _park("AI_RESULT_UNAVAILABLE", ai_request_attempted=True)
    route = _route(result)
    if route is None:
        return _park("AI_ROUTE_INVALID", ai_request_attempted=True)
    try:
        decision = json.loads(str(getattr(result, "output", "")))
    except (TypeError, ValueError):
        return _park("AI_ACTION_INVALID", ai_request_attempted=True, ai_route=route)
    if not isinstance(decision, dict) or set(decision) != {"action"}:
        return _park("AI_ACTION_INVALID", ai_request_attempted=True, ai_route=route)
    if decision["action"] == "escalate":
        return _park("AI_ESCALATED", ai_request_attempted=True, ai_route=route)
    if decision["action"] != "apply_watchdog_489":
        return _park("AI_ACTION_INVALID", ai_request_attempted=True, ai_route=route)

    replay = offline_check(workspace=workspace)
    return {**replay, "ai_request_attempted": True, "ai_route": route}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline-check",
        action="store_true",
        help="verify the approved action locally without invoking AI",
    )
    args = parser.parse_args(argv)
    result = offline_check() if args.offline_check else run_rehearsal()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") == "OBSERVED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
