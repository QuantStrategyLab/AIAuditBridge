#!/usr/bin/env python3
"""Gate one bounded, human-dispatched SOXL three-asset learning run through Codex."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from urllib.parse import urlparse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from client.config import GatewayConfig
from client.gateway_client import AiGatewayClient
from service.research_diagnosis import build_research_diagnosis_request, marker_for_research_diagnosis
from service.research_task import (
    SOXL_WATCHER_CANDIDATE_ID,
    SOXL_WATCHER_CONSUMER_REVISION,
    SOXL_WATCHER_P2_CONFIG_SHA256,
    SOXL_WATCHER_PARAMETER_BOUNDS_SHA256,
    SOXL_WATCHER_QPK_REVISION,
    SOXL_WATCHER_STRATEGY_REPOSITORY,
    SOXL_WATCHER_UES_REVISION,
    validate_strategy_diagnosis_task,
)

ADVICE_SCHEMA = "qsl.soxl-manual-learning-advice.v1"
ARTIFACT_SCHEMA = "qsl.soxl-manual-learning-run.v1"
NUMERIC_SCHEMA = "qsl.soxl-soxx-three-asset-learning.v1"
REPLAY_SCHEMA = "qsl.soxl-soxx-three-asset-learning-replay-result.v1"
BASELINE = 0.65
COST_BPS = (5.0, 10.0, 15.0)
DEVELOPMENT_CUTOFF = "2025-07-31"
UESP_REVISION = "b03ecbe4e0a7a0de22f298499f867a7039e4b60a"
UES_REVISION = "7756fe32585e85cf1d09a163203a02e3eee39fe1"
EXPECTED_REPOSITORY = "QuantStrategyLab/AIAuditBridge"
EXPECTED_REF = "refs/heads/main"
EXPECTED_EVENT = "workflow_dispatch"
SAFE_REASON = re.compile(r"[a-z0-9_]{1,64}\Z")
WATCHER_MARKER_PREFIX = "qsl-soxl-watcher-learning:v1"


class ManualLearningError(ValueError):
    """A fixed, safe failure at the manual-learning boundary."""


def parse_parameter_grid(value: str) -> tuple[float, ...]:
    try:
        parts = value.split(",")
        values = tuple(float(item.strip()) for item in parts)
    except (AttributeError, ValueError) as exc:
        raise ManualLearningError("learning_parameters_invalid") from exc
    if (
        not 1 <= len(values) <= 3
        or any(not math.isfinite(item) or item < 0 or item > BASELINE for item in values)
        or len(set(values)) != len(values)
        or BASELINE not in values
    ):
        raise ManualLearningError("learning_parameters_invalid")
    return values


def _validate_authority(context: Mapping[str, str]) -> None:
    if (
        context.get("repository") != EXPECTED_REPOSITORY
        or context.get("ref") != EXPECTED_REF
        or context.get("event_name") != EXPECTED_EVENT
        or not context.get("actor", "").strip()
        or not context.get("run_id", "").isdigit()
        or context.get("run_attempt") != "1"
    ):
        raise ManualLearningError("manual_authority_invalid")


def _safe_base(context: Mapping[str, str], values: Sequence[float], manifest_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "operation": "soxl_learning",
        "status": "unavailable",
        "authority": {
            "event_name": context["event_name"],
            "actor": context["actor"],
            "repository": context["repository"],
            "ref": context["ref"],
            "run_id": context["run_id"],
            "run_attempt": 1,
        },
        "parameter_key": "blend_gate_mid_soxl_weight",
        "parameter_values": list(values),
        "cost_bps": list(COST_BPS),
        "development_cutoff": DEVELOPMENT_CUTOFF,
        "input_identity": {"manifest_sha256": manifest_sha256, "member_count": 4},
        "consumer_source": {
            "repository": "QuantStrategyLab/UsEquitySnapshotPipelines",
            "revision": UESP_REVISION,
        },
        "learning_only": True,
        "no_order": True,
        "size_zero_required": True,
        "promotion_eligible": False,
        "research_executed": False,
    }


def _prompt(context: Mapping[str, str], values: Sequence[float], manifest_sha256: str) -> str:
    request = {
        "task": "human_dispatched_soxl_three_asset_learning_advice",
        "authority": {
            "event_name": context["event_name"], "actor": context["actor"],
            "run_id": context["run_id"], "run_attempt": 1,
        },
        "facts": {
            "manual_research": True, "drift_detected": False, "fault_detected": False,
            "symbols": ["SOXL", "SOXX", "BOXX"],
            "parameter_key": "blend_gate_mid_soxl_weight",
            "parameter_values": list(values), "baseline": BASELINE,
            "cost_bps": list(COST_BPS), "development_cutoff": DEVELOPMENT_CUTOFF,
            "input_manifest_sha256": manifest_sha256,
            "learning_only": True, "no_order": True, "promotion_eligible": False,
        },
        "allowed_decision": "Recommend execute or reject for this exact fixed grid only.",
        "forbidden": [
            "change code, parameters, commands, data permissions, symbols, or source revisions",
            "claim drift, fault, promotion, WFA, OOS, shadow, or trading authority",
        ],
        "required_output": {
            "schema_version": ADVICE_SCHEMA,
            "recommendation": "execute|reject",
            "reason_code": "short_fixed_identifier",
            "parameter_values": list(values),
            "learning_only": True, "no_order": True, "promotion_eligible": False,
        },
    }
    return json.dumps(request, sort_keys=True, separators=(",", ":"))


def _advice(result: object, values: Sequence[float]) -> tuple[dict[str, Any] | None, str]:
    raw = getattr(result, "raw", None)
    if isinstance(raw, Mapping) and raw.get("status") == "deferred":
        return None, "deferred"
    output = getattr(result, "output", None)
    if not (
        getattr(result, "success", False) is True
        and getattr(result, "provider", None) == "codex"
        and isinstance(getattr(result, "model", None), str)
        and bool(result.model.strip())
        and isinstance(output, str)
        and output.strip()
        and not getattr(result, "error", "")
        and not getattr(result, "note", "")
        and isinstance(raw, Mapping)
        and raw.get("status") == "succeeded"
        and raw.get("provider") == "codex"
        and raw.get("research_stage") == "optimization"
        and raw.get("model") == result.model
        and raw.get("reasoning_effort") in {"low", "medium", "high", "xhigh"}
        and isinstance(raw.get("job_id"), str)
        and bool(raw["job_id"].strip())
        and raw.get("output") == output
        and raw.get("policy_verdict", "advisory") in {"ok", "eligible", "advisory"}
    ):
        return None, "unavailable"
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return None, "unavailable"
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "recommendation", "reason_code", "parameter_values",
        "learning_only", "no_order", "promotion_eligible",
    }:
        return None, "unavailable"
    if (
        value["schema_version"] != ADVICE_SCHEMA
        or value["recommendation"] not in {"execute", "reject"}
        or not isinstance(value["reason_code"], str)
        or SAFE_REASON.fullmatch(value["reason_code"]) is None
        or value["parameter_values"] != list(values)
        or value["learning_only"] is not True
        or value["no_order"] is not True
        or value["promotion_eligible"] is not False
    ):
        return None, "unavailable"
    return {
        "status": "succeeded", "job_id": raw["job_id"], "provider": "codex",
        "model": result.model, "reasoning_effort": raw["reasoning_effort"],
        "research_stage": "optimization", "recommendation": value["recommendation"],
        "reason_code": value["reason_code"],
    }, "succeeded"


def _numeric_command(root: Path, consumer: Path, ues: Path, values: Sequence[float]) -> list[str]:
    command = [
        str(consumer / ".venv/bin/python"),
        str(consumer / "scripts/run_soxl_three_asset_learning.py"),
        "--p1-binding", str(root / "binding.json"),
        "--input-manifest", str(root / "manifest.json"),
        "--bars-member", str(root / "bars.json"),
        "--ues-project", str(ues),
        "--p2-candidate", str(consumer / "config/soxl_soxx_core_only_p2_v3.json"),
    ]
    for value in values:
        command.extend(("--blend-gate-mid-soxl-weight", f"{value:g}"))
    return command


def _sanitize_numeric(value: object, values: Sequence[float], manifest_sha256: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    if not isinstance(value, Mapping) or value.get("schema_version") != NUMERIC_SCHEMA or value.get("status") != "SUCCESS":
        raise ManualLearningError("numeric_result_invalid")
    if (
        value.get("learning_only") is not True or value.get("no_order") is not True
        or value.get("size_zero_required") is not True or value.get("promotion_eligible") is not False
        or value.get("research_executed") is not True or value.get("development_cutoff") != DEVELOPMENT_CUTOFF
        or value.get("parameter_key") != "blend_gate_mid_soxl_weight"
        or value.get("trial_count") != len(values) or value.get("cost_bps") != list(COST_BPS)
    ):
        raise ManualLearningError("numeric_result_invalid")
    p1 = value.get("p1_identity")
    source = value.get("source_identity")
    if not isinstance(p1, Mapping) or p1.get("input_manifest_sha256") != manifest_sha256:
        raise ManualLearningError("numeric_result_invalid")
    if (
        not isinstance(source, Mapping)
        or source.get("repository") != "QuantStrategyLab/UsEquityStrategies"
        or source.get("revision") != UES_REVISION
        or source.get("quant_platform_kit_revision") != SOXL_WATCHER_QPK_REVISION
        or not isinstance(source.get("uv_lock_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["uv_lock_sha256"]) is None
    ):
        raise ManualLearningError("numeric_result_invalid")
    results = value.get("results")
    expected = [(parameter, cost) for parameter in values for cost in COST_BPS]
    if not isinstance(results, list) or len(results) != len(expected):
        raise ManualLearningError("numeric_result_invalid")
    safe: list[dict[str, Any]] = []
    metrics = ("strategy_profile", "sharpe_ratio", "max_drawdown", "cagr", "volatility", "total_return", "start_date", "end_date", "observation_count")
    for item, (parameter, cost) in zip(results, expected, strict=True):
        if not isinstance(item, Mapping) or item.get("schema_version") != REPLAY_SCHEMA or item.get("status") != "SUCCESS" or item.get("parameter_override") != {"blend_gate_mid_soxl_weight": parameter} or item.get("cost_bps") != cost:
            raise ManualLearningError("numeric_result_invalid")
        backtest = item.get("backtest_result")
        if not isinstance(backtest, Mapping) or any(key not in backtest for key in metrics):
            raise ManualLearningError("numeric_result_invalid")
        numeric_metrics = ("sharpe_ratio", "max_drawdown", "cagr", "volatility", "total_return")
        if any(
            isinstance(backtest[key], bool)
            or not isinstance(backtest[key], (int, float))
            or not math.isfinite(float(backtest[key]))
            for key in numeric_metrics
        ):
            raise ManualLearningError("numeric_result_invalid")
        if (
            not isinstance(backtest["observation_count"], int)
            or isinstance(backtest["observation_count"], bool)
            or backtest["observation_count"] < 2
            or not isinstance(backtest["strategy_profile"], str)
            or backtest["strategy_profile"] != "soxl_soxx_three_asset_mid_weight_learning_v1"
            or not isinstance(backtest["start_date"], str)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", backtest["start_date"]) is None
            or not isinstance(backtest["end_date"], str)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", backtest["end_date"]) is None
            or backtest["start_date"] > backtest["end_date"]
            or backtest["end_date"] > DEVELOPMENT_CUTOFF
            or not isinstance(item.get("output_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["output_sha256"]) is None
        ):
            raise ManualLearningError("numeric_result_invalid")
        safe.append({
            "parameter_override": {"blend_gate_mid_soxl_weight": parameter},
            "cost_bps": cost,
            "backtest_result": {key: backtest[key] for key in metrics},
            "output_sha256": item.get("output_sha256"),
        })
    digest = value.get("result_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ManualLearningError("numeric_result_invalid")
    safe_source = {
        key: source[key]
        for key in ("repository", "revision", "quant_platform_kit_revision", "uv_lock_sha256")
    }
    return safe, safe_source, digest


def _issue_number(repository: str, issue_url: str) -> str:
    parsed = urlparse(issue_url)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https" or parsed.netloc != "github.com"
        or len(parts) != 4 or "/".join(parts[:2]) != repository
        or parts[2] != "issues" or not parts[3].isdigit()
    ):
        raise ManualLearningError("watcher_issue_invalid")
    return parts[3]


def read_issue_comments(repository: str, issue_url: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repository}/issues/{_issue_number(repository, issue_url)}/comments?per_page=100"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if completed.returncode:
        raise ManualLearningError("watcher_comments_unavailable")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ManualLearningError("watcher_comments_unavailable") from exc
    if not isinstance(value, list) or any(not isinstance(page, list) for page in value):
        raise ManualLearningError("watcher_comments_unavailable")
    comments = [item for page in value for item in page]
    if any(not isinstance(item, dict) for item in comments):
        raise ManualLearningError("watcher_comments_unavailable")
    return comments


def write_issue_comment(repository: str, issue_url: str, body: str) -> str:
    completed = subprocess.run(
        ["gh", "issue", "comment", issue_url, "--repo", repository, "--body-file", "-"],
        input=body, capture_output=True, text=True, timeout=30, check=False,
    )
    if completed.returncode:
        raise ManualLearningError("watcher_comment_write_failed")
    return completed.stdout.strip()


def _trusted_comment_bodies(comments: object, github_app_id: str) -> list[str]:
    if not github_app_id.isdigit() or int(github_app_id) <= 0:
        raise ManualLearningError("watcher_comment_identity_invalid")
    if not isinstance(comments, list):
        raise ManualLearningError("watcher_comments_unavailable")
    bodies: list[str] = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            raise ManualLearningError("watcher_comments_unavailable")
        app = comment.get("performed_via_github_app")
        if isinstance(app, Mapping) and app.get("id") == int(github_app_id) and isinstance(comment.get("body"), str):
            bodies.append(comment["body"])
    return bodies


def watcher_learning_comment(
    task: Mapping[str, Any], *, phase: str, status: str,
    numeric_result_sha256: str = "", numeric_summary: object = None,
    numeric_source_identity: object = None, failure_stage: str | None = None,
) -> str:
    verified = validate_strategy_diagnosis_task(task)
    if phase not in {"started", "terminal"} or status not in {"started", "accepted", "failed"}:
        raise ManualLearningError("watcher_stage_invalid")
    if phase == "started" and status != "started":
        raise ManualLearningError("watcher_stage_invalid")
    if phase == "terminal" and status == "started":
        raise ManualLearningError("watcher_stage_invalid")
    if failure_stage not in {None, "pre_numeric_failed", "numeric_execution_failed"}:
        raise ManualLearningError("watcher_stage_invalid")
    if status == "failed" and failure_stage is None:
        failure_stage = "numeric_execution_failed"
    if status != "failed" and failure_stage is not None:
        raise ManualLearningError("watcher_stage_invalid")
    if numeric_result_sha256 and re.fullmatch(r"[0-9a-f]{64}", numeric_result_sha256) is None:
        raise ManualLearningError("numeric_result_invalid")
    marker = ":".join(
        (
            WATCHER_MARKER_PREFIX, phase, verified["task_sha256"],
            SOXL_WATCHER_PARAMETER_BOUNDS_SHA256, SOXL_WATCHER_CONSUMER_REVISION,
        )
    )
    record = {
        "task_id": verified["task_id"], "task_sha256": verified["task_sha256"],
        "parameter_bounds_sha256": SOXL_WATCHER_PARAMETER_BOUNDS_SHA256,
        "consumer_revision": SOXL_WATCHER_CONSUMER_REVISION,
        "strategy_revision": SOXL_WATCHER_UES_REVISION,
        "status": status, "numeric_result_sha256": numeric_result_sha256 or None,
        "numeric_summary": numeric_summary,
        "numeric_source_identity": numeric_source_identity,
        "failure_stage": failure_stage,
        "learning_only": True, "no_order": True, "promotion_eligible": False,
    }
    return f"<!-- {marker} -->\n`{canonical_json(record)}`"


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _marker(task: Mapping[str, Any], phase: str) -> str:
    return ":".join(
        (WATCHER_MARKER_PREFIX, phase, str(task["task_sha256"]),
         SOXL_WATCHER_PARAMETER_BOUNDS_SHA256, SOXL_WATCHER_CONSUMER_REVISION)
    )


def _terminal_from_comments(task: Mapping[str, Any], bodies: Sequence[str]) -> dict[str, Any] | None:
    marker = f"<!-- {_marker(task, 'terminal')} -->"
    candidates = [body for body in bodies if body.startswith(marker)]
    if not candidates:
        return None
    if any(not body.startswith(marker + "\n`") or not body.endswith("`") for body in candidates):
        raise ManualLearningError("watcher_terminal_invalid")
    parsed: list[dict[str, Any]] = []
    for body in candidates:
        try:
            value = json.loads(body[len(marker) + 2 : -1])
        except json.JSONDecodeError as exc:
            raise ManualLearningError("watcher_terminal_invalid") from exc
        expected = {
            "task_id", "task_sha256", "parameter_bounds_sha256", "consumer_revision",
            "strategy_revision", "status", "numeric_result_sha256", "learning_only",
            "numeric_summary", "numeric_source_identity", "failure_stage", "no_order", "promotion_eligible",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ManualLearningError("watcher_terminal_invalid")
        if (
            value["task_id"] != task["task_id"] or value["task_sha256"] != task["task_sha256"]
            or value["parameter_bounds_sha256"] != SOXL_WATCHER_PARAMETER_BOUNDS_SHA256
            or value["consumer_revision"] != SOXL_WATCHER_CONSUMER_REVISION
            or value["strategy_revision"] != SOXL_WATCHER_UES_REVISION
            or value["status"] not in {"accepted", "failed"}
            or value["failure_stage"] not in {None, "pre_numeric_failed", "numeric_execution_failed"}
            or (value["status"] == "accepted" and value["failure_stage"] is not None)
            or (value["status"] == "failed" and value["failure_stage"] is None)
            or value["learning_only"] is not True or value["no_order"] is not True
            or value["promotion_eligible"] is not False
            or (value["status"] == "accepted" and re.fullmatch(r"[0-9a-f]{64}", str(value["numeric_result_sha256"] or "")) is None)
        ):
            raise ManualLearningError("watcher_terminal_invalid")
        if value["status"] == "accepted":
            raw_results = []
            if not isinstance(value["numeric_summary"], list):
                raise ManualLearningError("watcher_terminal_invalid")
            for item in value["numeric_summary"]:
                if not isinstance(item, Mapping):
                    raise ManualLearningError("watcher_terminal_invalid")
                raw_results.append({
                    "schema_version": REPLAY_SCHEMA, "status": "SUCCESS",
                    "parameter_override": item.get("parameter_override"),
                    "cost_bps": item.get("cost_bps"),
                    "backtest_result": item.get("backtest_result"),
                    "output_sha256": item.get("output_sha256"),
                })
            reconstructed = {
                "schema_version": NUMERIC_SCHEMA, "status": "SUCCESS",
                "learning_only": True, "no_order": True, "size_zero_required": True,
                "promotion_eligible": False, "research_executed": True,
                "development_cutoff": DEVELOPMENT_CUTOFF,
                "p1_identity": {"input_manifest_sha256": task["evidence"]["p1_input_digest"]},
                "source_identity": value["numeric_source_identity"],
                "parameter_key": "blend_gate_mid_soxl_weight", "trial_count": 3,
                "cost_bps": list(COST_BPS), "results": raw_results,
                "result_sha256": value["numeric_result_sha256"],
            }
            safe, source, digest = _sanitize_numeric(
                reconstructed, (0.65, 0.6, 0.55), task["evidence"]["p1_input_digest"],
            )
            value["numeric_summary"] = safe
            value["numeric_source_identity"] = source
            value["numeric_result_sha256"] = digest
        elif value["numeric_summary"] is not None or value["numeric_source_identity"] is not None or value["numeric_result_sha256"] is not None:
            raise ManualLearningError("watcher_terminal_invalid")
        parsed.append(value)
    if any(value != parsed[0] for value in parsed[1:]):
        raise ManualLearningError("watcher_terminal_conflict")
    return parsed[0]


def _watcher_context(
    watcher_result: Mapping[str, Any], diagnosis_result: Mapping[str, Any],
    *, github_app_id: str,
    read_comments: Callable[[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    snapshot = watcher_result.get("research_task_source_snapshot")
    issues = watcher_result.get("issues")
    if not isinstance(snapshot, Mapping) or snapshot.get("data_status") != "ready" or not isinstance(issues, list):
        raise ManualLearningError("watcher_task_unavailable")
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], Mapping):
        raise ManualLearningError("watcher_task_unavailable")
    task = validate_strategy_diagnosis_task(tasks[0])
    if (
        task["target"] != {
            "candidate_id": SOXL_WATCHER_CANDIDATE_ID, "candidate_kind": "individual",
            "domain": "us_equity", "repository": SOXL_WATCHER_STRATEGY_REPOSITORY,
            "strategy_revision": SOXL_WATCHER_UES_REVISION,
        }
        or task["evidence"]["p2_config_digest"] != SOXL_WATCHER_P2_CONFIG_SHA256
        or task["experiment"]["parameter_bounds_sha256"] != SOXL_WATCHER_PARAMETER_BOUNDS_SHA256
    ):
        raise ManualLearningError("watcher_task_not_executable")
    event_key = task["task_id"].removeprefix("watcher-")
    matching = [
        issue for issue in issues if isinstance(issue, Mapping)
        and isinstance(issue.get("task"), Mapping) and issue["task"].get("event_key") == event_key
        and issue.get("repo") == "QuantStrategyLab/UsEquitySnapshotPipelines"
        and isinstance(issue.get("url") or issue.get("existing_url"), str)
    ]
    if len(matching) != 1:
        raise ManualLearningError("watcher_issue_unavailable")
    repository = str(matching[0]["repo"])
    issue_url = str(matching[0].get("url") or matching[0].get("existing_url"))
    _issue_number(repository, issue_url)
    bodies = _trusted_comment_bodies(read_comments(repository, issue_url), github_app_id)
    if not isinstance(diagnosis_result.get("diagnoses"), list):
        raise ManualLearningError("watcher_diagnosis_unavailable")
    diagnosis_marker = marker_for_research_diagnosis(build_research_diagnosis_request(task))
    if not any(body.startswith(diagnosis_marker) for body in bodies):
        raise ManualLearningError("watcher_diagnosis_unavailable")
    terminal = _terminal_from_comments(task, bodies)
    started = any(body.startswith(f"<!-- {_marker(task, 'started')} -->\n") for body in bodies)
    return {"task": task, "repository": repository, "issue_url": issue_url, "terminal": terminal, "started": started}


def prepare_watcher_learning(
    watcher_result: Mapping[str, Any], diagnosis_result: Mapping[str, Any], *, github_app_id: str,
    read_comments: Callable[[str, str], list[dict[str, Any]]] = read_issue_comments,
) -> dict[str, Any]:
    try:
        context = _watcher_context(
            watcher_result, diagnosis_result, github_app_id=github_app_id, read_comments=read_comments,
        )
    except (ManualLearningError, OSError, ValueError, subprocess.SubprocessError):
        return {"status": "parked", "ready": False}
    if context["terminal"] is not None:
        return {"status": "reused", "ready": False}
    if context["started"]:
        return {"status": "parked", "ready": False, "failure_stage": "numeric_outcome_unknown"}
    task = context["task"]
    return {
        "status": "ready", "ready": True, "p1_manifest_sha256": task["evidence"]["p1_input_digest"],
        "producer_revision": task["evidence"]["producer_revision"],
    }


def _watcher_base(task: Mapping[str, Any], manifest_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_SCHEMA, "operation": "soxl_watcher_learning",
        "source": "watcher_event_independent_learning", "status": "parked",
        "task_id": task["task_id"], "task_sha256": task["task_sha256"],
        "experiment": {"parameter_bounds_sha256": SOXL_WATCHER_PARAMETER_BOUNDS_SHA256},
        "parameter_key": "blend_gate_mid_soxl_weight", "parameter_values": [0.65, 0.6, 0.55],
        "cost_bps": list(COST_BPS), "development_cutoff": DEVELOPMENT_CUTOFF,
        "input_identity": {"manifest_sha256": manifest_sha256, "member_count": 4},
        "consumer_source": {"repository": "QuantStrategyLab/UsEquitySnapshotPipelines", "revision": SOXL_WATCHER_CONSUMER_REVISION},
        "learning_only": True, "no_order": True, "size_zero_required": True,
        "promotion_eligible": False, "research_executed": False,
    }


def record_watcher_pre_numeric_failure(
    *, watcher_result: Mapping[str, Any], diagnosis_result: Mapping[str, Any], github_app_id: str,
    read_comments: Callable[[str, str], list[dict[str, Any]]] = read_issue_comments,
    write_comment: Callable[[str, str, str], str] = write_issue_comment,
) -> dict[str, Any]:
    context = _watcher_context(
        watcher_result, diagnosis_result, github_app_id=github_app_id, read_comments=read_comments,
    )
    if context["terminal"] is not None:
        return {"status": "reused"}
    if context["started"]:
        raise ManualLearningError("numeric_outcome_unknown")
    write_comment(
        context["repository"], context["issue_url"],
        watcher_learning_comment(
            context["task"], phase="terminal", status="failed", failure_stage="pre_numeric_failed",
        ),
    )
    return {"status": "failed", "failure_stage": "pre_numeric_failed"}


def run_watcher_learning(
    *, watcher_result: Mapping[str, Any], diagnosis_result: Mapping[str, Any], manifest_sha256: str,
    root: Path, consumer_source: Path, ues_source: Path, github_app_id: str,
    read_comments: Callable[[str, str], list[dict[str, Any]]] = read_issue_comments,
    write_comment: Callable[[str, str, str], str] = write_issue_comment,
    command_runner: Callable[[list[str]], Any] | None = None,
) -> dict[str, Any]:
    try:
        context = _watcher_context(watcher_result, diagnosis_result, github_app_id=github_app_id, read_comments=read_comments)
    except ManualLearningError as exc:
        return {"status": "parked", "failure_stage": str(exc), "research_executed": False,
                "learning_only": True, "no_order": True, "size_zero_required": True, "promotion_eligible": False}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "parked", "failure_stage": "watcher_comments_unavailable", "research_executed": False,
                "learning_only": True, "no_order": True, "size_zero_required": True, "promotion_eligible": False}
    task = context["task"]
    artifact = _watcher_base(task, manifest_sha256)
    if manifest_sha256 != task["evidence"]["p1_input_digest"]:
        artifact["failure_stage"] = "input_identity_invalid"
        return artifact
    terminal = context["terminal"]
    if terminal is not None:
        artifact["status"] = "accepted" if terminal["status"] == "accepted" else "parked"
        artifact["research_executed"] = terminal["status"] == "accepted"
        artifact["numeric_execution"] = {"status": "reused"}
        artifact["numeric_result_sha256"] = terminal["numeric_result_sha256"]
        if terminal["status"] == "failed":
            artifact["failure_stage"] = terminal["failure_stage"]
        if terminal["status"] == "accepted":
            artifact["numeric_summary"] = terminal["numeric_summary"]
            artifact["numeric_source_identity"] = terminal["numeric_source_identity"]
        return artifact
    if context["started"]:
        artifact.update(failure_stage="numeric_outcome_unknown", research_executed=None, numeric_execution={"status": "outcome_unknown"})
        return artifact
    required = (
        root / "binding.json", root / "manifest.json", root / "bars.json",
        consumer_source / "scripts/run_soxl_three_asset_learning.py",
        consumer_source / "config/soxl_soxx_core_only_p2_v3.json",
    )
    if any(path.is_symlink() or not path.is_file() for path in required) or not (consumer_source / ".venv/bin/python").is_file() or not ues_source.is_dir():
        artifact["failure_stage"] = "source_or_input_unavailable"
        return artifact
    try:
        write_comment(context["repository"], context["issue_url"], watcher_learning_comment(task, phase="started", status="started"))
    except (ManualLearningError, OSError, subprocess.SubprocessError):
        artifact["failure_stage"] = "watcher_comment_write_failed"
        return artifact
    artifact.update(research_executed=None, numeric_execution={"status": "started"})
    runner = command_runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=1200, check=False))
    try:
        completed = runner(_numeric_command(root, consumer_source, ues_source, (0.65, 0.6, 0.55)))
    except (OSError, subprocess.SubprocessError):
        artifact.update(failure_stage="numeric_outcome_unknown", numeric_execution={"status": "outcome_unknown"})
        return artifact
    if getattr(completed, "returncode", None) != 0:
        artifact.update(failure_stage="numeric_execution_failed", numeric_execution={"status": "failed"})
        try:
            write_comment(context["repository"], context["issue_url"], watcher_learning_comment(task, phase="terminal", status="failed"))
        except (ManualLearningError, OSError, subprocess.SubprocessError):
            artifact["failure_stage"] = "numeric_outcome_unknown"
            artifact["numeric_execution"] = {"status": "outcome_unknown"}
        return artifact
    try:
        safe, source, digest = _sanitize_numeric(json.loads(completed.stdout), (0.65, 0.6, 0.55), manifest_sha256)
        write_comment(
            context["repository"], context["issue_url"],
            watcher_learning_comment(
                task, phase="terminal", status="accepted", numeric_result_sha256=digest,
                numeric_summary=safe, numeric_source_identity=source,
            ),
        )
    except (AttributeError, TypeError, json.JSONDecodeError, ManualLearningError, OSError, subprocess.SubprocessError):
        artifact.update(failure_stage="numeric_outcome_unknown", numeric_execution={"status": "outcome_unknown"})
        return artifact
    artifact.update(status="accepted", research_executed=True, numeric_summary=safe,
                    numeric_source_identity=source, numeric_result_sha256=digest,
                    numeric_execution={"status": "succeeded"})
    return artifact


def run_manual_learning(
    *, parameter_grid: str, manifest_sha256: str, root: Path, consumer_source: Path,
    ues_source: Path, context: Mapping[str, str],
    client_factory: Callable[[GatewayConfig], Any] = AiGatewayClient,
    gateway_config: GatewayConfig | None = None,
    command_runner: Callable[[list[str]], Any] | None = None,
    progress_writer: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    values = parse_parameter_grid(parameter_grid)
    _validate_authority(context)
    if len(manifest_sha256) != 64 or any(char not in "0123456789abcdef" for char in manifest_sha256):
        raise ManualLearningError("input_identity_invalid")
    required = (
        root / "binding.json", root / "manifest.json", root / "bars.json",
        consumer_source / "scripts/run_soxl_three_asset_learning.py",
        consumer_source / "config/soxl_soxx_core_only_p2_v3.json",
    )
    interpreter = consumer_source / ".venv/bin/python"
    if (
        any(path.is_symlink() or not path.is_file() for path in required)
        or not interpreter.is_file()
        or ues_source.is_symlink()
        or not ues_source.is_dir()
    ):
        raise ManualLearningError("source_or_input_unavailable")
    artifact = _safe_base(context, values, manifest_sha256)
    try:
        config = gateway_config or GatewayConfig.from_env()
        result = client_factory(config).execute(
            _prompt(context, values, manifest_sha256), mode="review_only",
            research_stage="optimization", allowed_providers=["codex"],
            source_repository=EXPECTED_REPOSITORY, source_ref="main", timeout=600,
        )
    except Exception:  # noqa: BLE001 - provider detail must not cross this boundary
        artifact["failure_stage"] = "codex_unavailable"
        return artifact
    advice, state = _advice(result, values)
    if advice is None:
        artifact["status"] = state
        artifact["failure_stage"] = "codex_admission_or_result"
        return artifact
    artifact["ai_execution"] = advice
    if advice["recommendation"] != "execute":
        artifact["status"] = "rejected"
        return artifact
    runner = command_runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=1200, check=False))
    artifact["numeric_execution"] = {"status": "started"}
    artifact["research_executed"] = None
    if progress_writer is not None:
        progress_writer(artifact)
    try:
        completed = runner(_numeric_command(root, consumer_source, ues_source, values))
    except (OSError, subprocess.SubprocessError):
        artifact["status"] = "parked"
        artifact["failure_stage"] = "numeric_outcome_unknown"
        artifact["numeric_execution"] = {"status": "outcome_unknown"}
        return artifact
    if getattr(completed, "returncode", None) != 0:
        artifact["status"] = "parked"
        artifact["failure_stage"] = "numeric_execution_failed"
        artifact["numeric_execution"] = {"status": "failed"}
        return artifact
    try:
        numeric = json.loads(completed.stdout)
        safe, source, digest = _sanitize_numeric(numeric, values, manifest_sha256)
    except (AttributeError, TypeError, json.JSONDecodeError, ManualLearningError):
        artifact["status"] = "parked"
        artifact["failure_stage"] = "numeric_result_invalid"
        artifact["numeric_execution"] = {"status": "outcome_unknown"}
        return artifact
    artifact.update(
        status="accepted", research_executed=True, numeric_summary=safe,
        numeric_source_identity=source, numeric_result_sha256=digest,
        numeric_execution={"status": "succeeded"},
    )
    return artifact


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def initialize_record(path: Path, context: Mapping[str, str]) -> None:
    """Write the bound terminal placeholder before setup or remote reads begin."""
    _validate_authority(context)
    _write(
        path,
        {
            "schema_version": ARTIFACT_SCHEMA,
            "operation": "soxl_learning",
            "status": "parked",
            "failure_stage": "setup_incomplete",
            "authority": {
                "event_name": context["event_name"],
                "actor": context["actor"],
                "repository": context["repository"],
                "ref": context["ref"],
                "run_id": context["run_id"],
                "run_attempt": 1,
            },
            "research_executed": False,
            "learning_only": True,
            "no_order": True,
            "size_zero_required": True,
            "promotion_eligible": False,
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameter-grid")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--consumer-source", type=Path)
    parser.add_argument("--ues-source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repository")
    parser.add_argument("--ref")
    parser.add_argument("--event-name")
    parser.add_argument("--actor")
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt")
    parser.add_argument("--watcher-result", type=Path)
    parser.add_argument("--diagnosis-result", type=Path)
    parser.add_argument("--github-app-id")
    parser.add_argument("--watcher-preflight", action="store_true")
    parser.add_argument("--watcher-record-pre-numeric-failure", action="store_true")
    args = parser.parse_args(argv)
    if args.watcher_result is not None:
        if args.diagnosis_result is None or not args.github_app_id:
            parser.error("watcher mode requires --diagnosis-result and --github-app-id")
        watcher_result = json.loads(args.watcher_result.read_text(encoding="utf-8"))
        diagnosis_result = json.loads(args.diagnosis_result.read_text(encoding="utf-8"))
        if args.watcher_preflight:
            result = prepare_watcher_learning(
                watcher_result, diagnosis_result, github_app_id=args.github_app_id,
            )
            print(canonical_json(result))
            return 0
        if args.watcher_record_pre_numeric_failure:
            try:
                result = record_watcher_pre_numeric_failure(
                    watcher_result=watcher_result, diagnosis_result=diagnosis_result,
                    github_app_id=args.github_app_id,
                )
            except (ManualLearningError, OSError, ValueError, subprocess.SubprocessError):
                print(canonical_json({"status": "parked", "failure_stage": "watcher_failure_record_unavailable"}))
                return 2
            print(canonical_json(result))
            return 0 if result["status"] in {"failed", "reused"} else 2
        if any(value is None for value in (args.manifest_sha256, args.root, args.consumer_source, args.ues_source, args.output)):
            parser.error("watcher execution requires manifest, source, root and output paths")
        result = run_watcher_learning(
            watcher_result=watcher_result, diagnosis_result=diagnosis_result,
            manifest_sha256=args.manifest_sha256, root=args.root,
            consumer_source=args.consumer_source, ues_source=args.ues_source,
            github_app_id=args.github_app_id,
        )
        _write(args.output, result)
        print(json.dumps({"status": result["status"], "operation": "soxl_watcher_learning"}, sort_keys=True))
        return 0 if result["status"] == "accepted" else 2
    manual_required = (
        args.parameter_grid, args.manifest_sha256, args.root, args.consumer_source,
        args.ues_source, args.output, args.repository, args.ref, args.event_name,
        args.actor, args.run_id, args.run_attempt,
    )
    if any(value is None for value in manual_required):
        parser.error("manual mode requires the original bounded input and authority arguments")
    context = {key: getattr(args, key) for key in ("repository", "ref", "event_name", "actor", "run_id", "run_attempt")}
    try:
        result = run_manual_learning(
            parameter_grid=args.parameter_grid, manifest_sha256=args.manifest_sha256,
            root=args.root, consumer_source=args.consumer_source, ues_source=args.ues_source,
            context=context, progress_writer=lambda value: _write(args.output, value),
        )
        exit_code = 0 if result["status"] == "accepted" else 2
    except ManualLearningError as exc:
        result = {
            "schema_version": ARTIFACT_SCHEMA, "operation": "soxl_learning",
            "status": "parked", "failure_stage": str(exc), "research_executed": False,
            "learning_only": True, "no_order": True, "size_zero_required": True,
            "promotion_eligible": False,
        }
        exit_code = 2
    _write(args.output, result)
    print(json.dumps({"status": result["status"], "operation": "soxl_learning"}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
