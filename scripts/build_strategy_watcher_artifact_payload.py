#!/usr/bin/env python3
"""Build one comparable watcher payload from two sanitized run artifacts."""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "strategy_performance.v2"
METRICS_KIND = "performance"
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_METRICS = frozenset({"sharpe", "cagr", "calmar", "win_rate", "max_dd"})
_FORBIDDEN_KEYS = re.compile(r"(?:secret|token|password|credential|api[_-]?key|order|fill|capital|account|broker|path)", re.IGNORECASE)
_SAFE_RESEARCH_KEYS = frozenset({"no_order"})


class StrategyWatcherArtifactError(ValueError):
    """Raised when two artifacts cannot form a safe metric comparison."""


def _exact_mapping(value: object, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise StrategyWatcherArtifactError(f"invalid {label}")
    return dict(value)


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise StrategyWatcherArtifactError(f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StrategyWatcherArtifactError(f"invalid {label}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StrategyWatcherArtifactError(f"invalid {label}")
    return value


def _finite_metrics(value: object, label: str) -> dict[str, float]:
    metrics = _exact_mapping(value, _METRICS, label)
    result: dict[str, float] = {}
    for key, raw in metrics.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            raise StrategyWatcherArtifactError(f"invalid {label}")
        result[key] = float(raw)
    return result


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise StrategyWatcherArtifactError(f"invalid {label}")
    return value


def _revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise StrategyWatcherArtifactError(f"invalid {label}")
    return value


def _forbid_unsafe_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key) not in _SAFE_RESEARCH_KEYS and _FORBIDDEN_KEYS.search(str(key)):
                raise StrategyWatcherArtifactError("unsafe artifact field")
            _forbid_unsafe_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _forbid_unsafe_keys(nested)
    elif isinstance(value, float) and not math.isfinite(value):
        raise StrategyWatcherArtifactError("non-finite artifact value")


def _performance_artifact(value: object, *, expected_repository: str) -> dict[str, Any]:
    artifact = _exact_mapping(
        value,
        frozenset(
            {
                "schema_version",
                "metrics_kind",
                "repository",
                "strategy_profile",
                "candidate_kind",
                "domain",
                "generated_at",
                "as_of",
                "current_metrics",
                "evidence",
                "lifecycle",
                "authority",
            }
        ),
        "strategy performance artifact",
    )
    _forbid_unsafe_keys(artifact)
    if artifact["schema_version"] != SCHEMA_VERSION or artifact["metrics_kind"] != METRICS_KIND:
        raise StrategyWatcherArtifactError("unsupported strategy performance artifact")
    if artifact["repository"] != expected_repository:
        raise StrategyWatcherArtifactError("artifact repository does not match validated source repository")
    if not isinstance(artifact["strategy_profile"], str) or not re.fullmatch(r"[A-Za-z0-9._=-]{1,120}", artifact["strategy_profile"]):
        raise StrategyWatcherArtifactError("invalid strategy profile")
    if artifact["candidate_kind"] not in {"individual", "portfolio", "plugin"}:
        raise StrategyWatcherArtifactError("invalid candidate kind")
    if artifact["domain"] not in {"us_equity", "hk_equity", "cn_equity", "crypto"}:
        raise StrategyWatcherArtifactError("invalid domain")
    _timestamp(artifact["generated_at"], "generated_at")
    if not isinstance(artifact["as_of"], str) or not _DATE.fullmatch(artifact["as_of"]):
        raise StrategyWatcherArtifactError("invalid as_of")
    try:
        datetime.fromisoformat(artifact["as_of"])
    except ValueError as exc:
        raise StrategyWatcherArtifactError("invalid as_of") from exc
    artifact["current_metrics"] = _finite_metrics(artifact["current_metrics"], "current_metrics")
    evidence = _exact_mapping(
        artifact["evidence"],
        frozenset({"p1_input_digest", "p2_config_digest", "p3_evidence_id", "strategy_revision", "producer_revision"}),
        "evidence",
    )
    for key in ("p1_input_digest", "p2_config_digest", "p3_evidence_id"):
        _sha256(evidence[key], f"evidence.{key}")
    _revision(evidence["strategy_revision"], "evidence.strategy_revision")
    _revision(evidence["producer_revision"], "evidence.producer_revision")
    if artifact["lifecycle"] != {"stage": "P3", "status": "verified"}:
        raise StrategyWatcherArtifactError("artifact is not verified P3 evidence")
    if artifact["authority"] != {"research_only": True, "no_order": True, "p4_p5_p6_authorized": False}:
        raise StrategyWatcherArtifactError("artifact authority is not research-only")
    return artifact


def build_strategy_watcher_artifact_payload(
    *,
    current_artifact: object,
    baseline_artifact: object,
    source_repository: object,
    workflow_file: object,
    current_run_id: object,
    baseline_run_id: object,
) -> dict[str, object]:
    """Join two completed P3 performance observations for an issue-only watcher."""
    if not isinstance(source_repository, str) or not _REPOSITORY.fullmatch(source_repository):
        raise StrategyWatcherArtifactError("invalid source repository")
    if not isinstance(workflow_file, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow_file):
        raise StrategyWatcherArtifactError("invalid workflow file")
    if not isinstance(current_run_id, str) or not current_run_id.isdigit() or not isinstance(baseline_run_id, str) or not baseline_run_id.isdigit():
        raise StrategyWatcherArtifactError("invalid workflow run id")
    current = _performance_artifact(current_artifact, expected_repository=source_repository)
    baseline = _performance_artifact(baseline_artifact, expected_repository=source_repository)
    for key in ("strategy_profile", "candidate_kind", "domain"):
        if current[key] != baseline[key]:
            raise StrategyWatcherArtifactError("artifacts describe different research candidates")
    if _timestamp(baseline["generated_at"], "baseline generated_at") >= _timestamp(current["generated_at"], "current generated_at"):
        raise StrategyWatcherArtifactError("baseline must precede current observation")
    if baseline["as_of"] >= current["as_of"]:
        raise StrategyWatcherArtifactError("baseline must use an earlier data cutoff")
    return {
        "schema_version": SCHEMA_VERSION,
        "metrics_kind": METRICS_KIND,
        "repo": source_repository,
        "strategy_profile": current["strategy_profile"],
        "candidate_kind": current["candidate_kind"],
        "domain": current["domain"],
        "generated_at": current["generated_at"],
        "current_metrics": current["current_metrics"],
        "baseline_metrics": baseline["current_metrics"],
        "source": f"github_actions:{source_repository}:{workflow_file}:{baseline_run_id}-{current_run_id}",
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--workflow-file", required=True)
    parser.add_argument("--current-run-id", required=True)
    parser.add_argument("--baseline-run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_bytes())
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise StrategyWatcherArtifactError("invalid strategy performance artifact") from exc


def main() -> None:
    args = _arguments()
    payload = build_strategy_watcher_artifact_payload(
        current_artifact=_read_json(args.current),
        baseline_artifact=_read_json(args.baseline),
        source_repository=args.source_repository,
        workflow_file=args.workflow_file,
        current_run_id=args.current_run_id,
        baseline_run_id=args.baseline_run_id,
    )
    args.output.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
