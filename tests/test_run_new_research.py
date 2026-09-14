from __future__ import annotations

from datetime import datetime, timezone
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


def _install_adapter(monkeypatch):
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
