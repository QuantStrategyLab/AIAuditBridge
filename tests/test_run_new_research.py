from __future__ import annotations

from datetime import datetime, timezone
from datetime import date, timedelta
import hashlib
import sys
import types

import pytest

from quant_platform_kit.research_factory import ResearchSourceReceipt, ResearchWorkerManifest, ResearchWorkerRole
from quant_platform_kit.strategy_lifecycle.contracts import OptimizationProposal
from scripts.run_new_research import NewResearchInputError, _LOCAL_STRATEGY_FACTS, run_request


def _receipt() -> dict:
    value = ResearchSourceReceipt(
        schema_version="research_source_receipt.v1", source_id="soxl-bars",
        source_url="https://example.test/soxl.csv", publisher="example",
        retrieved_at=datetime(2026, 9, 1, tzinfo=timezone.utc), content_sha256="a" * 64,
        declared_license="CC-BY-4.0", usage_scope="candidate_copy", license_review_id="review-1",
    )
    return value.to_dict()


def _payload(tmp_path):
    worker = ResearchWorkerManifest.expected(worker_id="worker-1", role=ResearchWorkerRole.PLANNER_BUILDER)
    return {
        "request": {"strategy_profile": "soxl_rsi2_mean_reversion", "domain": "us_equity",
                    "as_of": "2026-09-08", "source_revision": "source-v1",
                    "research_intent": "bounded_template_selection"},
        "worker_manifest": {"schema_version": worker.schema_version, "worker_id": worker.worker_id,
                            "role": worker.role.value, "capabilities": sorted(worker.capabilities),
                            "secret_access": False, "broker_access": False,
                            "cloud_runtime_access": False, "deployment_write_access": False},
        "source_receipts": [_receipt()],
        "research_identity": {key: f"{key}-v1" for key in ("code_revision", "input_revision", "param_space_revision", "cost_model_revision", "validator_revision")},
        "source_commit": "a" * 40,
        "source_blobs": {"study.py": "b" * 40},
        "strategy_facts": _LOCAL_STRATEGY_FACTS,
        "caller_ref": "c" * 40,
        "input_paths": {"manifest": "manifest.json", "artifact": "artifact.csv", "readback": "readback.json"},
        "output_root": str(tmp_path / "output"), "ticket_dir": str(tmp_path / "tickets"),
    }


def _install_adapter(monkeypatch, *, prepare=None):
    package = types.ModuleType("us_equity_strategies")
    research = types.ModuleType("us_equity_strategies.research")
    adapter = types.ModuleType("us_equity_strategies.research.soxl_rsi2_research_adapter")
    class Paths:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    def factory(**_kwargs):
        def optimize(request, _budget):
            assert request.strategy_profile == "soxl_rsi2_mean_reversion"
            return OptimizationProposal(strategy_profile=request.strategy_profile, domain=request.domain,
                                        proposed_params={"candidate_id": "UNSCALED_SMA200"}, recommendation="research_candidate")
        return optimize
    adapter.Rsi2OfflineInputPaths = Paths
    adapter.load_rsi2_offline_input = lambda _paths: object()
    adapter._validate_research_identity = lambda _source, _commit, _identity: None
    adapter.make_soxl_rsi2_optimize = factory
    adapter.prepare_soxl_rsi2_promotion = prepare or (
        lambda *, optimization_source, source_commit, research_identity,
        promotion_binding, promotion_store, promotion_shadow_recorder: (
            dict(research_identity),
            lambda _proposal: None,
            lambda _proposal: {"status": "pending", "passed": False, "no_order": True,
                               "live_authority_granted": False},
        )
    )
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, research.__name__, research)
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)


def test_new_request_runs_existing_qpk_cycle_and_parks_without_promotion(tmp_path, monkeypatch):
    _install_adapter(monkeypatch)
    result = run_request(_payload(tmp_path), diagnose=lambda _context, _budget: {
        "optimization_needed": True, "design": "固定模板研究设计"
    })
    assert result["status"] == "parked"
    assert result["notes"] == ["promotion_backtest_gate_failed", "missing_promotion_backtest_evidence"]
    assert result["drift_score"] is None and result["drift_status"] == "new_research"
    assert result["live_authority_granted"] is False


def test_invalid_worker_or_receipt_fails_before_adapter(tmp_path):
    payload = _payload(tmp_path)
    payload["worker_manifest"]["secret_access"] = True
    with pytest.raises(NewResearchInputError, match="worker_manifest_invalid"):
        run_request(payload)
    payload = _payload(tmp_path)
    payload["source_receipts"][0]["receipt_sha256"] = "c" * 64
    with pytest.raises(NewResearchInputError, match="source_receipt_invalid"):
        run_request(payload)


def test_default_entry_requires_verified_adapter_facts_and_caller_ref(tmp_path, monkeypatch):
    _install_adapter(monkeypatch)
    payload = _payload(tmp_path)
    payload.pop("strategy_facts")
    payload.pop("caller_ref")
    with pytest.raises(NewResearchInputError, match="strategy_facts_or_caller_ref_invalid"):
        run_request(payload)


def test_formal_binding_is_passed_to_ues_prepare_and_identity_is_used(tmp_path, monkeypatch):
    calls = []

    def prepare(**kwargs):
        calls.append(kwargs)
        return ({**kwargs["research_identity"], "input_revision": "formal-root"},
                lambda _proposal: {"status": "PASS", "evidence_kind": "formal_backtest"},
                lambda _proposal: {"status": "pending", "passed": False, "no_order": True,
                                   "live_authority_granted": False})

    _install_adapter(monkeypatch, prepare=prepare)
    binding = object()
    store = object()
    def recorder(_proposal):
        return {"status": "pending", "passed": False, "no_order": True,
                "live_authority_granted": False}
    result = run_request(_payload(tmp_path), diagnose=lambda _context, _budget: {
        "optimization_needed": True, "design": "固定模板研究设计"
    }, promotion_binding=binding, promotion_store=store,
        promotion_shadow_recorder=recorder)
    assert result["status"] == "parked"
    assert calls and calls[0]["optimization_source"] is not None
    assert calls[0]["promotion_binding"] is binding
    assert calls[0]["promotion_store"] is store
    assert calls[0]["promotion_shadow_recorder"] is recorder
    assert result["reason"] == "promotion_cycle_completed"
    assert result["drift_status"] == "new_research"


def test_installed_ues_exposes_formal_promotion_binding_helper():
    from us_equity_strategies.research import soxl_rsi2_research_adapter as adapter

    assert callable(adapter.prepare_soxl_rsi2_promotion)


def _synthetic_ues_source(start: date, source_revision: str):
    from us_equity_strategies.research.soxl_soxx_offline_input_contract import (
        InputRow, OfflineInput, _canonical,
    )

    rows = []
    for index in range(753):
        day = (start + timedelta(days=index)).isoformat()
        for symbol, close in (("SOXL", 30.0 + (index % 7) * 0.2),
                              ("SOXX", 100.0 + (index % 11) * 0.1)):
            rows.append(InputRow(symbol, day, close, close + 1.0, close - 1.0, close, 100.0))
    typed = tuple(rows)
    canonical = _canonical(typed)
    return OfflineInput(typed, canonical, hashlib.sha256(canonical).hexdigest(), source_revision)


def _shift_ues_source(source, days: int):
    from us_equity_strategies.research.soxl_soxx_offline_input_contract import (
        InputRow, OfflineInput, _canonical,
    )

    rows = tuple(sorted(
        (InputRow(row.symbol, (date.fromisoformat(row.as_of) + timedelta(days=days)).isoformat(),
                   row.open, row.high, row.low, row.close, row.volume) for row in source.rows),
        key=lambda row: (row.as_of, row.symbol),
    ))
    canonical = _canonical(rows)
    return OfflineInput(rows, canonical, hashlib.sha256(canonical).hexdigest(), "promotion-source")


def _binding_for_source(source):
    from quant_platform_kit.strategy_lifecycle.contracts import PromotionCostModel, PurgedWalkForwardFold
    from us_equity_strategies.research.soxl_rsi2_promotion_runner import SoxlRsi2PromotionBinding

    dates = sorted({date.fromisoformat(row.as_of) for row in source.rows})
    folds = (
        PurgedWalkForwardFold(dates[0], dates[150], dates[200], dates[210]),
        PurgedWalkForwardFold(dates[220], dates[235], dates[250], dates[260]),
        PurgedWalkForwardFold(dates[270], dates[275], dates[285], dates[295]),
    )
    return SoxlRsi2PromotionBinding(
        source=source, candidate_id="UNSCALED_SMA200", folds=folds,
        locked_oos_start=dates[310], locked_oos_end=dates[752],
        purge_days=5, embargo_days=5, source_revision="a" * 40,
        cost_model=PromotionCostModel("C2_5", 2.0, 5.0),
    )


def test_actual_ues_binding_qpk_cycle_and_reentry(tmp_path, monkeypatch):
    from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore
    from us_equity_strategies.research import soxl_rsi2_research_adapter as adapter

    optimization = _synthetic_ues_source(date(2022, 1, 3), "source-v1")
    promotion = _shift_ues_source(optimization, 800)
    binding = _binding_for_source(promotion)
    payload = _payload(tmp_path)
    payload["source_commit"] = "a" * 40
    payload["research_identity"] = {
        "code_revision": "a" * 40,
        "input_revision": optimization.input_digest,
        "param_space_revision": adapter._PARAM_SPACE_REVISION,
        "cost_model_revision": adapter._COST_MODEL_REVISION,
        "validator_revision": adapter._VALIDATOR_REVISION,
    }
    payload["input_paths"] = {"manifest": "unused", "artifact": "unused", "readback": "unused"}
    monkeypatch.setattr(adapter, "load_rsi2_offline_input", lambda _paths: optimization)
    monkeypatch.setattr(adapter, "persist_rsi2_mean_reversion_result", lambda *args, **kwargs: None)
    from quant_platform_kit.strategy_lifecycle.contracts import OptimizationProposal

    monkeypatch.setattr(
        adapter,
        "_proposal_from_result",
        lambda _result: OptimizationProposal(
            strategy_profile=adapter.PROFILE, domain=adapter.DOMAIN,
            current_params={"candidate_id": "UNSCALED_SMA200"},
            proposed_params={"candidate_id": "UNSCALED_SMA200"},
            recommendation="research_candidate", improvement_score=0.0,
            confidence=0.0, winning_dimensions=(), regressing_dimensions=(),
            walk_forward_passed=False, optimization_method="synthetic_fixture",
            search_iterations=4,
        ),
    )
    optimize_calls = {"count": 0}
    original_run = adapter.run_soxl_rsi2_mean_reversion

    def counted_run(source):
        optimize_calls["count"] += 1
        return original_run(source)

    monkeypatch.setattr(adapter, "run_soxl_rsi2_mean_reversion", counted_run)

    shadow_calls = {"count": 0}

    def shadow(_proposal):
        shadow_calls["count"] += 1
        return {"status": "pending", "passed": False, "no_order": True,
                "live_authority_granted": False}

    kwargs = {
        "promotion_binding": binding,
        "promotion_store": PerformanceStore(local_root=tmp_path / "promotion-store"),
        "promotion_shadow_recorder": shadow,
    }
    def diagnose(_context, _budget):
        return {"optimization_needed": True, "design": "固定模板研究设计"}
    no_binding_payload = dict(payload)
    no_binding_payload["ticket_dir"] = str(tmp_path / "no-binding-tickets")
    no_binding = run_request(no_binding_payload, diagnose=diagnose)
    assert no_binding["notes"] == ["promotion_backtest_gate_failed", "missing_promotion_backtest_evidence"]
    assert shadow_calls["count"] == 0
    first = run_request(payload, diagnose=diagnose, **kwargs)
    second = run_request(payload, diagnose=diagnose, **kwargs)
    assert first["status"] == "deferred"
    assert second["resumed"] is True
    assert optimize_calls["count"] == 2
    assert shadow_calls["count"] == 1
    assert first["reason"] == "paired_shadow_observation_pending"
    from dataclasses import replace

    changed_binding = replace(binding, embargo_days=6)
    changed_payload = dict(payload)
    changed = run_request(changed_payload, diagnose=diagnose,
                          promotion_binding=changed_binding,
                          promotion_store=kwargs["promotion_store"],
                          promotion_shadow_recorder=shadow)
    assert changed["status"] == "deferred"
    assert changed["reason"] == "paired_shadow_observation_pending"
    assert optimize_calls["count"] == 3
    assert shadow_calls["count"] == 2
