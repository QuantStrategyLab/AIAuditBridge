"""Produce and validate a read-only review message for one fixed A aggregate."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "qsl.development_research_review.v1"
EVIDENCE_KIND = "development_research_review"
A_SUMMARY_PATH = Path(
    "/Users/lisiyi/Projects/.private-research/post-r9-batch2-20260927-001/A/summary.json"
)
A_SUMMARY_SHA256 = "7c4a2dcf03c2becb19c012e015f86c5a1f5b6f845afd7d88516a2c515462da91"
_IDENTITY = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEY = re.compile(r"(?:^|_)(?:uri|path|raw|account|credential|personal)(?:_|$)", re.IGNORECASE)
_AUTHORITY = {
    "review_disposition": "advisory",
    "mode": "read_only",
    "no_order": True,
    "adoption": False,
    "codegen": False,
    "experiment": False,
    "notification": False,
    "trade": False,
}
_METRICS = (
    "cagr_252_sessions",
    "cumulative_return",
    "max_drawdown",
    "total_fees_usd",
)
_SCALES = ("1000", "10000", "100000")
_VARIANTS = ("C0", "C1", "C2")


class DevelopmentResearchReviewError(ValueError):
    """Raised when the fixed development summary or message is invalid."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise DevelopmentResearchReviewError("invalid_json_value") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _producer_source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DevelopmentResearchReviewError(code)
    return value


def _identity(value: object, code: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise DevelopmentResearchReviewError(code)
    return value


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_FORBIDDEN_KEY.search(str(key)) or _contains_forbidden_key(nested) for key, nested in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _finite_numeric_tree(value: object) -> bool:
    if isinstance(value, Mapping):
        return bool(value) and all(_finite_numeric_tree(nested) for nested in value.values())
    if isinstance(value, list):
        return bool(value) and all(_finite_numeric_tree(nested) for nested in value)
    return _number(value)


def _required_digest(summary: Mapping[str, Any], field: str) -> str:
    value = summary.get(field)
    if value is None:
        raise DevelopmentResearchReviewError("missing_upstream_input")
    return _digest(value, "invalid_upstream_input")


def _input_index(summary: Mapping[str, Any]) -> list[dict[str, str]]:
    index: list[dict[str, str]] = [
        {"name": "source_manifest", "sha256": _required_digest(summary, "raw_manifest_sha256")},
        {"name": "r6_manifest", "sha256": _required_digest(summary, "r6_manifest_sha256")},
        {"name": "r6_materialized_file", "sha256": _required_digest(summary, "r6_materialized_file_sha256")},
    ]
    validation = summary.get("input_validation")
    if not isinstance(validation, Mapping):
        raise DevelopmentResearchReviewError("missing_upstream_input")
    index.append({"name": "future_manifest", "sha256": _required_digest(validation, "future_manifest_sha256")})
    pages = validation.get("future_page_sha256")
    if not isinstance(pages, Mapping) or len(pages) != 12:
        raise DevelopmentResearchReviewError("missing_upstream_input")
    for name, digest in sorted(pages.items()):
        safe_name = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
        index.append({"name": _identity(f"future_page_{safe_name}", "invalid_upstream_input"),
                      "sha256": _digest(digest, "invalid_upstream_input")})
    index.extend(
        [
            {"name": "license_basis_record", "sha256": _required_digest(validation, "license_basis_record_sha256")},
            {"name": "r7_engine", "sha256": _required_digest(summary, "r7_engine_sha256")},
            {"name": "r7_policy", "sha256": _required_digest(summary, "r7_policy_sha256")},
            {"name": "r8_engine", "sha256": _required_digest(summary, "r8_engine_sha256")},
            {"name": "r8_policy", "sha256": _required_digest(summary, "r8_policy_sha256")},
            {"name": "capital_policy", "sha256": _required_digest(summary, "policy_sha256")},
            {"name": "settlement_policy", "sha256": _required_digest(summary, "settlement_policy_sha256")},
            {"name": "research_runner", "sha256": _required_digest(summary, "runner_sha256")},
            {"name": "tqqq_contract", "sha256": _required_digest(summary, "tqqq_contract_sha256")},
        ]
    )
    names = [item["name"] for item in index]
    if len(names) != len(set(names)):
        raise DevelopmentResearchReviewError("invalid_upstream_input")
    return index


def validate_summary(summary: object) -> dict[str, Any]:
    """Check the complete, known A aggregate shape without trusting caller labels."""
    if not isinstance(summary, Mapping):
        raise DevelopmentResearchReviewError("invalid_summary")
    if summary.get("schema") != "qsl.research.post_r9_capital_study.v1":
        raise DevelopmentResearchReviewError("unsupported_source_summary")
    if summary.get("candidate_id") != "r8_finite_action_joint_account_60session_b0_startup_development_v2":
        raise DevelopmentResearchReviewError("candidate_identity_mismatch")
    if summary.get("development") is not True or summary.get("research_only") is not True:
        raise DevelopmentResearchReviewError("not_development_research")
    if any(summary.get(flag) is not False for flag in ("paper_authorized", "shadow_authorized", "live_authorized")):
        raise DevelopmentResearchReviewError("permission_upgrade")
    if summary.get("real_contribution_plan_supported") is not False:
        raise DevelopmentResearchReviewError("permission_upgrade")
    if summary.get("source_assurance") != "single_source_structural_only_no_cross_provider_verification":
        raise DevelopmentResearchReviewError("source_assurance_upgrade")
    if summary.get("strict_point_in_time_certified") is not False:
        raise DevelopmentResearchReviewError("pit_upgrade")
    if summary.get("curve_superiority_assumed") is not False:
        raise DevelopmentResearchReviewError("unsupported_superiority_claim")
    if summary.get("recovery") != {"short_history_checks": 2, "status": "matched"}:
        raise DevelopmentResearchReviewError("incomplete_aggregate")

    scales = summary.get("scales_usd")
    variants = summary.get("variants")
    if scales != [1000, 10000, 100000] or variants != list(_VARIANTS):
        raise DevelopmentResearchReviewError("incomplete_aggregate")
    if isinstance(summary.get("sessions"), bool) or summary.get("sessions") != 856:
        raise DevelopmentResearchReviewError("incomplete_aggregate")
    input_validation = summary.get("input_validation")
    if not isinstance(input_validation, Mapping):
        raise DevelopmentResearchReviewError("missing_upstream_input")
    for field in ("first_extension_session", "last_extension_session"):
        if not isinstance(input_validation.get(field), str) or not input_validation[field]:
            raise DevelopmentResearchReviewError("missing_upstream_input")
    if input_validation.get("future_session_count") != 412:
        raise DevelopmentResearchReviewError("incomplete_aggregate")
    paths = summary.get("paths")
    comparisons = summary.get("comparisons")
    if not isinstance(paths, Mapping) or set(paths) != set(_SCALES):
        raise DevelopmentResearchReviewError("incomplete_aggregate")
    if not isinstance(comparisons, Mapping) or set(comparisons) != set(_SCALES):
        raise DevelopmentResearchReviewError("incomplete_aggregate")
    for scale in _SCALES:
        scale_paths = paths[scale]
        if not isinstance(scale_paths, Mapping) or set(scale_paths) != set(_VARIANTS):
            raise DevelopmentResearchReviewError("incomplete_aggregate")
        for variant in _VARIANTS:
            path_result = scale_paths[variant]
            if not isinstance(path_result, Mapping) or any(not _number(path_result.get(key)) for key in _METRICS):
                raise DevelopmentResearchReviewError("incomplete_aggregate")
        scale_comparisons = comparisons[scale]
        if not isinstance(scale_comparisons, Mapping) or set(scale_comparisons) != {"C1_minus_C0", "C2_minus_C1"}:
            raise DevelopmentResearchReviewError("incomplete_aggregate")
        for comparison in scale_comparisons.values():
            if not isinstance(comparison, Mapping) or not _finite_numeric_tree(comparison):
                raise DevelopmentResearchReviewError("incomplete_aggregate")
    _input_index(summary)
    return copy.deepcopy(dict(summary))


def _normalized_result(summary: Mapping[str, Any]) -> dict[str, Any]:
    paths = {
        scale: {
            variant: {metric: summary["paths"][scale][variant][metric] for metric in _METRICS}
            for variant in _VARIANTS
        }
        for scale in _SCALES
    }
    comparisons = {
        scale: {
            comparison: dict(summary["comparisons"][scale][comparison])
            for comparison in ("C1_minus_C0", "C2_minus_C1")
        }
        for scale in _SCALES
    }
    return {
        "session_count": summary["sessions"],
        "scales_usd": list(summary["scales_usd"]),
        "variants": list(summary["variants"]),
        "paths": paths,
        "comparisons": comparisons,
    }


def _validate_normalized_result(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {"session_count", "scales_usd", "variants", "paths", "comparisons"}:
        raise DevelopmentResearchReviewError("invalid_result")
    if value["session_count"] != 856 or value["scales_usd"] != [1000, 10000, 100000] or value["variants"] != list(_VARIANTS):
        raise DevelopmentResearchReviewError("invalid_result")
    paths, comparisons = value["paths"], value["comparisons"]
    if not isinstance(paths, Mapping) or set(paths) != set(_SCALES):
        raise DevelopmentResearchReviewError("invalid_result")
    if not isinstance(comparisons, Mapping) or set(comparisons) != set(_SCALES):
        raise DevelopmentResearchReviewError("invalid_result")
    for scale in _SCALES:
        scale_paths = paths[scale]
        if not isinstance(scale_paths, Mapping) or set(scale_paths) != set(_VARIANTS):
            raise DevelopmentResearchReviewError("invalid_result")
        for variant in _VARIANTS:
            metrics = scale_paths[variant]
            if not isinstance(metrics, Mapping) or set(metrics) != set(_METRICS) or not all(_number(number) for number in metrics.values()):
                raise DevelopmentResearchReviewError("invalid_result")
        scale_comparisons = comparisons[scale]
        if not isinstance(scale_comparisons, Mapping) or set(scale_comparisons) != {"C1_minus_C0", "C2_minus_C1"}:
            raise DevelopmentResearchReviewError("invalid_result")
        if any(not isinstance(item, Mapping) or not item or not all(_number(number) for number in item.values())
               for item in scale_comparisons.values()):
            raise DevelopmentResearchReviewError("invalid_result")
def _economic_identity(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": message["schema"],
        "identities": message["identities"],
        "upstream_input_index_sha256": _sha256(message["upstream_input_index"]),
        "policy_id": message["identities"]["policy_id"],
        "policy_sha256": message["identities"]["policy_sha256"],
        "settlement_policy_id": message["identities"]["settlement_policy_id"],
        "settlement_policy_sha256": message["identities"]["settlement_policy_sha256"],
        "cost_id": message["identities"]["cost_id"],
        "message_version": message["message_version"],
    }


def create_message(source_summary_bytes: bytes) -> dict[str, Any]:
    """Create a message only from the exact frozen A summary bytes."""
    if not isinstance(source_summary_bytes, bytes):
        raise DevelopmentResearchReviewError("invalid_source_summary_bytes")
    digest = hashlib.sha256(source_summary_bytes).hexdigest()
    if digest != A_SUMMARY_SHA256:
        raise DevelopmentResearchReviewError("source_summary_digest_mismatch")
    try:
        summary = json.loads(source_summary_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DevelopmentResearchReviewError("invalid_source_summary_json") from exc
    source = validate_summary(summary)
    input_index = _input_index(source)
    result = _normalized_result(source)
    result_digest = _sha256(result)
    candidate_id = source["candidate_id"]
    identities = {
        "study_id": "post_r9_capital_study",
        "candidate_id": candidate_id,
        "portfolio_id": candidate_id,
        "strategy_id": candidate_id,
        "strategy_revision_sha256": _required_digest(source, "r8_engine_sha256"),
        "producer_id": "aiauditbridge",
        "producer_revision_sha256": _producer_source_sha256(),
        "runner_id": _required_digest(source, "runner_sha256"),
        "policy_id": _identity(source["policy_id"], "invalid_policy_identity"),
        "policy_sha256": _required_digest(source, "policy_sha256"),
        "settlement_policy_id": _identity(source["settlement_policy_id"], "invalid_settlement_identity"),
        "settlement_policy_sha256": _required_digest(source, "settlement_policy_sha256"),
        "cost_id": f"flat_{source['cost_bps']}bps",
        "cost_bps": source["cost_bps"],
    }
    message: dict[str, Any] = {
        "schema": SCHEMA,
        "evidence_kind": EVIDENCE_KIND,
        "message_version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "identities": identities,
        "provenance": {
            "source_summary_schema": source["schema"],
            "source_summary_bytes_sha256": digest,
            "upstream_input_index_sha256": _sha256(input_index),
            "normalized_result_sha256": result_digest,
        },
        "upstream_input_index": input_index,
        "source_extension_window": {
            "first_session": source["input_validation"]["first_extension_session"],
            "last_session": source["input_validation"]["last_extension_session"],
            "future_session_count": source["input_validation"]["future_session_count"],
        },
        "session_count": source["sessions"],
        "capital_scales_usd": list(source["scales_usd"]),
        "result": result,
        "result_digest": result_digest,
        "completion": {"status": "complete", "aggregate_recovery": "matched"},
        "research_stage": "development",
        "source_assurance": source["source_assurance"],
        "strict_point_in_time_certified": False,
        "limitations": [
            "single_source_structural_only_no_cross_provider_verification",
            "corporate_action_process_date_is_a_retrospective_proxy",
            "capital_curve_is_pre_specified_not_optimized",
            "no_paper_shadow_live_deployment_account_or_trading_authority",
        ],
        "authority": dict(_AUTHORITY),
    }
    message["duplicate_key"] = _sha256(_economic_identity(message))
    message["message_sha256"] = _sha256(message)
    return message


def build_message_from_file(summary_path: Path | str = A_SUMMARY_PATH) -> dict[str, Any]:
    """Read and digest-check the exact fixed A summary bytes before parsing."""
    try:
        raw = Path(summary_path).read_bytes()
    except OSError as exc:
        raise DevelopmentResearchReviewError("source_summary_unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != A_SUMMARY_SHA256:
        raise DevelopmentResearchReviewError("source_summary_digest_mismatch")
    return create_message(raw)


def validate_development_research_review(payload: object) -> dict[str, Any]:
    """Validate a message seal, authority, input index, and economic key."""
    if not isinstance(payload, Mapping) or _contains_forbidden_key(payload):
        raise DevelopmentResearchReviewError("forbidden_field")
    allowed = {
        "schema", "evidence_kind", "message_version", "created_at", "identities", "provenance",
        "upstream_input_index", "source_extension_window", "session_count", "capital_scales_usd",
        "result", "result_digest", "completion", "research_stage", "source_assurance",
        "strict_point_in_time_certified", "limitations", "authority", "duplicate_key", "message_sha256",
    }
    if set(payload) != allowed:
        raise DevelopmentResearchReviewError("unexpected_or_missing_field")
    message = copy.deepcopy(dict(payload))
    if message["schema"] != SCHEMA or message["evidence_kind"] != EVIDENCE_KIND or message["message_version"] != 1:
        raise DevelopmentResearchReviewError("unsupported_review_schema")
    if not isinstance(message["created_at"], str) or re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", message["created_at"]
    ) is None:
        raise DevelopmentResearchReviewError("invalid_created_at")
    try:
        datetime.fromisoformat(message["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise DevelopmentResearchReviewError("invalid_created_at") from exc
    if message["research_stage"] != "development":
        raise DevelopmentResearchReviewError("research_stage_upgrade")
    if message["source_assurance"] != "single_source_structural_only_no_cross_provider_verification":
        raise DevelopmentResearchReviewError("source_assurance_upgrade")
    if message["strict_point_in_time_certified"] is not False:
        raise DevelopmentResearchReviewError("pit_upgrade")
    if message["authority"] != _AUTHORITY:
        raise DevelopmentResearchReviewError("permission_upgrade")
    if message["completion"] != {"status": "complete", "aggregate_recovery": "matched"}:
        raise DevelopmentResearchReviewError("incomplete_aggregate")
    expected_limitations = [
        "single_source_structural_only_no_cross_provider_verification",
        "corporate_action_process_date_is_a_retrospective_proxy",
        "capital_curve_is_pre_specified_not_optimized",
        "no_paper_shadow_live_deployment_account_or_trading_authority",
    ]
    if message["limitations"] != expected_limitations:
        raise DevelopmentResearchReviewError("missing_limitations")
    identities = message["identities"]
    if not isinstance(identities, Mapping) or set(identities) != {
        "study_id", "candidate_id", "portfolio_id", "strategy_id", "strategy_revision_sha256",
        "producer_id", "producer_revision_sha256", "runner_id", "policy_id", "policy_sha256",
        "settlement_policy_id", "settlement_policy_sha256", "cost_id", "cost_bps",
    }:
        raise DevelopmentResearchReviewError("invalid_identity")
    if (identities["study_id"] != "post_r9_capital_study"
            or identities["candidate_id"] != "r8_finite_action_joint_account_60session_b0_startup_development_v2"
            or identities["portfolio_id"] != identities["candidate_id"]
            or identities["strategy_id"] != identities["candidate_id"]
            or identities["producer_id"] != "aiauditbridge"):
        raise DevelopmentResearchReviewError("invalid_identity")
    for field in ("strategy_revision_sha256", "producer_revision_sha256", "runner_id", "policy_sha256", "settlement_policy_sha256"):
        _digest(identities[field], "invalid_identity")
    if identities["producer_revision_sha256"] != _producer_source_sha256():
        raise DevelopmentResearchReviewError("producer_revision_mismatch")
    for field in ("policy_id", "settlement_policy_id", "cost_id"):
        _identity(identities[field], "invalid_identity")
    if identities["cost_bps"] != 10 or identities["cost_id"] != "flat_10bps":
        raise DevelopmentResearchReviewError("invalid_identity")
    provenance = message["provenance"]
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "source_summary_schema", "source_summary_bytes_sha256", "upstream_input_index_sha256", "normalized_result_sha256"
    }:
        raise DevelopmentResearchReviewError("invalid_provenance")
    if provenance["source_summary_schema"] != "qsl.research.post_r9_capital_study.v1":
        raise DevelopmentResearchReviewError("unsupported_source_summary")
    source_digest = _digest(provenance.get("source_summary_bytes_sha256"), "source_summary_digest_mismatch")
    if source_digest != A_SUMMARY_SHA256:
        raise DevelopmentResearchReviewError("source_summary_digest_mismatch")
    _digest(message["result_digest"], "result_digest_mismatch")
    if _sha256(message["result"]) != message["result_digest"]:
        raise DevelopmentResearchReviewError("result_digest_mismatch")
    if provenance.get("normalized_result_sha256") != message["result_digest"]:
        raise DevelopmentResearchReviewError("result_digest_mismatch")
    if provenance.get("upstream_input_index_sha256") != _sha256(message["upstream_input_index"]):
        raise DevelopmentResearchReviewError("input_index_digest_mismatch")
    index = message["upstream_input_index"]
    if not isinstance(index, list) or len(index) != 25:
        raise DevelopmentResearchReviewError("missing_upstream_input")
    names: list[str] = []
    for item in index:
        if not isinstance(item, Mapping) or set(item) != {"name", "sha256"}:
            raise DevelopmentResearchReviewError("invalid_upstream_input")
        names.append(_identity(item["name"], "invalid_upstream_input"))
        _digest(item["sha256"], "invalid_upstream_input")
    if len(set(names)) != 25:
        raise DevelopmentResearchReviewError("invalid_upstream_input")
    if message["session_count"] != 856 or message["capital_scales_usd"] != [1000, 10000, 100000]:
        raise DevelopmentResearchReviewError("incomplete_aggregate")
    _validate_normalized_result(message["result"])
    window = message["source_extension_window"]
    if (not isinstance(window, Mapping)
            or set(window) != {"first_session", "last_session", "future_session_count"}
            or window != {"first_session": "2025-01-02", "last_session": "2026-08-25", "future_session_count": 412}):
        raise DevelopmentResearchReviewError("invalid_window")
    _digest(message["duplicate_key"], "duplicate_key_mismatch")
    if message["duplicate_key"] != _sha256(_economic_identity(message)):
        raise DevelopmentResearchReviewError("duplicate_key_mismatch")
    expected_message_digest = message.pop("message_sha256")
    _digest(expected_message_digest, "message_digest_mismatch")
    if _sha256(message) != expected_message_digest:
        raise DevelopmentResearchReviewError("message_digest_mismatch")
    message["message_sha256"] = expected_message_digest
    return message


class DuplicateReviewRegistry:
    """In-memory idempotency check for repeat local reads in one process."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def record(self, payload: object) -> dict[str, str]:
        message = validate_development_research_review(payload)
        key = message["duplicate_key"]
        digest = message["result_digest"]
        previous = self._seen.get(key)
        if previous is not None and previous != digest:
            raise DevelopmentResearchReviewError("duplicate_key_conflict")
        self._seen[key] = digest
        return {"duplicate_key": key, "result_digest": digest, "status": "duplicate" if previous else "accepted"}


__all__ = [
    "A_SUMMARY_PATH", "A_SUMMARY_SHA256", "DuplicateReviewRegistry",
    "DevelopmentResearchReviewError", "build_message_from_file", "canonical_json", "create_message",
    "validate_development_research_review", "validate_summary",
]
