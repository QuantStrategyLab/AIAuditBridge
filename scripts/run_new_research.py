#!/usr/bin/env python3
"""Run one source-bound, non-live research request through the shared QPK cycle."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
import re
import subprocess
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from quant_platform_kit.research_factory import (
    RESEARCH_SOURCE_RECEIPT_SCHEMA_VERSION,
    ResearchSourceReceipt,
    ResearchWorkerManifest,
    ResearchWorkerRole,
    validate_source_receipt,
    validate_worker_manifest,
)
from quant_platform_kit.strategy_lifecycle.candidate_control import ResearchSourceReceiptRef
from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import (
    NewResearchRequest,
    ResearchPromotionBudget,
    run_saved_research_promotion_cycle,
)


_REVISION = re.compile(r"^[0-9a-f]{40}$")
_WORKER_CAPABILITIES = frozenset({"quarantine_read", "sandbox_write"})
_LOCAL_STRATEGY_FACTS = {
    "strategy_description": "SOXL/SOXX RSI2 mean reversion with SMA200 baseline and SOXL next-open execution.",
    "plugins": ["SOXL RSI2", "SMA200", "SOXL next-open"],
    "limitations": ["plugins disabled", "research_only", "strict promotion evidence required"],
}
_REQUIRED = frozenset({
    "request", "worker_manifest", "source_receipts", "research_identity",
    "source_commit", "source_blobs", "input_paths", "output_root", "ticket_dir",
})
_OPTIONAL = frozenset({"ues_repo_root", "caller_ref", "strategy_facts", "fixed_input_identity"})
SOXL_P1_MANIFEST_SHA256 = "b39008e05eeebd4a126eefe289a18bcae05869d937dc76e6040b71d3b0daf36d"
SOXL_P1_MANIFEST_URL = "https://storage.googleapis.com/qsl-runtime-logs-shared/soxl-p1-p3/b39008e05eeebd4a126eefe289a18bcae05869d937dc76e6040b71d3b0daf36d/manifest.json"
SOXL_UES_COMMIT = "d6b37b77c309e1fb7f25263271b6b0f653f7e7b8"
SOXL_QPK_COMMIT = "de13e486da1bdba60f425e576e944591fc97b809"
_SOXL_PROVENANCE_PATHS = (
    "src/us_equity_strategies/research/soxl_core_optimization.py",
    "src/us_equity_strategies/research/soxl_soxx_offline_input_contract.py",
    "src/us_equity_strategies/research/soxl_soxx_typed_baseline_result.py",
)
_FIXED_REQUEST_FILENAME = "request.json"
_FIXED_REQUEST_IDENTITY_FIELDS = (
    "request", "worker_manifest", "source_receipts", "research_identity",
    "source_commit", "source_blobs", "strategy_facts", "caller_ref", "fixed_input_identity",
)


class NewResearchInputError(ValueError):
    """Sanitized request validation failure."""


def build_soxl_p1_source_receipt(*, retrieved_at: datetime) -> dict[str, Any]:
    """Build the citation-only receipt for the already verified fixed P1 run."""
    value = ResearchSourceReceipt(
        schema_version="research_source_receipt.v1",
        source_id="soxl-p1-b39008e05eee",
        source_url=SOXL_P1_MANIFEST_URL,
        publisher="QuantStrategyLab/UsEquitySnapshotPipelines",
        retrieved_at=retrieved_at,
        content_sha256=SOXL_P1_MANIFEST_SHA256,
        declared_license=None,
        usage_scope="citation_or_summary",
        license_review_id=None,
    )
    return value.to_dict()


SOXL_RSI2_FOLDS = (
    (date(2023, 9, 12), date(2024, 12, 31), date(2025, 1, 27), date(2025, 2, 7)),
    (date(2025, 3, 3), date(2025, 3, 14), date(2025, 4, 7), date(2025, 4, 18)),
    (date(2025, 5, 12), date(2025, 5, 23), date(2025, 6, 16), date(2025, 6, 27)),
)


def _read_aab_caller_revision() -> str:
    try:
        revision = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise NewResearchInputError("aab_caller_revision_unavailable") from None
    expected = os.environ.get("GITHUB_SHA", "").strip()
    if expected and revision != expected:
        raise NewResearchInputError("aab_caller_revision_mismatch")
    if _REVISION.fullmatch(revision) is None:
        raise NewResearchInputError("aab_caller_revision_invalid")
    return revision


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _load_fixed_request(path: Path) -> dict[str, Any]:
    raw = _read_json(path)
    if (not isinstance(raw, Mapping) or not _REQUIRED <= set(raw)
            or set(raw) - _REQUIRED - _OPTIONAL):
        raise NewResearchInputError("fixed_research_request_invalid")
    try:
        _worker(raw["worker_manifest"])
        receipts = raw["source_receipts"]
        if not isinstance(receipts, list) or len(receipts) != 1:
            raise NewResearchInputError("fixed_research_request_invalid")
        parsed_receipts = [_receipt(receipts[0])]
        expected_receipt = build_soxl_p1_source_receipt(
            retrieved_at=parsed_receipts[0].retrieved_at,
        )
        if parsed_receipts[0].to_dict() != expected_receipt:
            raise NewResearchInputError("fixed_research_input_mismatch")
        _request(raw["request"], parsed_receipts)
        _identity(raw["research_identity"])
        _digest_map(raw["source_blobs"])
        fixed_input_identity = raw.get("fixed_input_identity")
        if (not isinstance(fixed_input_identity, Mapping)
                or set(fixed_input_identity) != {
                    "p1_manifest_sha256", "optimization_input_digest", "promotion_input_digest",
                }
                or any(not isinstance(value, str) or not value.strip()
                       for value in fixed_input_identity.values())):
            raise NewResearchInputError("fixed_research_request_invalid")
    except NewResearchInputError:
        raise
    except Exception:
        raise NewResearchInputError("fixed_research_request_invalid") from None
    return dict(raw)


def _save_fixed_request(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, TypeError, ValueError):
        raise NewResearchInputError("fixed_research_request_unavailable") from None


def _fixed_request_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in _FIXED_REQUEST_IDENTITY_FIELDS}


def _assert_fixed_request_unchanged(
    saved_payload: Mapping[str, Any], current_payload: Mapping[str, Any],
) -> None:
    if _canonical_json(_fixed_request_identity(saved_payload)) != _canonical_json(
        _fixed_request_identity(current_payload)
    ):
        raise NewResearchInputError("fixed_research_input_mismatch")


def _matching_fixed_ticket(
    ticket_dir: Path, *, request: NewResearchRequest,
    research_identity: Mapping[str, str],
) -> Any | None:
    """Read only this request's existing QPK checkpoints.

    The pinned QPK loader validates the ticket shape and authority flag. A
    saved ticket with a different fixed identity is a mismatch, never a reason
    to start another optimizer run in the same run root.
    """
    if not ticket_dir.exists():
        return None
    try:
        from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import (
            load_research_promotion_ticket,
        )
    except ImportError as exc:
        raise NewResearchInputError("fixed_research_checkpoint_unavailable") from exc
    match = None
    expected_request = request.to_dict()
    fixed_revision_fields = (
        "code_revision", "param_space_revision", "cost_model_revision", "validator_revision",
    )
    try:
        paths = sorted(ticket_dir.glob("*.json"))
    except OSError:
        raise NewResearchInputError("fixed_research_checkpoint_unavailable") from None
    for path in paths:
        try:
            ticket = load_research_promotion_ticket(path)
        except Exception as exc:
            raise NewResearchInputError("fixed_research_checkpoint_invalid") from exc
        progress = getattr(ticket, "research_progress", {})
        saved_identity = progress.get("identity") if isinstance(progress, Mapping) else None
        if not isinstance(saved_identity, Mapping):
            continue
        saved_request = saved_identity.get("request")
        if saved_request != expected_request:
            raise NewResearchInputError("fixed_research_checkpoint_mismatch")
        revisions = saved_identity.get("revisions")
        if not isinstance(revisions, Mapping):
            raise NewResearchInputError("fixed_research_checkpoint_invalid")
        if any(revisions.get(field) != research_identity.get(field) for field in fixed_revision_fields):
            raise NewResearchInputError("fixed_research_checkpoint_mismatch")
        if match is not None:
            raise NewResearchInputError("fixed_research_checkpoint_duplicate")
        match = ticket
    return match


def _proposal_from_fixed_ticket(ticket: Any, request: NewResearchRequest) -> Any | None:
    if ticket is None:
        return None
    progress = getattr(ticket, "research_progress", {})
    stages = progress.get("stages", {}) if isinstance(progress, Mapping) else {}
    optimize = stages.get("optimize", {}) if isinstance(stages, Mapping) else {}
    if optimize.get("status") != "completed" or not isinstance(optimize.get("result"), Mapping):
        return None
    try:
        from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import _saved_proposal
        proposal = _saved_proposal(optimize["result"])
    except Exception:
        raise NewResearchInputError("fixed_research_checkpoint_invalid") from None
    if (proposal.strategy_profile != request.strategy_profile
            or proposal.domain != request.domain):
        raise NewResearchInputError("fixed_research_checkpoint_mismatch")
    return proposal


def _fixed_ticket_summary(ticket: Any) -> dict[str, Any]:
    if ticket is None:
        raise NewResearchInputError("fixed_research_checkpoint_invalid")
    progress = getattr(ticket, "research_progress", {})
    stages = progress.get("stages", {}) if isinstance(progress, Mapping) else {}
    unknown = any(isinstance(value, Mapping) and value.get("status") in {"running", "unknown"}
                  for value in stages.values())
    state = getattr(getattr(ticket, "state", None), "value", "parked")
    raw = ticket.to_dict(include_progress=True)
    terminal = state in {"parked", "human_accepted", "human_rejected"}
    reason = (
        "research_outcome_unknown" if unknown
        else "saved_research_ticket_terminal" if terminal
        else "research_checkpoint_invalid"
    )
    return {
        "status": "parked",
        "reason": reason,
        "resumed": True,
        "live_authority_granted": False,
        "optimizer_executions": 0,
        "optimizer_candidate_id": None,
        "ticket": raw,
        "state": state,
        "drift_status": raw.get("drift_status"),
        "drift_score": raw.get("drift_score"),
        "notes": raw.get("notes", []),
    }


def _fixed_ticket_requires_no_optimizer(ticket: Any) -> bool:
    if ticket is None:
        return False
    progress = getattr(ticket, "research_progress", {})
    stages = progress.get("stages", {}) if isinstance(progress, Mapping) else {}
    if not isinstance(stages, Mapping):
        raise NewResearchInputError("fixed_research_checkpoint_invalid")
    if any(isinstance(value, Mapping) and value.get("status") in {"running", "unknown"}
           for value in stages.values()):
        return True
    state = getattr(getattr(ticket, "state", None), "value", "")
    return bool(stages) or state in {
        "parked", "shadow_recorded", "awaiting_human", "human_accepted", "human_rejected",
    }


def run_fixed_soxl_rsi2_case(
    *, p1_root: str | Path, ues_repo_root: str | Path, run_root: str | Path,
    as_of: date | None = None, diagnose=None, summarize=None,
    sync_console=None, pull_console=None,
) -> dict[str, Any]:
    """Run the one approved real-input case, once, with fixed folds and local-only evidence."""
    from quant_platform_kit.strategy_lifecycle.contracts import PromotionCostModel, PurgedWalkForwardFold
    from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore
    from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import ResearchPromotionBudget
    from us_equity_strategies.research.soxl_alpaca_input_adapter import materialize_soxl_alpaca_input
    from us_equity_strategies.research.soxl_rsi2_research_adapter import (
        Rsi2OfflineInputPaths, _COST_MODEL_REVISION, _PARAM_SPACE_REVISION,
        _VALIDATOR_REVISION, SoxlRsi2PromotionBinding, load_rsi2_offline_input,
        make_soxl_rsi2_optimize,
    )

    root = Path(run_root)
    optimization_root = root / "input-optimization"
    promotion_root = root / "input-promotion"
    result_root = root / "optimization-result"
    ticket_root = root / "tickets"
    try:
        root.mkdir(parents=True, exist_ok=True)
        source_commit, source_blobs = read_soxl_ues_provenance(ues_repo_root)
        caller_revision = _read_aab_caller_revision()
        manifest_bytes = (Path(p1_root) / "manifest.json").read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != SOXL_P1_MANIFEST_SHA256:
            raise NewResearchInputError("p1_manifest_invalid")
        optimization_paths = materialize_soxl_alpaca_input(
            p1_root, optimization_root, start="2022-01-03", end_exclusive="2025-01-01", expected_sessions=753,
        )
        promotion_paths = materialize_soxl_alpaca_input(
            p1_root, promotion_root, start="2023-09-12", end_exclusive="2026-09-12", expected_sessions=753,
        )
        optimization = load_rsi2_offline_input(Rsi2OfflineInputPaths(
            optimization_paths.manifest, optimization_paths.artifact, optimization_paths.readback,
        ))
        promotion = load_rsi2_offline_input(Rsi2OfflineInputPaths(
            promotion_paths.manifest, promotion_paths.artifact, promotion_paths.readback,
        ))
        worker = ResearchWorkerManifest.expected(
            worker_id="soxl-rsi2-controlled-case", role=ResearchWorkerRole.PLANNER_BUILDER,
        )
        identity = {
            "code_revision": source_commit, "input_revision": optimization.input_digest,
            "param_space_revision": _PARAM_SPACE_REVISION,
            "cost_model_revision": _COST_MODEL_REVISION,
            "validator_revision": _VALIDATOR_REVISION,
        }
        request_path = root / _FIXED_REQUEST_FILENAME
        saved_payload = None
        if request_path.exists():
            saved_payload = _load_fixed_request(request_path)
            saved_request = _request(
                saved_payload["request"],
                [_receipt(item) for item in saved_payload["source_receipts"]],
            )
            if as_of is not None and as_of != saved_request.as_of:
                raise NewResearchInputError("fixed_research_as_of_mismatch")
            _receipt(saved_payload["source_receipts"][0])
            receipt = dict(saved_payload["source_receipts"][0])
            request_as_of = saved_request.as_of
        else:
            if as_of is not None and type(as_of) is not date:
                raise NewResearchInputError("fixed_research_as_of_invalid")
            receipt = build_soxl_p1_source_receipt(retrieved_at=datetime.now(timezone.utc))
            request_as_of = as_of or datetime.now(timezone.utc).date()
        current_payload = {
            "request": {
                "strategy_profile": "soxl_rsi2_mean_reversion", "domain": "us_equity",
                "as_of": request_as_of.isoformat(),
                "source_revision": optimization.source_revision,
                "research_intent": "fixed_soxl_rsi2_dual_window",
            },
            "worker_manifest": {
                "schema_version": worker.schema_version, "worker_id": worker.worker_id,
                "role": worker.role.value, "capabilities": sorted(worker.capabilities),
                "secret_access": False, "broker_access": False,
                "cloud_runtime_access": False, "deployment_write_access": False,
            },
            "source_receipts": [receipt], "research_identity": identity,
            "source_commit": source_commit, "source_blobs": source_blobs,
            "fixed_input_identity": {
                "p1_manifest_sha256": SOXL_P1_MANIFEST_SHA256,
                "optimization_input_digest": optimization.input_digest,
                "promotion_input_digest": promotion.input_digest,
            },
            "ues_repo_root": str(ues_repo_root), "strategy_facts": _LOCAL_STRATEGY_FACTS,
            "caller_ref": caller_revision,
            "input_paths": {"manifest": str(optimization_paths.manifest),
                            "artifact": str(optimization_paths.artifact),
                            "readback": str(optimization_paths.readback)},
            "output_root": str(result_root), "ticket_dir": str(ticket_root),
        }
        if saved_payload is None:
            _save_fixed_request(request_path, current_payload)
            payload = current_payload
        else:
            _assert_fixed_request_unchanged(saved_payload, current_payload)
            payload = dict(saved_payload)
            payload["input_paths"] = current_payload["input_paths"]
            payload["output_root"] = current_payload["output_root"]
            payload["ticket_dir"] = current_payload["ticket_dir"]
            payload["ues_repo_root"] = current_payload["ues_repo_root"]
        request = _request(payload["request"], [_receipt(receipt)])
        folds = tuple(PurgedWalkForwardFold(*values) for values in SOXL_RSI2_FOLDS)
        cost_model = PromotionCostModel("C2_5", 2.0, 5.0)
        from quant_platform_kit.strategy_lifecycle.backtest_orchestrator import _validate_promotion_plan
        _validate_promotion_plan(
            folds, locked_oos_start=date(2025, 8, 1), locked_oos_end=date(2026, 9, 11),
            purge_days=20, embargo_days=20, source_revision=source_commit, cost_model=cost_model,
        )
        ticket = _matching_fixed_ticket(
            ticket_root, request=request, research_identity=identity,
        )
        proposal = _proposal_from_fixed_ticket(ticket, request)
        optimizer_executions = 0
        if proposal is None and _fixed_ticket_requires_no_optimizer(ticket):
            return _fixed_ticket_summary(ticket)
        if proposal is None:
            optimizer = make_soxl_rsi2_optimize(
                input_paths=Rsi2OfflineInputPaths(
                    optimization_paths.manifest, optimization_paths.artifact, optimization_paths.readback,
                ), output_root=result_root, source_commit=source_commit,
                source_blobs=source_blobs, ues_repo_root=ues_repo_root,
                source=optimization, expected_identity=identity,
            )
            proposal = optimizer(request, ResearchPromotionBudget(max_search_iterations=4, max_param_keys=1))
            optimizer_executions = 1
        candidate_id = dict(proposal.proposed_params or {}).get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise NewResearchInputError("rsi2_candidate_invalid")
        binding = SoxlRsi2PromotionBinding(
            source=promotion, candidate_id=candidate_id, folds=folds,
            locked_oos_start=date(2025, 8, 1), locked_oos_end=date(2026, 9, 11),
            purge_days=20, embargo_days=20, source_revision=source_commit,
            cost_model=cost_model,
        )
        store = PerformanceStore(cloud_bucket="", local_root=root / "performance")
        shadow_check_attempted = {"value": False}
        shadow_started = {"value": False}
        formal_backtest_started = {"value": False}
        from quant_platform_kit.strategy_lifecycle.paired_shadow_adapter import resolve_promotion_shadow_record
        def shadow(proposal_value):
            shadow_check_attempted["value"] = True
            return resolve_promotion_shadow_record(
                proposal=proposal_value, collector=None, allow_proxy_fallback=False,
            )
        result = run_request(
            payload, diagnose=diagnose, summarize=summarize,
            promotion_binding=binding, promotion_store=store,
            promotion_shadow_recorder=shadow,
            sync_console=sync_console, pull_console=pull_console,
            frozen_proposal=proposal,
            research_budget=ResearchPromotionBudget(
                max_search_iterations=4, max_param_keys=1, require_paired_shadow=True,
            ),
            on_formal_backtest=lambda: formal_backtest_started.__setitem__("value", True),
        )
        result.update({
            "optimizer_executions": optimizer_executions,
            "optimizer_candidate_id": candidate_id,
            "optimizer_recommendation": proposal.recommendation,
            "optimization_input_digest": optimization.input_digest,
            "promotion_input_digest": promotion.input_digest,
            "optimization_window": {"start": "2022-01-03", "end_exclusive": "2025-01-01", "sessions": 753},
            "promotion_window": {"start": "2023-09-12", "end_exclusive": "2026-09-12", "sessions": 753},
            "formal_backtest_attempted": formal_backtest_started["value"],
            "shadow_started": shadow_started["value"],
            "shadow_check_attempted": shadow_check_attempted["value"],
            "shadow_status": (
                "awaiting_forward_observation" if shadow_check_attempted["value"] else "not_started"
            ),
        })
        return result
    finally:
        for path in (optimization_root, promotion_root):
            shutil.rmtree(path, ignore_errors=True)


def read_soxl_ues_provenance(ues_repo_root: str | Path) -> tuple[str, dict[str, str]]:
    """Read exact pinned UES commit and blob IDs from a checked-out repository."""
    root = Path(ues_repo_root)
    try:
        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(root), *args], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
        commit = git("rev-parse", "HEAD")
        blobs = {path: git("rev-parse", f"HEAD:{path}") for path in _SOXL_PROVENANCE_PATHS}
    except (OSError, subprocess.CalledProcessError):
        raise NewResearchInputError("ues_provenance_unavailable") from None
    if commit != SOXL_UES_COMMIT or any(_REVISION.fullmatch(value) is None for value in blobs.values()):
        raise NewResearchInputError("ues_provenance_mismatch")
    return commit, blobs


def run_trusted_soxl_rsi2_dual_window(
    payload: Mapping[str, Any], *, p1_root: str | Path,
    optimization_root: str | Path, promotion_root: str | Path,
    expected_p1_manifest_sha256: str,
    candidate_id: str, folds: tuple[Any, ...], locked_oos_start: date,
    locked_oos_end: date, purge_days: int, embargo_days: int,
    source_revision: str, cost_model: Any, diagnose=None, summarize=None,
    promotion_store=None, promotion_shadow_recorder=None,
    read_pending_shadow=None, sync_console=None, pull_console=None,
    frozen_proposal=None,
) -> dict[str, Any]:
    """Run one fixed SOXL RSI2 case from one verified P1 root.

    The caller owns the receipt, source blobs, identity, P1 verifier and typed
    promotion plan.  This helper only materializes the two frozen windows and
    passes them through the existing trusted entry; it never derives evidence
    or accepts promotion decisions from JSON.
    """
    if (not isinstance(expected_p1_manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_p1_manifest_sha256) is None):
        raise NewResearchInputError("p1_manifest_invalid")
    try:
        _worker(payload["worker_manifest"])
        if not isinstance(payload["source_receipts"], list) or not payload["source_receipts"]:
            raise NewResearchInputError("source_receipts_required")
        receipts = [_receipt(item) for item in payload["source_receipts"] if isinstance(item, Mapping)]
        if len(receipts) != len(payload["source_receipts"]):
            raise NewResearchInputError("source_receipt_invalid")
        _request(payload["request"], receipts)
        if (not isinstance(payload["source_commit"], str)
                or _REVISION.fullmatch(payload["source_commit"]) is None):
            raise NewResearchInputError("source_commit_invalid")
        if source_revision != payload["source_commit"]:
            raise NewResearchInputError("rsi2_promotion_source_mismatch")
        _digest_map(payload["source_blobs"])
        _identity(payload["research_identity"])
        root = Path(p1_root)
        manifest_bytes = (root / "manifest.json").read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_p1_manifest_sha256:
            raise NewResearchInputError("p1_manifest_invalid")
    except NewResearchInputError:
        raise
    except Exception:
        raise NewResearchInputError("p1_manifest_invalid") from None

    try:
        from us_equity_strategies.research.soxl_alpaca_input_adapter import (
            materialize_soxl_alpaca_input,
        )
        from us_equity_strategies.research.soxl_rsi2_research_adapter import (
            Rsi2OfflineInputPaths, SoxlRsi2PromotionBinding,
            _validate_research_identity, load_rsi2_offline_input,
            validate_promotion_timing,
        )
        from quant_platform_kit.strategy_lifecycle.backtest_orchestrator import (
            _validate_promotion_plan,
        )
        optimization_paths = materialize_soxl_alpaca_input(
            p1_root, optimization_root,
            start="2022-01-03", end_exclusive="2025-01-01", expected_sessions=753,
        )
        promotion_paths = materialize_soxl_alpaca_input(
            p1_root, promotion_root,
            start="2023-09-12", end_exclusive="2026-09-12", expected_sessions=753,
        )
        optimization_input = load_rsi2_offline_input(Rsi2OfflineInputPaths(
            optimization_paths.manifest, optimization_paths.artifact, optimization_paths.readback,
        ))
        promotion_input = load_rsi2_offline_input(Rsi2OfflineInputPaths(
            promotion_paths.manifest, promotion_paths.artifact, promotion_paths.readback,
        ))
        if not isinstance(payload, Mapping) or not isinstance(payload.get("request"), Mapping):
            raise NewResearchInputError("rsi2_dual_window_input_invalid")
        if payload["request"].get("source_revision") != optimization_input.source_revision:
            raise NewResearchInputError("rsi2_optimization_source_mismatch")
        identity = payload.get("research_identity")
        if (not isinstance(identity, Mapping)
                or identity.get("input_revision") != optimization_input.input_digest):
            raise NewResearchInputError("rsi2_optimization_identity_mismatch")
        _validate_research_identity(
            optimization_input, payload["source_commit"], identity,
        )
        if frozen_proposal is not None and dict(getattr(frozen_proposal, "proposed_params", {}) or {}) != {
            "candidate_id": candidate_id
        }:
            raise NewResearchInputError("rsi2_promotion_candidate_mismatch")
        binding = SoxlRsi2PromotionBinding(
            source=promotion_input, candidate_id=candidate_id, folds=folds,
            locked_oos_start=locked_oos_start, locked_oos_end=locked_oos_end,
            purge_days=purge_days, embargo_days=embargo_days,
            source_revision=source_revision, cost_model=cost_model,
        )
        _validate_promotion_plan(
            binding.folds, locked_oos_start=binding.locked_oos_start,
            locked_oos_end=binding.locked_oos_end, purge_days=binding.purge_days,
            embargo_days=binding.embargo_days, source_revision=binding.source_revision,
            cost_model=binding.cost_model,
        )
        validate_promotion_timing(optimization_input, binding)
    except NewResearchInputError:
        raise
    except Exception:
        raise NewResearchInputError("rsi2_dual_window_invalid") from None

    request_payload = dict(payload)
    request_payload["input_paths"] = {
        "manifest": str(optimization_paths.manifest),
        "artifact": str(optimization_paths.artifact),
        "readback": str(optimization_paths.readback),
    }
    return run_request(
        request_payload, diagnose=diagnose, summarize=summarize,
        promotion_binding=binding, promotion_store=promotion_store,
        promotion_shadow_recorder=promotion_shadow_recorder,
        read_pending_shadow=read_pending_shadow, sync_console=sync_console,
        pull_console=pull_console, frozen_proposal=frozen_proposal,
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except Exception as exc:
        raise NewResearchInputError("request_json_invalid") from exc


def _receipt(raw: Mapping[str, Any]) -> ResearchSourceReceipt:
    try:
        value = ResearchSourceReceipt(
            schema_version=str(raw["schema_version"]),
            source_id=str(raw["source_id"]),
            source_url=str(raw["source_url"]),
            publisher=str(raw["publisher"]),
            retrieved_at=datetime.fromisoformat(str(raw["retrieved_at"]).replace("Z", "+00:00")),
            content_sha256=str(raw["content_sha256"]),
            declared_license=raw.get("declared_license"),
            usage_scope=str(raw["usage_scope"]),
            license_review_id=raw.get("license_review_id"),
            untrusted=raw.get("untrusted", True),
        )
    except Exception as exc:
        raise NewResearchInputError("source_receipt_invalid") from exc
    if validate_source_receipt(value) or raw.get("receipt_sha256") != value.receipt_sha256:
        raise NewResearchInputError("source_receipt_invalid")
    return value


def _worker(raw: Mapping[str, Any]) -> None:
    try:
        value = ResearchWorkerManifest(
            schema_version=str(raw["schema_version"]), worker_id=str(raw["worker_id"]),
            role=ResearchWorkerRole(str(raw["role"])),
            capabilities=frozenset(raw["capabilities"]),
            secret_access=raw.get("secret_access", False),
            broker_access=raw.get("broker_access", False),
            cloud_runtime_access=raw.get("cloud_runtime_access", False),
            deployment_write_access=raw.get("deployment_write_access", False),
        )
    except Exception as exc:
        raise NewResearchInputError("worker_manifest_invalid") from exc
    if value.role is not ResearchWorkerRole.PLANNER_BUILDER or value.capabilities != _WORKER_CAPABILITIES:
        raise NewResearchInputError("worker_manifest_invalid")
    if validate_worker_manifest(value):
        raise NewResearchInputError("worker_manifest_invalid")


def _request(raw: Mapping[str, Any], receipts: list[ResearchSourceReceipt]) -> NewResearchRequest:
    try:
        refs = tuple(sorted(
            (ResearchSourceReceiptRef(RESEARCH_SOURCE_RECEIPT_SCHEMA_VERSION, item.receipt_sha256)
             for item in receipts),
            key=lambda item: item.receipt_sha256,
        ))
        value = NewResearchRequest(
            strategy_profile=str(raw["strategy_profile"]), domain=str(raw["domain"]),
            as_of=date.fromisoformat(str(raw["as_of"])),
            source_revision=str(raw["source_revision"]),
            research_intent=str(raw["research_intent"]), source_receipt_refs=refs,
        )
    except Exception as exc:
        raise NewResearchInputError("new_research_request_invalid") from exc
    if value.strategy_profile != "soxl_rsi2_mean_reversion" or value.domain != "us_equity":
        raise NewResearchInputError("new_research_target_invalid")
    return value


def _identity(raw: Any) -> dict[str, str]:
    fields = {"code_revision", "input_revision", "param_space_revision", "cost_model_revision", "validator_revision"}
    if not isinstance(raw, Mapping) or set(raw) != fields or any(not isinstance(v, str) or not v.strip() for v in raw.values()):
        raise NewResearchInputError("research_identity_invalid")
    return dict(raw)


def _digest_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) or not _REVISION.fullmatch(v) for k, v in raw.items()):
        raise NewResearchInputError("source_blobs_invalid")
    return dict(raw)


def _codex_callbacks(*, source_ref: str, facts: Mapping[str, Any]):
    """Bind the existing Codex-only bounded design and summary calls.

    The callbacks only receive frozen local request/facts.  Their text is
    advisory; lifecycle gates and source evidence remain deterministic.
    """
    from ai_gateway_client import AiGatewayClient, GatewayConfig

    config = GatewayConfig.from_env()
    if config.research_providers != ("codex",):
        raise NewResearchInputError("codex_only_required")
    client = AiGatewayClient(config)
    encoded_facts = json.dumps(dict(facts), ensure_ascii=False, allow_nan=False, sort_keys=True)

    def diagnose(context, _budget):
        prompt = (
            "只做受限研究设计选择。只能解释下面冻结的请求和本地事实，禁止联网、工具、"
            "执行代码、改生产代码、交易或改变准入。只输出 JSON，字段必须恰好为 "
            "optimization_needed (boolean) 和 design (string，不超过240字符)。"
            f"\nREQUEST:\n{json.dumps(context.to_dict(), ensure_ascii=False, sort_keys=True)}"
            f"\nLOCAL_FACTS:\n{encoded_facts}"
        )
        response = client.execute(
            prompt, task="new_research_design", mode="review_only", complexity="low",
            research_stage="optimization", sandbox="read-only",
            allowed_providers=["codex"], source_repository="QuantStrategyLab/AIAuditBridge",
            source_ref=source_ref, timeout=300,
        )
        raw = response.raw if isinstance(response.raw, dict) else {}
        if not (response.success is True and raw.get("status") == "succeeded"
                and response.provider == "codex"
                and raw.get("provider") == "codex"
                and raw.get("research_stage") == "optimization"):
            if (raw.get("status") == "deferred" and type(raw.get("retry_at")) in (int, float)
                    and raw["retry_at"] > time.time()):
                return {"optimization_needed": False, "reason": "codex_research_deferred",
                        "retry_at": raw["retry_at"]}
            raise NewResearchInputError("codex_result_invalid")
        try:
            result = json.loads(response.output)
        except Exception:
            raise NewResearchInputError("codex_result_invalid") from None
        if (not isinstance(result, dict) or set(result) != {"optimization_needed", "design"}
                or type(result["optimization_needed"]) is not bool
                or not isinstance(result["design"], str) or not result["design"].strip()
                or len(result["design"]) > 240):
            raise NewResearchInputError("codex_result_invalid")
        effort = raw.get("reasoning_effort")
        job_id = raw.get("job_id")
        if (not isinstance(response.model, str) or not response.model.strip()
                or not isinstance(effort, str) or not effort.strip()
                or not isinstance(job_id, str) or not job_id.strip()):
            raise NewResearchInputError("codex_result_invalid")
        return {"optimization_needed": result["optimization_needed"],
                "design": result["design"].strip(), "provider": response.provider,
                "model": response.model, "reasoning_effort": effort, "job_id": job_id}

    from scripts.research_summary import make_summary_callback
    summarize = make_summary_callback(
        runtime=SimpleNamespace(config=GatewayConfig, client=AiGatewayClient),
        revision=source_ref, repository="QuantStrategyLab/AIAuditBridge",
        local_facts=facts,
        validate_context=lambda value: dict(value) if isinstance(value, Mapping) else (_ for _ in ()).throw(ValueError("summary_context_invalid")),
        research_stage="research_summary",
    )

    return diagnose, summarize


def _instrument_formal_backtest(callback, started):
    def wrapped(proposal):
        started()
        return callback(proposal)
    return wrapped


def run_request(
    payload: Mapping[str, Any], *, diagnose=None, summarize=None,
    promotion_binding=None, promotion_store=None, promotion_shadow_recorder=None,
    read_pending_shadow=None, sync_console=None, pull_console=None,
    frozen_proposal=None, research_budget=None, on_formal_backtest=None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or not _REQUIRED <= set(payload) or set(payload) - _REQUIRED - _OPTIONAL:
        raise NewResearchInputError("new_research_input_fields_invalid")
    _worker(payload["worker_manifest"])
    if not isinstance(payload["source_receipts"], list) or not payload["source_receipts"]:
        raise NewResearchInputError("source_receipts_required")
    receipts = [_receipt(item) for item in payload["source_receipts"] if isinstance(item, Mapping)]
    if len(receipts) != len(payload["source_receipts"]):
        raise NewResearchInputError("source_receipt_invalid")
    request = _request(payload["request"], receipts)
    source_commit = payload["source_commit"]
    if not isinstance(source_commit, str) or not _REVISION.fullmatch(source_commit):
        raise NewResearchInputError("source_commit_invalid")
    source_blobs = _digest_map(payload["source_blobs"])
    paths = payload["input_paths"]
    if not isinstance(paths, Mapping) or set(paths) != {"manifest", "artifact", "readback"}:
        raise NewResearchInputError("input_paths_invalid")
    from us_equity_strategies.research.soxl_rsi2_research_adapter import (
        Rsi2OfflineInputPaths, _validate_research_identity, load_rsi2_offline_input,
        make_soxl_rsi2_optimize, prepare_soxl_rsi2_promotion,
    )
    research_identity = _identity(payload["research_identity"])
    paths_value = Rsi2OfflineInputPaths(**{key: Path(paths[key]) for key in paths})
    try:
        typed_source = load_rsi2_offline_input(paths_value)
        _validate_research_identity(typed_source, source_commit, research_identity)
    except Exception:
        raise NewResearchInputError("rsi2_research_identity_mismatch") from None
    facts = payload.get("strategy_facts", _LOCAL_STRATEGY_FACTS)
    if facts != _LOCAL_STRATEGY_FACTS:
        raise NewResearchInputError("strategy_facts_unverified")
    if frozen_proposal is None:
        optimize = make_soxl_rsi2_optimize(
            input_paths=paths_value,
            output_root=Path(str(payload["output_root"])), source_commit=source_commit,
            source_blobs=source_blobs, ues_repo_root=payload.get("ues_repo_root"),
            source=typed_source, expected_identity=research_identity,
        )
    else:
        def optimize(_request, _budget):
            return frozen_proposal
    try:
        cycle_identity, enforce_backtest_gates, record_shadow = prepare_soxl_rsi2_promotion(
            optimization_source=typed_source,
            source_commit=source_commit,
            research_identity=research_identity,
            promotion_binding=promotion_binding,
            promotion_store=promotion_store,
            promotion_shadow_recorder=promotion_shadow_recorder,
        )
    except Exception as exc:
        if isinstance(exc, NewResearchInputError):
            raise
        raise NewResearchInputError("rsi2_promotion_binding_invalid") from None
    if callable(on_formal_backtest):
        enforce_backtest_gates = _instrument_formal_backtest(
            enforce_backtest_gates, on_formal_backtest,
        )
    if diagnose is None and summarize is None:
        source_ref = payload.get("caller_ref")
        if (not isinstance(facts, Mapping) or not isinstance(source_ref, str)
                or not _REVISION.fullmatch(source_ref)):
            raise NewResearchInputError("strategy_facts_or_caller_ref_invalid")
        diagnose, summarize = _codex_callbacks(source_ref=source_ref, facts=facts)
    result = run_saved_research_promotion_cycle(
        new_request=request, research_identity=cycle_identity,
        ticket_dir=Path(str(payload["ticket_dir"])), optimize=optimize,
        enforce_backtest_gates=enforce_backtest_gates,
        record_shadow=record_shadow,
        read_pending_shadow=read_pending_shadow,
        budget=research_budget or ResearchPromotionBudget(require_paired_shadow=True),
        diagnose=diagnose, summarize=summarize,
        sync_console=sync_console, pull_console=pull_console,
    )
    summary = {key: result[key] for key in ("status", "reason", "research_key", "resumed", "live_authority_granted") if key in result}
    ticket = result.get("ticket")
    if isinstance(ticket, Mapping):
        summary.update({key: ticket[key] for key in ("state", "drift_score", "drift_status", "notes") if key in ticket})
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--soxl-rsi2-controlled", action="store_true")
    parser.add_argument("--p1-root", type=Path)
    parser.add_argument("--ues-repo-root", type=Path)
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.soxl_rsi2_controlled:
            if args.request is not None or args.p1_root is None or args.ues_repo_root is None or args.run_root is None:
                parser.error("controlled SOXL mode requires --p1-root, --ues-repo-root, --run-root and no --request")
            result = run_fixed_soxl_rsi2_case(
                p1_root=args.p1_root, ues_repo_root=args.ues_repo_root, run_root=args.run_root,
            )
        else:
            if args.request is None:
                parser.error("--request is required unless --soxl-rsi2-controlled is used")
            result = run_request(_read_json(args.request))
        args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return 0
    except NewResearchInputError as exc:
        print(f"new_research_{exc}")
        return 2
    except Exception:
        print("new_research_unavailable")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
