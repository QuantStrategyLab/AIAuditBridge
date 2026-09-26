"""Offline mapping from sanitized M1/R9 aggregates to the research-task consumer.

Public hashes come from the batch execution note. These tests do not read
private ledgers, URIs, raw bars, positions, credentials, or personal accounts.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import unittest
from pathlib import Path

from service.research_task import (
    ResearchTaskError,
    build_strategy_diagnosis_task,
    calculate_task_sha256,
    canonical_json,
    validate_strategy_diagnosis_task,
)
from service.research_result_handoff import (
    ResearchResultHandoffError,
    map_research_result,
)


AAB_BASE = "d47a78d0c538e790310a610298842e5cf3db3c16"
UES_REVISION = "1c4a1c3118d4d482bdb7191f9b7cda40cd4955cf"
M1_POLICY_ID = "post_r9_us_equity_dtc_standard_settlement_v1"
M1_POLICY_DIGEST = "c135c023ee7329ad6103021ffbb79d4cdfea01e903ac331865c157a6a1246853"
M1_SUMMARY = "e0e5c2e51836edb246e70cd9ea7f4b64def713928aafd4d6933b403a69f793cc"
M1_B0_LEDGER = "68b96ff510bec653c2286d456b719fa680a7a731debd71b0a1bb27b4c57392ba"
M1_DYNAMIC_LEDGER = "9ab7b28d0fa9a024c389d49d4eaa3f79adae591cd100d80411f125bf03da832e"
R9_SUMMARY = "628de89afde2fad718ae6298370e895585d3c4c082452a5c9044b5abeb2ba95f"
P3_HYPOTHESIS = (
    "A verified P3 observation crossed a degradation threshold; diagnose it with "
    "one bounded offline comparison without changing active parameters."
)
_FORBIDDEN_KEYS = ("uri", "path", "raw", "daily", "credential", "account", "personal")


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _seal(fields: dict) -> dict:
    sealed = copy.deepcopy(fields)
    sealed["result_digest"] = _sha(sealed["result"])
    body = {key: sealed[key] for key in _BODY_KEYS if key in sealed}
    sealed["aggregate_sha256"] = _sha(body)
    return sealed


_BODY_KEYS = (
    "authority",
    "candidate_id",
    "cost_contract",
    "inputs",
    "policy_digest",
    "policy_id",
    "portfolio_id",
    "producer_revision",
    "result",
    "result_digest",
    "settlement_policy_digest",
    "settlement_policy_id",
    "signals",
    "source_assurance",
    "stage",
    "strategy_revision",
    "study_id",
    "summary_digest",
)


def _m1_fields(**overrides) -> dict:
    fields = {
        "portfolio_id": "post_r9_us_equity",
        "study_id": "m1_settlement_sensitivity",
        "candidate_id": "post_r9_m1_settlement",
        "strategy_revision": UES_REVISION,
        "producer_revision": UES_REVISION,
        "inputs": [
            {"name": "b0_ledger", "digest": M1_B0_LEDGER},
            {"name": "dynamic_ledger", "digest": M1_DYNAMIC_LEDGER},
        ],
        "summary_digest": M1_SUMMARY,
        "result": {"session_count": 856},
        "policy_id": M1_POLICY_ID,
        "policy_digest": M1_POLICY_DIGEST,
        "settlement_policy_id": M1_POLICY_ID,
        "settlement_policy_digest": M1_POLICY_DIGEST,
        "cost_contract": {"contract_id": "flat_10bps", "cost_bps": 10},
        "source_assurance": "development",
        "stage": "development",
        "authority": {"no_order": True, "research_only": True},
    }
    fields.update(overrides)
    return fields


def _r9_fields(**overrides) -> dict:
    """Caller-bound explicit index plus the published R9 summary digest.

    The two input digests are hashes of public fixture labels. They are not
    historical R9 ledger identities.
    """
    fields = {
        "portfolio_id": "post_r9_us_equity",
        "study_id": "r9_validation",
        "candidate_id": "r9_validation",
        "strategy_revision": UES_REVISION,
        "producer_revision": AAB_BASE,
        "inputs": [
            {"name": "explicit_slot_a", "digest": hashlib.sha256(b"post-r9-batch2-r9-explicit-slot-a").hexdigest()},
            {"name": "explicit_slot_b", "digest": hashlib.sha256(b"post-r9-batch2-r9-explicit-slot-b").hexdigest()},
        ],
        "summary_digest": R9_SUMMARY,
        "result": {"explicit_numeric_result": 1},
        "policy_id": "r9_unchanged_result",
        "policy_digest": hashlib.sha256(b"post-r9-batch2-r9-policy-label").hexdigest(),
        "settlement_policy_id": "r9_settlement_unspecified",
        "settlement_policy_digest": hashlib.sha256(b"post-r9-batch2-r9-settlement-label").hexdigest(),
        "cost_contract": {"contract_id": "r9_cost_unspecified", "cost_bps": 0},
        "source_assurance": "development",
        "stage": "development",
        "authority": {"no_order": True, "research_only": True},
    }
    fields.update(overrides)
    return fields


def _qrs_validate():
    configured = os.environ.get("QRS_RESEARCH_TASK_CONTRACT")
    if not configured:
        raise unittest.SkipTest("QRS_RESEARCH_TASK_CONTRACT is not configured")
    script = Path(configured)
    spec = importlib.util.spec_from_file_location("qrs_research_task_contract", script)
    if spec is None or spec.loader is None:
        raise AssertionError("QRS research task contract is not readable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_research_task


class _Counter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args, **_kwargs) -> None:
        self.calls += 1


def _walk_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


class ResearchResultHandoffTests(unittest.TestCase):
    def test_published_m1_output_set_stays_advisory_and_incompatible(self) -> None:
        sealed = _seal(_m1_fields())
        projected = map_research_result(sealed, sealed)

        self.assertEqual(projected["status"], "incompatible")
        self.assertEqual(projected["disposition"], "advisory")
        self.assertIs(projected["research_only"], True)
        self.assertIs(projected["no_order"], True)
        self.assertEqual(projected["stage"], "development")
        self.assertEqual(projected["source_assurance"], "development")
        self.assertNotIn("task", projected)
        self.assertNotIn("p3_evidence_id", projected)
        self.assertEqual(projected["consumer_base"], AAB_BASE)
        self.assertEqual(projected["summary_digest"], M1_SUMMARY)
        reasons = json.dumps(projected["incompatibilities"])
        self.assertIn("evidence.p3_evidence_id", reasons)
        self.assertIn("validate_strategy_diagnosis_task", reasons)
        self.assertIn("validate_research_task", reasons)
        self.assertIn("diagnose_degradation", reasons)
        self.assertIn(P3_HYPOTHESIS, reasons)
        self.assertIn(M1_SUMMARY, reasons)
        self.assertFalse(projected["adoption"])
        self.assertFalse(projected["trade"])
        self.assertFalse(projected["repair_to_profit"])

    def test_r9_public_summary_is_incompatible_without_a_task(self) -> None:
        sealed = _seal(_r9_fields())
        projected = map_research_result(sealed, sealed)

        self.assertEqual(projected["status"], "incompatible")
        self.assertNotIn("task", projected)
        self.assertEqual(projected["source_assurance"], "development")
        reasons = json.dumps(projected["incompatibilities"])
        self.assertIn(R9_SUMMARY, reasons)
        self.assertIn("evidence.p3_evidence_id", reasons)
        self.assertNotIn(R9_SUMMARY, json.dumps(projected.get("input_index")))

    def test_structural_hash_acceptance_does_not_fill_p3_evidence_id(self) -> None:
        task = build_strategy_diagnosis_task(
            event_key="a1b2c3d4e5f6",
            created_at="2026-09-27T00:00:00Z",
            candidate_id="post_r9_m1_settlement",
            candidate_kind="portfolio",
            domain="us_equity",
            strategy_repository="QuantStrategyLab/UsEquityStrategies",
            evidence={
                "p1_input_digest": M1_B0_LEDGER,
                "p2_config_digest": M1_POLICY_DIGEST,
                "p3_evidence_id": M1_SUMMARY,
                "strategy_revision": UES_REVISION,
                "producer_revision": UES_REVISION,
            },
        )
        self.assertEqual(task["experiment"]["objective"], "diagnose_degradation")
        self.assertEqual(task["experiment"]["hypothesis"], P3_HYPOTHESIS)
        accepted = validate_strategy_diagnosis_task(task)
        self.assertEqual(accepted["evidence"]["p3_evidence_id"], M1_SUMMARY)
        qrs_accepted = _qrs_validate()(task)
        self.assertEqual(qrs_accepted["evidence"]["p3_evidence_id"], M1_SUMMARY)

        relabeled = copy.deepcopy(task)
        relabeled["experiment"]["hypothesis"] = "M1 development summary is a native P3 observation."
        relabeled["task_sha256"] = calculate_task_sha256(relabeled)
        with self.assertRaises(ResearchTaskError):
            validate_strategy_diagnosis_task(relabeled)

        projected = map_research_result(_seal(_m1_fields()), _seal(_m1_fields()))
        self.assertNotIn("task", projected)
        self.assertNotIn("p3_evidence_id", projected)

    def test_missing_numeric_result_is_rejected(self) -> None:
        fields = _m1_fields()
        fields.pop("result")
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(fields, _seal(_m1_fields()))
        self.assertEqual(caught.exception.code, "missing_result")

    def test_drawdown_metric_name_is_not_mistaken_for_raw_data(self) -> None:
        sealed = _seal(
            _m1_fields(
                summary_digest="ab" * 32,
                result={"max_drawdown": -0.1, "session_count": 856},
            )
        )
        projected = map_research_result(sealed, sealed)
        self.assertEqual(projected["status"], "incompatible")

    def test_wrong_candidate_study_and_revision_are_rejected(self) -> None:
        expected = _seal(_m1_fields())
        cases = (
            ("candidate_mismatch", {"candidate_id": "other_candidate"}),
            ("study_mismatch", {"study_id": "other_study"}),
            ("revision_mismatch", {"strategy_revision": AAB_BASE}),
        )
        for code, overrides in cases:
            with self.subTest(code=code):
                sealed = _seal(_m1_fields(**overrides))
                with self.assertRaises(ResearchResultHandoffError) as caught:
                    map_research_result(sealed, expected)
                self.assertEqual(caught.exception.code, code)

    def test_head_cannot_replace_an_uncommitted_revision(self) -> None:
        fields = _m1_fields(producer_revision="HEAD")
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(fields, _seal(_m1_fields()))
        self.assertEqual(caught.exception.code, "uncommitted_revision")

    def test_tampered_summary_is_rejected(self) -> None:
        sealed = _seal(_m1_fields())
        sealed["summary_digest"] = R9_SUMMARY
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(sealed, _seal(_m1_fields()))
        self.assertEqual(caught.exception.code, "tampered_digest")

    def test_expected_binding_must_also_have_a_valid_seal(self) -> None:
        expected = _seal(_m1_fields())
        expected["result_digest"] = R9_SUMMARY
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(_seal(_m1_fields()), expected)
        self.assertEqual(caught.exception.code, "tampered_digest")

    def test_unsigned_extra_field_is_rejected(self) -> None:
        sealed = _seal(_m1_fields())
        sealed["comment"] = "not covered by the canonical binding"
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(sealed, _seal(_m1_fields()))
        self.assertEqual(caught.exception.code, "unexpected_field")

    def test_wrong_settlement_cost_and_policy_are_rejected(self) -> None:
        expected = _seal(_m1_fields())
        cases = (
            ("settlement_mismatch", {"settlement_policy_digest": "ab" * 32}),
            ("cost_mismatch", {"cost_contract": {"contract_id": "flat_15bps", "cost_bps": 15}}),
            ("policy_mismatch", {"policy_id": "other_policy"}),
        )
        for code, overrides in cases:
            with self.subTest(code=code):
                sealed = _seal(_m1_fields(**overrides))
                with self.assertRaises(ResearchResultHandoffError) as caught:
                    map_research_result(sealed, expected)
                self.assertEqual(caught.exception.code, code)

        negative_cost = _seal(
            _m1_fields(cost_contract={"contract_id": "flat_10bps", "cost_bps": -10})
        )
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(negative_cost, negative_cost)
        self.assertEqual(caught.exception.code, "cost_mismatch")

    def test_published_m1_summary_cannot_be_rebound(self) -> None:
        fields = _m1_fields(
            inputs=[
                {"name": "b0_ledger", "digest": "cd" * 32},
                {"name": "dynamic_ledger", "digest": M1_DYNAMIC_LEDGER},
            ]
        )
        sealed = _seal(fields)
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(sealed, sealed)
        self.assertEqual(caught.exception.code, "input_mismatch")

    def test_permission_upgrade_and_non_development_are_rejected(self) -> None:
        upgraded = _seal(_m1_fields(authority={"no_order": False, "research_only": True, "trade": True}))
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(upgraded, upgraded)
        self.assertEqual(caught.exception.code, "permission_upgrade")

        live = _seal(_m1_fields(stage="live", source_assurance="production"))
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(live, live)
        self.assertEqual(caught.exception.code, "not_development")

    def test_raw_digest_and_unsorted_inputs_are_not_a_complete_index(self) -> None:
        raw = _m1_fields()
        raw["raw_digest"] = M1_B0_LEDGER
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(raw, _seal(_m1_fields()))
        self.assertEqual(caught.exception.code, "incomplete_inputs")

        reversed_inputs = list(reversed(_m1_fields()["inputs"]))
        sealed = _seal(_m1_fields(inputs=reversed_inputs))
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(sealed, sealed)
        self.assertEqual(caught.exception.code, "incomplete_inputs")

    def test_repeat_calls_match_and_do_not_invoke_callbacks(self) -> None:
        sealed = _seal(_m1_fields())
        ai, experiment, notify = _Counter(), _Counter(), _Counter()
        kwargs = {"on_ai": ai, "on_experiment": experiment, "on_notify": notify}
        first = map_research_result(sealed, sealed, **kwargs)
        second = map_research_result(sealed, sealed, **kwargs)
        self.assertEqual(first, second)
        self.assertEqual((ai.calls, experiment.calls, notify.calls), (0, 0, 0))

    def test_status_signals_and_signed_returns_do_not_authorize_action(self) -> None:
        for net_return in (0.25, -0.4):
            with self.subTest(net_return=net_return):
                sealed = _seal(
                    _m1_fields(
                        signals={
                            "astra_go": True,
                            "ci_green": True,
                            "job_done": True,
                            "net_return": net_return,
                            "p1_accepted": True,
                        }
                    )
                )
                projected = map_research_result(sealed, sealed)
                self.assertEqual(projected["status"], "incompatible")
                self.assertNotIn("task", projected)
                self.assertIs(projected["adoption"], False)
                self.assertIs(projected["trade"], False)
                self.assertIs(projected["repair_to_profit"], False)
                self.assertEqual(projected["stage"], "development")
                self.assertEqual(projected["source_assurance"], "development")

    def test_projection_omits_private_fields(self) -> None:
        projected = map_research_result(_seal(_m1_fields()), _seal(_m1_fields()))
        for key in _walk_keys(projected):
            lowered = str(key).lower()
            self.assertFalse(any(token in lowered for token in _FORBIDDEN_KEYS), key)
        blob = json.dumps(projected)
        for token in ("gs://", "https://", "http://", "/Users/"):
            self.assertNotIn(token, blob)

        dirty = _m1_fields()
        dirty["credential"] = "do-not-project"
        dirty["daily_positions"] = [{"path": "/tmp/private"}]
        with self.assertRaises(ResearchResultHandoffError) as caught:
            map_research_result(dirty, _seal(_m1_fields()))
        self.assertEqual(caught.exception.code, "forbidden_field")


if __name__ == "__main__":
    unittest.main()
