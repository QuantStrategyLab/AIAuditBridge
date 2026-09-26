"""Map one sanitized aggregate research result onto the research-task consumer.

The helper checks supplied bindings and canonical digests. It does not emit
``qsl.research_task.v1``. Development summaries, including the published M1 and
R9 digests, are not native P3 evidence, even when a 64-character SHA-256 would
pass the structural task validators.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Mapping

from service.research_task import ResearchTaskError, canonical_json


AAB_CONSUMER_BASE = "d47a78d0c538e790310a610298842e5cf3db3c16"
UES_M1_STRATEGY_REVISION = "1c4a1c3118d4d482bdb7191f9b7cda40cd4955cf"
M1_POLICY_ID = "post_r9_us_equity_dtc_standard_settlement_v1"
M1_POLICY_DIGEST = "c135c023ee7329ad6103021ffbb79d4cdfea01e903ac331865c157a6a1246853"
M1_SUMMARY_DIGEST = "e0e5c2e51836edb246e70cd9ea7f4b64def713928aafd4d6933b403a69f793cc"
M1_B0_LEDGER_DIGEST = "68b96ff510bec653c2286d456b719fa680a7a731debd71b0a1bb27b4c57392ba"
M1_DYNAMIC_LEDGER_DIGEST = "9ab7b28d0fa9a024c389d49d4eaa3f79adae591cd100d80411f125bf03da832e"
R9_SUMMARY_DIGEST = "628de89afde2fad718ae6298370e895585d3c4c082452a5c9044b5abeb2ba95f"
P3_OBJECTIVE = "diagnose_degradation"
P3_HYPOTHESIS = (
    "A verified P3 observation crossed a degradation threshold; diagnose it with "
    "one bounded offline comparison without changing active parameters."
)
AAB_CONSUMER = "AIAuditBridge.service.research_task.validate_strategy_diagnosis_task"
QRS_CONSUMER = "QuantRuntimeSettings.python.scripts.research_task_contract.validate_research_task"

_IDENTITY = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_FORBIDDEN_KEY = re.compile(
    r"(?:^|_)(?:uri|path|raw|daily|credential|account|personal)(?:_|$)",
    re.IGNORECASE,
)
_AUTHORITY = {"research_only": True, "no_order": True}
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
_PAYLOAD_KEYS = frozenset((*_BODY_KEYS, "aggregate_sha256"))
# This is the published M1 output-evidence set consumed by this helper, not the
# full upstream research-input inventory.
_M1_OUTPUTS = (
    {"name": "b0_ledger", "digest": M1_B0_LEDGER_DIGEST},
    {"name": "dynamic_ledger", "digest": M1_DYNAMIC_LEDGER_DIGEST},
)
_M1_RESULT = {"session_count": 856}
_M1_COST = {"contract_id": "flat_10bps", "cost_bps": 10}


class ResearchResultHandoffError(ValueError):
    """Raised when a sanitized aggregate is not a faithful research binding."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256(value: object) -> str:
    try:
        canonical = canonical_json(value)
    except ResearchTaskError as exc:
        raise ResearchResultHandoffError("missing_result") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, Mapping):
        return key in value or any(_contains_key(nested, key) for nested in value.values())
    if isinstance(value, list):
        return any(_contains_key(nested, key) for nested in value)
    return False


def _reject_forbidden_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _FORBIDDEN_KEY.search(str(key)):
                raise ResearchResultHandoffError("forbidden_field")
            _reject_forbidden_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_keys(nested)


def _has_finite_number(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_has_finite_number(nested) for nested in value.values())
    return False


def _identity(value: object, code: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ResearchResultHandoffError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ResearchResultHandoffError(code)
    return value


def _revision(value: object) -> str:
    if not isinstance(value, str) or value.upper() == "HEAD" or _REVISION.fullmatch(value) is None:
        raise ResearchResultHandoffError("uncommitted_revision")
    return value


def _inputs(value: object) -> list[dict[str, str]]:
    if isinstance(value, str) or not isinstance(value, list) or not value:
        raise ResearchResultHandoffError("incomplete_inputs")
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"name", "digest"}:
            raise ResearchResultHandoffError("incomplete_inputs")
        normalized.append({
            "name": _identity(item["name"], "incomplete_inputs"),
            "digest": _digest(item["digest"], "incomplete_inputs"),
        })
    names = [item["name"] for item in normalized]
    if names != sorted(names) or len(names) != len(set(names)):
        raise ResearchResultHandoffError("incomplete_inputs")
    return normalized


def _cost(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"contract_id", "cost_bps"}:
        raise ResearchResultHandoffError("cost_mismatch")
    cost_bps = value["cost_bps"]
    if isinstance(cost_bps, bool) or not isinstance(cost_bps, int) or not 0 <= cost_bps <= 10_000:
        raise ResearchResultHandoffError("cost_mismatch")
    return {"contract_id": _identity(value["contract_id"], "cost_mismatch"), "cost_bps": cost_bps}


def _authority(value: object) -> dict[str, bool]:
    if value != _AUTHORITY:
        raise ResearchResultHandoffError("permission_upgrade")
    return dict(_AUTHORITY)


def _development(stage: object, source_assurance: object) -> None:
    if stage != "development" or source_assurance != "development":
        raise ResearchResultHandoffError("not_development")


def _require_result(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not _has_finite_number(value):
        raise ResearchResultHandoffError("missing_result")
    return dict(value)


def _aggregate_digest(aggregate: Mapping[str, Any]) -> str:
    body = {key: aggregate[key] for key in _BODY_KEYS if key in aggregate}
    return _sha256(body)


def _normalize(payload: Mapping[str, Any]) -> dict[str, Any]:
    if _contains_key(payload, "raw_digest"):
        raise ResearchResultHandoffError("incomplete_inputs")
    _reject_forbidden_keys(payload)
    if not set(payload).issubset(_PAYLOAD_KEYS):
        raise ResearchResultHandoffError("unexpected_field")
    result = _require_result(payload.get("result"))
    normalized = {
        "portfolio_id": _identity(payload.get("portfolio_id"), "candidate_mismatch"),
        "study_id": _identity(payload.get("study_id"), "study_mismatch"),
        "candidate_id": _identity(payload.get("candidate_id"), "candidate_mismatch"),
        "strategy_revision": _revision(payload.get("strategy_revision")),
        "producer_revision": _revision(payload.get("producer_revision")),
        "inputs": _inputs(payload.get("inputs")),
        "summary_digest": _digest(payload.get("summary_digest"), "tampered_digest"),
        "result": result,
        "policy_id": _identity(payload.get("policy_id"), "policy_mismatch"),
        "policy_digest": _digest(payload.get("policy_digest"), "policy_mismatch"),
        "settlement_policy_id": _identity(payload.get("settlement_policy_id"), "settlement_mismatch"),
        "settlement_policy_digest": _digest(payload.get("settlement_policy_digest"), "settlement_mismatch"),
        "cost_contract": _cost(payload.get("cost_contract")),
        "source_assurance": payload.get("source_assurance"),
        "stage": payload.get("stage"),
        "authority": _authority(payload.get("authority")),
    }
    _development(normalized["stage"], normalized["source_assurance"])
    if "signals" in payload:
        signals = payload["signals"]
        if not isinstance(signals, Mapping):
            raise ResearchResultHandoffError("tampered_digest")
        _reject_forbidden_keys(signals)
        normalized["signals"] = dict(signals)
    return normalized


def _validate_seal(payload: Mapping[str, Any], normalized: Mapping[str, Any]) -> dict[str, Any]:
    result_digest = _sha256(normalized["result"])
    if payload.get("result_digest") != result_digest:
        raise ResearchResultHandoffError("tampered_digest")
    digest_source = dict(payload)
    digest_source.update(normalized)
    if payload.get("aggregate_sha256") != _aggregate_digest(digest_source):
        raise ResearchResultHandoffError("tampered_digest")
    sealed = dict(normalized)
    sealed["result_digest"] = result_digest
    return sealed


def _same(left: Mapping[str, Any], right: Mapping[str, Any], key: str, code: str) -> None:
    if left.get(key) != right.get(key):
        raise ResearchResultHandoffError(code)


def _match_expected(aggregate: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    pairs = (
        ("portfolio_id", "candidate_mismatch"),
        ("candidate_id", "candidate_mismatch"),
        ("study_id", "study_mismatch"),
        ("strategy_revision", "revision_mismatch"),
        ("producer_revision", "revision_mismatch"),
        ("inputs", "input_mismatch"),
        ("summary_digest", "tampered_digest"),
        ("result", "missing_result"),
        ("result_digest", "tampered_digest"),
        ("policy_id", "policy_mismatch"),
        ("policy_digest", "policy_mismatch"),
        ("settlement_policy_id", "settlement_mismatch"),
        ("settlement_policy_digest", "settlement_mismatch"),
        ("cost_contract", "cost_mismatch"),
        ("source_assurance", "not_development"),
        ("stage", "not_development"),
        ("authority", "permission_upgrade"),
        ("signals", "tampered_digest"),
    )
    for key, code in pairs:
        if key in aggregate or key in expected:
            _same(aggregate, expected, key, code)


def _match_published_m1(aggregate: Mapping[str, Any]) -> None:
    if aggregate["summary_digest"] != M1_SUMMARY_DIGEST:
        return
    checks = (
        ("portfolio_id", "post_r9_us_equity", "candidate_mismatch"),
        ("study_id", "m1_settlement_sensitivity", "study_mismatch"),
        ("candidate_id", "post_r9_m1_settlement", "candidate_mismatch"),
        ("strategy_revision", UES_M1_STRATEGY_REVISION, "revision_mismatch"),
        ("policy_id", M1_POLICY_ID, "policy_mismatch"),
        ("policy_digest", M1_POLICY_DIGEST, "policy_mismatch"),
        ("settlement_policy_id", M1_POLICY_ID, "settlement_mismatch"),
        ("settlement_policy_digest", M1_POLICY_DIGEST, "settlement_mismatch"),
        ("inputs", list(_M1_OUTPUTS), "input_mismatch"),
        ("cost_contract", _M1_COST, "cost_mismatch"),
        ("result", _M1_RESULT, "missing_result"),
    )
    for key, expected, code in checks:
        if aggregate.get(key) != expected:
            raise ResearchResultHandoffError(code)


def _projection(aggregate: Mapping[str, Any]) -> dict[str, Any]:
    summary = aggregate["summary_digest"]
    if summary == M1_SUMMARY_DIGEST:
        identity = f"M1 authoritative summary {M1_SUMMARY_DIGEST}"
    elif summary == R9_SUMMARY_DIGEST:
        identity = f"R9 summary {R9_SUMMARY_DIGEST}"
    else:
        identity = f"development summary {summary}"
    shared = (
        f"{identity} is a development research digest, not a native P3 evidence id. "
        "M1/R9/A development summaries are not that verified P3 observation. "
        f"Published public digests remain {M1_SUMMARY_DIGEST} and {R9_SUMMARY_DIGEST}. "
        "The summary SHA is not written into evidence.p3_evidence_id, and no "
        "qsl.research_task.v1 task is emitted."
    )
    return {
        "status": "incompatible",
        "disposition": "advisory",
        "research_only": True,
        "no_order": True,
        "stage": aggregate["stage"],
        "source_assurance": aggregate["source_assurance"],
        "adoption": False,
        "trade": False,
        "repair_to_profit": False,
        "consumer_base": AAB_CONSUMER_BASE,
        "portfolio_id": aggregate["portfolio_id"],
        "study_id": aggregate["study_id"],
        "candidate_id": aggregate["candidate_id"],
        "strategy_revision": aggregate["strategy_revision"],
        "producer_revision": aggregate["producer_revision"],
        "summary_digest": summary,
        "policy_id": aggregate["policy_id"],
        "policy_digest": aggregate["policy_digest"],
        "settlement_policy_id": aggregate["settlement_policy_id"],
        "settlement_policy_digest": aggregate["settlement_policy_digest"],
        "cost_contract": dict(aggregate["cost_contract"]),
        "input_index": [dict(item) for item in aggregate["inputs"]],
        "incompatibilities": [
            {
                "field": "evidence.p3_evidence_id",
                "consumer": AAB_CONSUMER,
                "reason": (
                    f"At consumer base {AAB_CONSUMER_BASE}, validate_strategy_diagnosis_task "
                    f"requires objective={P3_OBJECTIVE} and hypothesis exactly: {P3_HYPOTHESIS} "
                    f"{shared}"
                ),
            },
            {
                "field": "evidence.p3_evidence_id",
                "consumer": QRS_CONSUMER,
                "reason": (
                    "validate_research_task and qsl.research_task.v1 accept any 64-character "
                    "lowercase SHA-256 or null in evidence.p3_evidence_id. Structural acceptance "
                    f"does not make a development summary native P3 evidence. {shared}"
                ),
            },
        ],
    }


def map_research_result(
    aggregate: object,
    expected_binding: object,
    *,
    on_ai: object = None,
    on_experiment: object = None,
    on_notify: object = None,
) -> dict[str, Any]:
    """Validate one sanitized aggregate and return an advisory incompatible projection.

    ``on_ai``, ``on_experiment``, and ``on_notify`` are accepted so callers can
    observe that this mapping performs no AI, experiment, or notification work.
    They are never called.
    """
    del on_ai, on_experiment, on_notify
    if not isinstance(aggregate, Mapping) or not isinstance(expected_binding, Mapping):
        raise ResearchResultHandoffError("missing_result")
    normalized = _validate_seal(aggregate, _normalize(aggregate))
    normalized_expected = _validate_seal(expected_binding, _normalize(expected_binding))
    _match_expected(normalized, normalized_expected)
    _match_published_m1(normalized)
    return _projection(normalized)


__all__ = [
    "AAB_CONSUMER",
    "AAB_CONSUMER_BASE",
    "M1_SUMMARY_DIGEST",
    "P3_HYPOTHESIS",
    "P3_OBJECTIVE",
    "QRS_CONSUMER",
    "R9_SUMMARY_DIGEST",
    "ResearchResultHandoffError",
    "map_research_result",
]
