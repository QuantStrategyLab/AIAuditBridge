"""Tamper-evident provenance receipts for analyze and review outputs."""

from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Mapping

SCHEMA_VERSION = "ai_provenance_receipt.v1"
REVIEW_VERDICTS = frozenset({
    "agree",
    "approve",
    "data_insufficient",
    "escalate",
    "mismatch",
    "reject",
    "review",
    "verified",
})
ELIGIBLE_REVIEW_VERDICTS = frozenset({"agree", "approve", "verified"})


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _review_evaluation(output: str) -> tuple[str, str]:
    try:
        payload = json.loads(output)
        if not isinstance(payload, dict):
            raise ValueError("review output must be a JSON object")
        verdict = str(payload.get("verdict") or "").strip().lower()
        confidence = payload.get("confidence")
        if verdict not in REVIEW_VERDICTS:
            raise ValueError("review verdict is unsupported")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("review confidence must be numeric")
        if not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("review confidence is out of range")
        return "verified", verdict
    except (TypeError, ValueError, json.JSONDecodeError):
        return "unavailable", "unavailable"


@dataclass(frozen=True)
class AiProvenanceReceipt:
    schema_version: str
    operation: str
    requested_provider: str
    requested_model: str
    actual_provider: str
    actual_model: str
    identity_verdict: str
    input_sha256: str
    output_sha256: str
    evaluation_status: str
    evaluation_verdict: str
    policy_verdict: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["receipt_sha256"] = _sha256(_canonical_json(payload))
        return payload


def build_provenance_receipt(
    *,
    operation: str,
    requested_provider: str,
    requested_model: str,
    actual_provider: str,
    actual_model: str,
    system: str,
    user: str,
    output: str,
) -> AiProvenanceReceipt:
    operation = operation.strip().lower()
    if operation not in {"analyze", "review"}:
        raise ValueError("operation must be analyze or review")

    requested_provider = requested_provider.strip().lower()
    requested_model = requested_model.strip()
    actual_provider = actual_provider.strip().lower()
    actual_model = actual_model.strip()
    if not all((requested_provider, requested_model, actual_provider, actual_model)):
        identity_verdict = "unavailable"
    elif (requested_provider, requested_model) == (actual_provider, actual_model):
        identity_verdict = "verified"
    else:
        identity_verdict = "mismatch"

    if operation == "review":
        evaluation_status, evaluation_verdict = _review_evaluation(output)
    else:
        evaluation_status, evaluation_verdict = "unavailable", "unavailable"

    policy_verdict = (
        "eligible"
        if (
            identity_verdict == "verified"
            and evaluation_status == "verified"
            and evaluation_verdict in ELIGIBLE_REVIEW_VERDICTS
        )
        else "advisory"
    )
    input_bytes = _canonical_json({"system": system, "user": user})
    return AiProvenanceReceipt(
        schema_version=SCHEMA_VERSION,
        operation=operation,
        requested_provider=requested_provider,
        requested_model=requested_model,
        actual_provider=actual_provider,
        actual_model=actual_model,
        identity_verdict=identity_verdict,
        input_sha256=_sha256(input_bytes),
        output_sha256=_sha256(output.encode("utf-8")),
        evaluation_status=evaluation_status,
        evaluation_verdict=evaluation_verdict,
        policy_verdict=policy_verdict,
    )


def verify_receipt(
    receipt: Mapping[str, Any],
    *,
    system: str | None = None,
    user: str | None = None,
    output: str | None = None,
) -> bool:
    try:
        expected = str(receipt.get("receipt_sha256") or "")
        payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        actual = _sha256(_canonical_json(payload))
        if len(expected) != 64 or not hmac.compare_digest(expected, actual):
            return False
        if system is not None or user is not None:
            if system is None or user is None:
                return False
            input_sha256 = _sha256(_canonical_json({"system": system, "user": user}))
            if not hmac.compare_digest(str(receipt.get("input_sha256") or ""), input_sha256):
                return False
        if output is not None:
            output_sha256 = _sha256(output.encode("utf-8"))
            if not hmac.compare_digest(str(receipt.get("output_sha256") or ""), output_sha256):
                return False
        return True
    except (TypeError, ValueError):
        return False


def fail_closed_review_action(action: Mapping[str, Any]) -> dict[str, Any]:
    """Remove autonomous PR/merge authority from an unverified review."""
    safe = deepcopy(dict(action))
    reason = "AI provenance or evaluation is unavailable; advisory only"
    safe.update({
        "action": "escalate",
        "initial_action": "escalate",
        "human_review_required": True,
        "auto_merge_allowed": False,
        "provenance_policy_verdict": "advisory",
        "reason": reason,
    })
    authority = safe.get("automation_authority")
    if isinstance(authority, dict):
        authority.update({
            "authority": "human_review_required",
            "max_action": "escalate",
            "proposed_action": "escalate",
            "final_action": "escalate",
            "human_review_required": True,
        })
        reasons = authority.get("reasons")
        authority["reasons"] = [*reasons, reason] if isinstance(reasons, list) else [reason]
    return safe
