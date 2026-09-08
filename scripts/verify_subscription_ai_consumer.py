"""One manual, synthetic Codex subscription check through the installed consumer."""

from __future__ import annotations

import json
import math
import os
from urllib.parse import urlsplit

REPOSITORY = "QuantStrategyLab/AIAuditBridge"
MODEL = "gpt-6-astra"


def validate_environment() -> None:
    expected = {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "AI_GATEWAY_SOURCE_REPO": REPOSITORY,
    }
    url = urlsplit(os.environ.get("CODEX_AUDIT_SERVICE_URL", ""))
    if (
        any(os.environ.get(key) != value for key, value in expected.items())
        or not os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
        or not os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        or url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or any(value for key, value in os.environ.items() if "API_KEY" in key)
        or os.environ.get("CODEX_AUDIT_SERVICE_TOKEN")
    ):
        raise ValueError("verification environment rejected")


def run_check() -> dict[str, object]:
    validate_environment()
    from quant_strategy_plugins.ai_audit import (
        build_ai_audit_endpoints,
        run_crisis_ai_audit,
    )

    endpoints = build_ai_audit_endpoints(codex_enabled=True, codex_model=MODEL)
    if len(endpoints) != 1 or endpoints[0].provider != "codex" or endpoints[0].model != MODEL:
        raise ValueError("subscription-only endpoint required")
    # No market data, real symbol, price, portfolio, or request for tool use.
    source = {
        "profile": "synthetic_subscription_check",
        "canonical_route": "no_action",
        "suggested_action": "watch_only",
        "would_trade_if_enabled": False,
        "data_quality": "synthetic; no market evidence supplied",
        "evidence": "Synthetic test only. No external research or tool use is needed.",
    }
    original = dict(source)
    result = run_crisis_ai_audit(
        source, enabled=True, codex_enabled=True, codex_model=MODEL, timeout_seconds=120,
    )
    controls = result.get("execution_controls", {})
    confidence = result.get("confidence")
    confidence_available = (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and math.isfinite(confidence)
        and 0 <= confidence <= 1
    )
    # The consumer preserves unavailable confidence as None, not financial evidence.
    structured = (
        result.get("verdict") in {"agree", "review", "data_insufficient"}
        and bool(result.get("summary"))
        and (confidence is None or confidence_available)
    )
    unchanged = (
        source == original
        and result.get("deterministic_route") == source["canonical_route"]
        and result.get("deterministic_action") == source["suggested_action"]
        and result.get("final_route_unchanged") is True
        and result.get("mode") == "shadow_only"
        and controls.get("capital_impact") == "none"
        and all(controls.get(key) is False for key in (
            "broker_order_allowed", "live_allocation_mutation_allowed", "allocation_recommendation_allowed",
        ))
    )
    attempts = result.get("attempts", [])
    advisory = result.get("status") == "advisory"
    return {
        "passed": bool(structured and unchanged and advisory and len(attempts) == 1),
        "synthetic_input": True,
        "structured_result": structured,
        "confidence_available": confidence_available,
        "financial_claims_verified": False,
        "consumer_advisory": advisory,
        "deterministic_route_unchanged": unchanged,
        "consumer_attempts": len(attempts),
        "requested_model": MODEL,
        "learning_only": True,
        "promotion_eligible": False,
        "live_ready": False,
        "size_zero_required": True,
        "no_order": True,
    }


def main() -> int:
    try:
        report = run_check()
    except Exception:  # noqa: BLE001 - sanitize every external failure at the CLI boundary.
        # Never emit provider text, response bodies, headers, or credentials.
        report = {"passed": False, "failure_category": "verification_failed"}
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
