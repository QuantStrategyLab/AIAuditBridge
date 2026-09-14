from __future__ import annotations

from datetime import datetime, timezone
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types

import pytest

from quant_platform_kit.research_factory import ResearchSourceReceipt, ResearchWorkerManifest, ResearchWorkerRole
from quant_platform_kit.strategy_lifecycle.contracts import OptimizationProposal
from scripts.run_new_research import (
    NewResearchInputError, _LOCAL_STRATEGY_FACTS, run_request,
    run_trusted_soxl_rsi2_dual_window,
)


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


def test_frozen_proposal_skips_second_optimizer_run(tmp_path, monkeypatch):
    _install_adapter(monkeypatch)
    import us_equity_strategies.research.soxl_rsi2_research_adapter as adapter
    proposal = OptimizationProposal(
        strategy_profile="soxl_rsi2_mean_reversion", domain="us_equity",
        proposed_params={"candidate_id": "UNSCALED_SMA200"}, recommendation="research_candidate",
    )
    monkeypatch.setattr(adapter, "make_soxl_rsi2_optimize", lambda **_: (_ for _ in ()).throw(
        AssertionError("optimizer must not rerun after candidate freeze")
    ))
    result = run_request(
        _payload(tmp_path),
        diagnose=lambda _context, _budget: {"optimization_needed": True, "design": "固定模板研究设计"},
        frozen_proposal=proposal,
    )
    assert result["status"] == "parked"


def test_installed_ues_exposes_formal_promotion_binding_helper():
    from us_equity_strategies.research import soxl_rsi2_research_adapter as adapter

    assert callable(adapter.prepare_soxl_rsi2_promotion)


def _alpaca_source_root(tmp_path: Path, count: int = 753, sessions: list[str] | None = None) -> Path:
    root = tmp_path / "alpaca-p1"
    root.mkdir()
    start = date(2022, 1, 3)
    sessions = sessions or [(start + timedelta(days=index)).isoformat() for index in range(count)]
    cutoff = sessions[-1]

    def canonical(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()

    binding = {
        "schema_version": "qsl.soxl_soxx_core_only_p1_data_binding.v2",
        "data_identity": {
            "provider": "ALPACA_MARKET_DATA", "feed": "SIP",
            "calendar": {"calendar_id": "XNYS", "timezone": "America/New_York", "source": "exchange_calendars:4.13.2:XNYS"},
            "adjustment": {"policy": "total_return_adjusted", "source": "ALPACA_MARKET_DATA adjustment=all(split,dividend,spin-off)"},
            "universe": ["SOXL", "SOXX", "BOXX"], "date_cutoff": cutoff,
        },
    }
    binding_bytes = canonical(binding)
    binding_digest = hashlib.sha256(binding_bytes).hexdigest()
    series = {}
    for symbol, base in (("SOXL", 30.0), ("SOXX", 100.0), ("BOXX", 10.0)):
        rows = []
        for index, session in enumerate(sessions):
            close = base + (index % 7) * 0.2
            rows.append({"session_date": session, "bar": {"open": close, "high": close + 1.0, "low": close - 1.0, "close": close, "volume": 100.0}})
        series[symbol] = rows
    bars = {"schema_version": "qsl.soxl-soxx-core-only-adjusted-ohlcv.v2", "series": series}
    bars_bytes = canonical(bars)
    manifest = {
        "schema_version": "research_input_manifest.v1", "profile": "soxl_soxx_core_only_p2_v3",
        "artifact_type": "immutable_adjusted_ohlcv_etf_only", "observed_at": "2026-01-08T00:00:00Z",
        "calendar": {"calendar_id": "XNYS", "timezone": "America/New_York", "session_date": cutoff, "source_revision": binding_digest},
        "adjustment": {"policy": "total_return_adjusted", "source": "ALPACA_MARKET_DATA adjustment=all(split,dividend,spin-off)", "source_revision": binding_digest},
        "members": [{"path": "bars.json", "media_type": "application/json", "size_bytes": len(bars_bytes), "sha256": hashlib.sha256(bars_bytes).hexdigest()}],
        "sources": [{"source_id": f"alpaca_sip_1day_adjustment_all:{symbol}", "content_sha256": hashlib.sha256(canonical({"schema_version": "qsl.soxl-soxx-core-only-adjusted-ohlcv-source.v1", "symbol": symbol, "sessions": series[symbol]})).hexdigest()} for symbol in ("SOXL", "SOXX", "BOXX")],
    }
    (root / "binding.json").write_bytes(binding_bytes)
    (root / "bars.json").write_bytes(bars_bytes)
    (root / "manifest.json").write_bytes(canonical(manifest))
    return root


def _ues_provenance_repo(tmp_path: Path) -> tuple[Path, str, dict[str, str]]:
    repo = tmp_path / "ues-source"
    required = (
        "src/us_equity_strategies/research/soxl_core_optimization.py",
        "src/us_equity_strategies/research/soxl_soxx_offline_input_contract.py",
        "src/us_equity_strategies/research/soxl_soxx_typed_baseline_result.py",
    )
    for relative in required:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic provenance fixture\n", encoding="utf-8")
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "fixture@example.test")
    git("config", "user.name", "fixture")
    git("add", ".")
    git("commit", "-qm", "synthetic UES provenance")
    commit = git("rev-parse", "HEAD")
    return repo, commit, {path: git("rev-parse", f"HEAD:{path}") for path in required}


def test_installed_ues_alpaca_path_runs_aab_request(tmp_path):
    from us_equity_strategies.research.soxl_alpaca_input_adapter import (
        load_soxl_alpaca_input, materialize_soxl_alpaca_input,
    )
    from us_equity_strategies.research.soxl_rsi2_research_adapter import (
        _COST_MODEL_REVISION, _PARAM_SPACE_REVISION, _VALIDATOR_REVISION,
        Rsi2OfflineInputPaths, load_rsi2_offline_input,
    )

    paths = materialize_soxl_alpaca_input(
        _alpaca_source_root(tmp_path), tmp_path / "alpaca-pack",
        start="2022-01-03", end_exclusive="2024-01-26", expected_sessions=753,
    )
    source = load_soxl_alpaca_input(paths.manifest, paths.artifact, paths.readback)
    routed = load_rsi2_offline_input(Rsi2OfflineInputPaths(paths.manifest, paths.artifact, paths.readback))
    assert source.input_digest == routed.input_digest
    assert source.source_revision == routed.source_revision
    assert source.source_revision.startswith("alpaca_sip:total_return_adjusted:adjustment=all:")

    provenance_repo, source_commit, source_blobs = _ues_provenance_repo(tmp_path)
    payload = _payload(tmp_path)
    payload["source_commit"] = source_commit
    payload["source_blobs"] = source_blobs
    payload["ues_repo_root"] = str(provenance_repo)
    payload["request"]["source_revision"] = source.source_revision
    payload["input_paths"] = {"manifest": str(paths.manifest), "artifact": str(paths.artifact), "readback": str(paths.readback)}
    payload["research_identity"] = {
        "code_revision": source_commit, "input_revision": source.input_digest,
        "param_space_revision": _PARAM_SPACE_REVISION, "cost_model_revision": _COST_MODEL_REVISION,
        "validator_revision": _VALIDATOR_REVISION,
    }
    result = run_request(payload, diagnose=lambda _context, _budget: {
        "optimization_needed": True, "design": "固定模板研究设计",
    })
    assert result["status"] == "parked"
    assert result["reason"] == "promotion_cycle_completed"
    assert result["notes"] == ["recommendation=reject"]
    assert (Path(payload["output_root"]) / "soxl_rsi2_mean_reversion_v1.json").is_file()
    assert result["live_authority_granted"] is False


def test_trusted_dual_window_materializes_both_windows_before_request(tmp_path, monkeypatch):
    import scripts.run_new_research as runner
    from quant_platform_kit.strategy_lifecycle.contracts import PromotionCostModel, PurgedWalkForwardFold
    from us_equity_strategies.research.soxl_rsi2_research_adapter import (
        _COST_MODEL_REVISION, _PARAM_SPACE_REVISION, _VALIDATOR_REVISION,
        load_rsi2_offline_input, Rsi2OfflineInputPaths,
    )
    from us_equity_strategies.research.soxl_alpaca_input_adapter import materialize_soxl_alpaca_input

    def weekdays(start: date, end: date) -> list[str]:
        values = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                values.append(current.isoformat())
            current += timedelta(days=1)
        return values

    # Fixed XNYS sessions from the verified 2022-2026 NYSE calendar; prices remain synthetic.
    holidays = {
        "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20", "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
        "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29", "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
        "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
        "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2025-01-09",
    }
    first_window = [x for x in weekdays(date(2022, 1, 3), date(2024, 12, 31)) if x not in holidays]
    second_window = [x for x in weekdays(date(2023, 9, 12), date(2026, 9, 11)) if x not in holidays]
    sessions = sorted(set(first_window) | set(second_window))
    root = _alpaca_source_root(tmp_path, sessions=sessions)
    optimization_paths = materialize_soxl_alpaca_input(
        root, tmp_path / "optimization-check",
        start="2022-01-03", end_exclusive="2025-01-01", expected_sessions=753,
    )
    optimization = load_rsi2_offline_input(Rsi2OfflineInputPaths(
        optimization_paths.manifest, optimization_paths.artifact, optimization_paths.readback,
    ))
    promotion_paths = materialize_soxl_alpaca_input(
        root, tmp_path / "promotion-check",
        start="2023-09-12", end_exclusive="2026-09-12", expected_sessions=753,
    )
    promotion = load_rsi2_offline_input(Rsi2OfflineInputPaths(
        promotion_paths.manifest, promotion_paths.artifact, promotion_paths.readback,
    ))
    promotion_dates = sorted({date.fromisoformat(row.as_of) for row in promotion.rows})
    folds = (
        # Fixed before OOS; QPK validates 20-day purge and 20-day embargo.
        PurgedWalkForwardFold(date(2023, 9, 12), date(2024, 12, 31), date(2025, 1, 27), date(2025, 2, 7)),
        PurgedWalkForwardFold(date(2025, 3, 3), date(2025, 3, 14), date(2025, 4, 7), date(2025, 4, 18)),
        PurgedWalkForwardFold(date(2025, 5, 12), date(2025, 5, 23), date(2025, 6, 16), date(2025, 6, 27)),
    )
    payload = _payload(tmp_path)
    payload["request"]["source_revision"] = optimization.source_revision
    payload["research_identity"] = {
        "code_revision": "a" * 40, "input_revision": optimization.input_digest,
        "param_space_revision": _PARAM_SPACE_REVISION, "cost_model_revision": _COST_MODEL_REVISION,
        "validator_revision": _VALIDATOR_REVISION,
    }
    captured = {}
    original_run_request = runner.run_request
    monkeypatch.setattr(
        "scripts.run_new_research.run_request",
        lambda request_payload, **kwargs: captured.update(payload=request_payload, kwargs=kwargs) or {"status": "parked"},
    )
    binding_source = promotion
    result = run_trusted_soxl_rsi2_dual_window(
        payload, p1_root=root, optimization_root=tmp_path / "optimization",
        promotion_root=tmp_path / "promotion",
        expected_p1_manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
        candidate_id="UNSCALED_SMA200",
        folds=folds, locked_oos_start=date(2025, 8, 1), locked_oos_end=date(2026, 9, 11),
        purge_days=20, embargo_days=20, source_revision="a" * 40,
        cost_model=PromotionCostModel("C2_5", 2.0, 5.0),
    )
    assert result == {"status": "parked"}
    assert captured["payload"]["research_identity"]["input_revision"] == optimization.input_digest
    assert captured["payload"]["input_paths"]["manifest"].endswith("optimization/prices.csv.manifest.json")
    binding = captured["kwargs"]["promotion_binding"]
    assert binding.source.input_digest == binding_source.input_digest
    assert binding.locked_oos_start == date(2025, 8, 1)
    assert promotion_dates[-1] >= date(2026, 9, 11)

    # Full fixed caller path: real materializer, real optimizer, and real AAB/QPK
    # request cycle; only the AI diagnosis is deterministic in this synthetic test.
    monkeypatch.setattr(runner, "run_request", original_run_request)
    monkeypatch.setattr(runner, "SOXL_P1_MANIFEST_SHA256", hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest())
    synthetic_ues, synthetic_commit, synthetic_blobs = _ues_provenance_repo(tmp_path)
    monkeypatch.setattr(runner, "SOXL_UES_COMMIT", synthetic_commit)
    monkeypatch.setattr(runner, "read_soxl_ues_provenance", lambda _root: (synthetic_commit, synthetic_blobs))
    case_result = runner.run_fixed_soxl_rsi2_case(
        p1_root=root, ues_repo_root=synthetic_ues,
        run_root=tmp_path / "fixed-case", diagnose=lambda _context, _budget: {
            "optimization_needed": True, "design": "固定模板研究设计",
        },
    )
    assert case_result["status"] == "parked"
    assert case_result["live_authority_granted"] is False

    bad_identity = dict(payload)
    bad_identity["research_identity"] = dict(payload["research_identity"])
    bad_identity["research_identity"]["input_revision"] = "f" * 64
    with pytest.raises(NewResearchInputError, match="rsi2_optimization_identity_mismatch"):
        run_trusted_soxl_rsi2_dual_window(
            bad_identity, p1_root=root, optimization_root=tmp_path / "optimization-identity-error",
            promotion_root=tmp_path / "promotion-identity-error",
            expected_p1_manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
            candidate_id="UNSCALED_SMA200", folds=folds, locked_oos_start=date(2025, 8, 1),
            locked_oos_end=date(2026, 9, 11), purge_days=20, embargo_days=20,
            source_revision="a" * 40, cost_model=PromotionCostModel("C2_5", 2.0, 5.0),
        )
    overlap_folds = (
        PurgedWalkForwardFold(date(2024, 11, 1), date(2024, 11, 15), date(2024, 12, 1), date(2024, 12, 10)),
        *folds[1:],
    )
    with pytest.raises(NewResearchInputError, match="rsi2_dual_window_invalid"):
        run_trusted_soxl_rsi2_dual_window(
            payload, p1_root=root, optimization_root=tmp_path / "optimization-overlap",
            promotion_root=tmp_path / "promotion-overlap",
            expected_p1_manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
            candidate_id="UNSCALED_SMA200", folds=overlap_folds, locked_oos_start=date(2025, 8, 1),
            locked_oos_end=date(2026, 9, 11), purge_days=20, embargo_days=20,
            source_revision="a" * 40, cost_model=PromotionCostModel("C2_5", 2.0, 5.0),
        )


def test_trusted_dual_window_rejects_bad_manifest_before_request(tmp_path, monkeypatch):
    called = {"run": 0}
    monkeypatch.setattr("scripts.run_new_research.run_request", lambda *_args, **_kwargs: called.__setitem__("run", 1))
    with pytest.raises(NewResearchInputError, match="p1_manifest_invalid"):
        run_trusted_soxl_rsi2_dual_window(
            _payload(tmp_path), p1_root=tmp_path, optimization_root=tmp_path / "optimization",
            promotion_root=tmp_path / "promotion", expected_p1_manifest_sha256="b" * 64,
            candidate_id="UNSCALED_SMA200", folds=(), locked_oos_start=date(2025, 8, 1),
            locked_oos_end=date(2026, 9, 11), purge_days=20, embargo_days=20,
            source_revision="a" * 40, cost_model=object(),
        )
    assert called["run"] == 0


def test_trusted_dual_window_rejects_missing_promotion_window_before_request(tmp_path, monkeypatch):
    root = _alpaca_source_root(tmp_path)
    payload = _payload(tmp_path)
    from us_equity_strategies.research.soxl_alpaca_input_adapter import materialize_soxl_alpaca_input
    from us_equity_strategies.research.soxl_rsi2_research_adapter import Rsi2OfflineInputPaths, load_rsi2_offline_input
    opt_paths = materialize_soxl_alpaca_input(
        root, tmp_path / "identity-check", start="2022-01-03", end_exclusive="2024-01-26", expected_sessions=753,
    )
    payload["request"]["source_revision"] = load_rsi2_offline_input(Rsi2OfflineInputPaths(
        opt_paths.manifest, opt_paths.artifact, opt_paths.readback,
    )).source_revision
    monkeypatch.setattr("scripts.run_new_research.run_request", lambda *_args, **_kwargs: pytest.fail("AI/request must not run"))
    with pytest.raises(NewResearchInputError, match="rsi2_dual_window_invalid"):
        run_trusted_soxl_rsi2_dual_window(
            payload, p1_root=root, optimization_root=tmp_path / "optimization",
            promotion_root=tmp_path / "promotion",
            expected_p1_manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
            candidate_id="UNSCALED_SMA200", folds=(), locked_oos_start=date(2025, 8, 1),
            locked_oos_end=date(2026, 9, 11), purge_days=20, embargo_days=20,
            source_revision="a" * 40, cost_model=object(),
        )


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


def _complete_paired_shadow():
    from quant_platform_kit.strategy_lifecycle.forward_observation import ForwardObservationPolicy
    from quant_platform_kit.strategy_lifecycle.forward_observation_receipt import (
        FORWARD_OBSERVATION_DEPENDENCY_DIGESTS, build_forward_observation_receipt,
    )
    from quant_platform_kit.strategy_lifecycle.paired_shadow_evidence import build_paired_shadow_evidence

    policy = ForwardObservationPolicy(
        candidate_id="UNSCALED_SMA200", strategy_profile="soxl_rsi2_mean_reversion",
        domain="us_equity", benchmark_symbol="SOXX", required_trading_sessions=2,
        review_milestones=(1,), automatic_non_live_modes=("shadow", "paper"),
        auto_resume_clean_sessions=2, observation_calendar="XNYS",
        observation_window_type="fixed", observation_start_session="2026-09-14",
        window_rationale_ref="sha256:soxl-rsi2-forward-window",
        non_live_evidence_modes=("shadow_decision", "simulated_replay"),
    )
    dependencies = {field: character * 64 for field, character in zip(
        sorted(FORWARD_OBSERVATION_DEPENDENCY_DIGESTS), "abcdef")}
    def leg(name):
        return {"signal": {"kind": "target_weight", "source": name},
                "hypothetical_order": {"kind": "rebalance_preview", "source": name},
                "position": {"kind": "end_of_snapshot", "source": name},
                "cost": {"kind": "configured_cost_model", "source": name},
                "return": {"kind": "one_snapshot_return", "source": name}}
    first_receipt = build_forward_observation_receipt(
        policy=policy, observation_session="2026-09-14", observation_index=1,
        dependency_digests=dependencies, evidence_modes=policy.non_live_evidence_modes,
    )
    earlier = build_paired_shadow_evidence(
        policy=policy, forward_observation_receipt=first_receipt,
        baseline_id="baseline", observed_at="2026-09-14T20:00:00Z",
        input_snapshot_sha256="a" * 64, candidate=leg("candidate"), baseline=leg("baseline"),
    )
    second_receipt = build_forward_observation_receipt(
        policy=policy, observation_session="2026-09-15", observation_index=2,
        dependency_digests=dependencies, evidence_modes=policy.non_live_evidence_modes,
        previous_receipt=first_receipt,
    )
    return {"status": "complete", "observation": dict(
        policy=policy, forward_observation_receipt=second_receipt,
        baseline_id="baseline", observed_at="2026-09-15T20:00:00Z",
        input_snapshot_sha256="a" * 64, candidate=leg("candidate"), baseline=leg("baseline"),
        previous_evidence=earlier, previous_forward_observation_receipt=first_receipt,
    )}


def test_actual_ues_binding_qpk_cycle_and_reentry(tmp_path, monkeypatch):
    from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore
    import quant_platform_kit.strategy_lifecycle.research_promotion_cycle as qpk_cycle
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
            search_iterations=4, computed_at=clock[0].isoformat(),
        ),
    )
    optimize_calls = {"count": 0}
    original_run = adapter.run_soxl_rsi2_mean_reversion

    def counted_run(source):
        optimize_calls["count"] += 1
        return original_run(source)

    monkeypatch.setattr(adapter, "run_soxl_rsi2_mean_reversion", counted_run)

    shadow_calls = {"count": 0}
    clock = [datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(qpk_cycle, "_clock_now", lambda: clock[0])

    def shadow(_proposal):
        shadow_calls["count"] += 1
        return {"status": "pending", "passed": False, "no_order": True,
                "live_authority_granted": False,
                "retry_at": (clock[0] + timedelta(seconds=60)).timestamp()}

    reader_calls = {"count": 0}

    def reader(_proposal):
        reader_calls["count"] += 1
        return _complete_paired_shadow()

    sync_calls = {"count": 0}
    synced_ticket = {}

    def sync(ticket):
        sync_calls["count"] += 1
        synced_ticket.update(ticket.to_dict(include_progress=True))
        return True

    pull_calls = {"count": 0}

    def pull(_ticket_id):
        pull_calls["count"] += 1
        from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import (
            ResearchPromotionTicket, apply_human_promotion_decision,
        )
        ticket = ResearchPromotionTicket.from_dict(synced_ticket)
        return apply_human_promotion_decision(
            ticket, decision="reject", decided_at=clock[0].isoformat(),
        ).to_dict(include_progress=True)

    kwargs = {
        "promotion_binding": binding,
        "promotion_store": PerformanceStore(local_root=tmp_path / "promotion-store"),
        "promotion_shadow_recorder": shadow,
        "read_pending_shadow": reader,
        "sync_console": sync,
        "pull_console": pull,
    }
    def diagnose(_context, _budget):
        return {"optimization_needed": True, "design": "固定模板研究设计"}

    summary_calls = {"count": 0}

    def summarize(_context):
        summary_calls["count"] += 1
        return {"status": "available", "text": "这是受限研究说明。", "provider": "codex",
                "model": "fixture-codex"}

    kwargs["summarize"] = summarize
    no_binding_payload = dict(payload)
    no_binding_payload["ticket_dir"] = str(tmp_path / "no-binding-tickets")
    no_binding = run_request(no_binding_payload, diagnose=diagnose)
    assert no_binding["notes"] == ["promotion_backtest_gate_failed", "missing_promotion_backtest_evidence"]
    assert shadow_calls["count"] == 0
    first = run_request(payload, diagnose=diagnose, **kwargs)
    assert reader_calls["count"] == 0
    clock[0] = datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc)
    second = run_request(payload, diagnose=diagnose, **kwargs)
    assert first["status"] == "deferred"
    assert second["resumed"] is True
    assert second["reason"] == "promotion_cycle_completed"
    assert optimize_calls["count"] == 2
    assert shadow_calls["count"] == 1
    assert reader_calls["count"] == 1
    assert sync_calls["count"] == 1
    assert summary_calls["count"] == 1
    assert synced_ticket["research_summary"]["ai_explanation"]["status"] == "available"
    third = run_request(payload, diagnose=diagnose, **kwargs)
    assert third["reason"] == "saved_research_ticket_reused"
    assert third["state"] == "human_rejected"
    assert third["live_authority_granted"] is False
    assert pull_calls["count"] == 1
    assert optimize_calls["count"] == 2
    assert shadow_calls["count"] == 1
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

    invalid_payload = dict(payload)
    invalid_payload["ticket_dir"] = str(tmp_path / "invalid-shadow-tickets")
    invalid_reader_calls = {"count": 0}

    def invalid_reader(_proposal):
        invalid_reader_calls["count"] += 1
        return {"status": "complete", "observation": {}}

    invalid_first = run_request(
        invalid_payload, diagnose=diagnose,
        promotion_binding=binding, promotion_store=kwargs["promotion_store"],
        promotion_shadow_recorder=shadow, read_pending_shadow=invalid_reader,
        sync_console=sync, pull_console=pull,
    )
    assert invalid_first["status"] == "deferred"
    sync_before_invalid = sync_calls["count"]
    clock[0] += timedelta(seconds=120)
    invalid_second = run_request(
        invalid_payload, diagnose=diagnose,
        promotion_binding=binding, promotion_store=kwargs["promotion_store"],
        promotion_shadow_recorder=shadow, read_pending_shadow=invalid_reader,
        sync_console=sync, pull_console=pull,
    )
    assert invalid_second["reason"] == "research_outcome_unknown"
    assert invalid_reader_calls["count"] == 1
    assert sync_calls["count"] == sync_before_invalid
