#!/usr/bin/env python3
"""One policy-bound CN experiment, owned by the existing watcher Actions job.

The watcher task is a trigger, not optimization or trading authority. Only the
root-owned local policy selects inputs, installed code and the bounded runner.
QPK's existing ticket is the sole progress/admission record.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from service.research_task import validate_strategy_diagnosis_task

PROFILE = "cn_index_etf_tactical_rotation"
STRATEGY_REPOSITORY = "QuantStrategyLab/CnEquityStrategies"
BRIDGE_REPOSITORY = "QuantStrategyLab/AIAuditBridge"
WORKFLOW_REF = f"{BRIDGE_REPOSITORY}/.github/workflows/strategy_optimization_watcher.yml@refs/heads/main"
POLICY_PATH = Path("/etc/codex-audit-bridge-policy/cn-index-etf-research.json")
STATE_ROOT = Path("/var/lib/codex-audit-bridge/cn-index-etf-research")
_REVISION = re.compile(r"[0-9a-f]{40}")
_IDENTITY_FIELDS = {"code_revision", "input_revision", "param_space_revision", "cost_model_revision", "validator_revision"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(raw: str | bytes) -> Any:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate_json_key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))


def _read_bytes(path: Path, *, limit: int = 2_000_000) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("input_file_unavailable")
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError("input_file_unavailable")
    return raw


def _read_json(path: Path, *, limit: int = 2_000_000) -> Any:
    return _json(_read_bytes(path, limit=limit))


def _protected_file(path: Path, *, secret: bool = False) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("protected_file_required")
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("protected_file_required")
    if secret and path.stat().st_mode & 0o007:
        raise ValueError("protected_secret_required")


def _read_policy(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError
    _protected_file(path)
    value = _read_json(path, limit=128_000)
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("policy_invalid")
    return value


def _workflow_authenticated() -> bool:
    return (
        os.environ.get("GITHUB_REPOSITORY") == BRIDGE_REPOSITORY
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_WORKFLOW_REF") == WORKFLOW_REF
        and os.environ.get("GITHUB_EVENT_NAME") in {"schedule", "workflow_dispatch"}
        and bool(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")))
        and bool(_REVISION.fullmatch(os.environ.get("GITHUB_SHA", "")))
        and bool(os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"))
        and bool(os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"))
        and not os.environ.get("CODEX_AUDIT_SERVICE_TOKEN")
    )


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp_required")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamp_timezone_required")
    return timestamp.astimezone(timezone.utc)


def _select_task(watcher: dict, revision: str, now: datetime, *, allow_saved_shadow: bool = False) -> dict:
    source = watcher["research_task_source_snapshot"]
    if source.get("schema_version") != "qsl_research_task_source_snapshot.v1" or source.get("data_status") != "ready":
        raise ValueError("watcher_task_unavailable")
    matches = []
    for raw in source["tasks"]:
        task = validate_strategy_diagnosis_task(raw)
        target = task["target"]
        if target["candidate_id"] != PROFILE:
            continue
        if (target["repository"] != STRATEGY_REPOSITORY or target["domain"] != "cn_equity"
                or target["candidate_kind"] != "individual" or target["strategy_revision"] != revision):
            raise ValueError("watcher_task_identity_mismatch")
        age = (now - _timestamp(task["created_at"])).total_seconds()
        if age < 0 or (not allow_saved_shadow and age > 7 * 86400):
            raise ValueError("watcher_task_stale")
        matches.append(task)
    if len(matches) != 1:
        raise ValueError("single_verified_task_required")
    return matches[0]


def _read_drift(binding: dict, now: datetime, *, allow_saved_shadow: bool = False) -> dict:
    path = Path(binding["path"])
    if not path.is_absolute():
        raise ValueError("observation_source_required")
    raw = _read_json(path)
    score = raw.get("drift_score")
    if not isinstance(raw.get("as_of"), str):
        raise ValueError("observation_date_required")
    as_of = date.fromisoformat(raw["as_of"])
    if (raw.get("strategy_profile") != PROFILE or raw.get("domain") != "cn_equity"
            or raw.get("source_revision") != binding["source_revision"]
            or not isinstance(raw.get("source_revision"), str) or not raw["source_revision"].strip()
            or raw.get("status") not in {"review", "critical"}
            or type(score) not in (float, int) or not math.isfinite(score) or not 0 <= score <= 1
            or raw.get("alert_suppressed") is True or raw.get("baseline_available") is False
            or as_of.isoformat() != raw["as_of"] or (now.date() - as_of).days < 0
            or (not allow_saved_shadow and (now.date() - as_of).days > 7)):
        raise ValueError("observation_unavailable")
    return {"strategy_profile": PROFILE, "domain": "cn_equity", "as_of": raw["as_of"],
            "drift_score": score, "status": raw["status"], "source_revision": raw["source_revision"]}


def admit_one_new_experiment(ticket_dir: Path, created_at: str) -> bool:
    """Called inside QPK's directory lock; no second lock, ledger or retry."""
    try:
        current = _timestamp(created_at)
        count = 0
        for path in ticket_dir.glob("*.json"):
            ticket = _read_json(path)
            if (not re.fullmatch(r"rpt_[0-9a-f]{64}", path.stem) or ticket["ticket_id"] != path.stem
                    or ticket["strategy_profile"] != PROFILE or ticket["domain"] != "cn_equity"
                    or ticket["live_authority_granted"] is not False):
                return False
            timestamp = _timestamp(ticket["created_at"])
            if timestamp > current:
                return False
            count += timestamp.date() == current.date()
        return count == 0
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _installed_revision(distribution: str, expected: str) -> None:
    if not isinstance(expected, str) or not _REVISION.fullmatch(expected):
        raise ValueError("installed_revision_unavailable")
    raw = importlib.metadata.distribution(distribution).read_text("direct_url.json")
    source = _json(raw or "{}")
    if (not isinstance(source, dict) or source.get("dir_info") or source.get("archive_info")
            or source.get("vcs_info", {}).get("vcs") != "git"
            or source.get("vcs_info", {}).get("commit_id") != expected):
        raise ValueError("installed_revision_mismatch")


def _load_runtime(policy: dict) -> SimpleNamespace:
    for package, revision in (("cn-equity-strategies", "code_revision"),
                              ("quant-platform-kit", "qpk_revision"), ("ai-gateway-client", "sdk_revision")):
        _installed_revision(package, policy[revision])
    from ai_gateway_client import AiGatewayClient, GatewayConfig
    from cn_equity_strategies.backtest import index_etf_research_job as cn
    from cn_equity_strategies.backtest.index_etf_strict_runner import IndexEtfExecutionConfig, read_index_etf_input
    from quant_platform_kit.strategy_lifecycle.codex_integration import AiOptimizationContext, build_optimization_prompt
    from quant_platform_kit.strategy_lifecycle.contracts import DriftResult, DriftStatus, PromotionCostModel, PurgedWalkForwardFold
    from quant_platform_kit.strategy_lifecycle.production_drift_health_probe import probe_production_drift_health
    return SimpleNamespace(cn=cn, client=AiGatewayClient, config=GatewayConfig,
        read_input=read_index_etf_input, execution_config=IndexEtfExecutionConfig, cost_model=PromotionCostModel,
        fold=PurgedWalkForwardFold, drift=DriftResult, status=DriftStatus, context=AiOptimizationContext,
        prompt=build_optimization_prompt, probe=probe_production_drift_health)


def _diagnosis(runtime, drift, revision):
    config = runtime.config.from_env()
    if config.research_providers != ("codex",):
        raise ValueError("codex_only_required")
    client = runtime.client(config)
    context = runtime.context(strategy_profile=PROFILE, domain="cn_equity", drift=drift,
                              current_params=runtime.cn.BASELINE_PARAMS)

    def diagnose(*_):
        result = client.execute(runtime.prompt(context), mode="review_only", research_stage="optimization",
            allowed_providers=["codex"], source_repository=STRATEGY_REPOSITORY, source_ref=revision, timeout=600)
        raw = result.raw
        if not result.success and isinstance(raw, dict) and raw.get("status") == "deferred":
            return {"optimization_needed": False, "reason": "codex_research_deferred", "retry_at": raw.get("retry_at")}
        if (result.success is not True or result.provider != "codex" or result.error or result.note
                or not isinstance(raw, dict) or raw.get("status") != "succeeded"):
            raise ValueError("research_model_outcome_unavailable")
        decision = _json(result.output)
        if (not isinstance(decision, dict) or type(decision.get("optimization_needed")) is not bool
                or (decision["optimization_needed"] and decision.get("recommended_method") != "grid_search")):
            raise ValueError("research_model_decision_invalid")
        return {"optimization_needed": decision["optimization_needed"], "recommended_method": "grid_search",
                "reason": "codex_research_decision", "provider": result.provider, "model": result.model,
                "reasoning_effort": raw["reasoning_effort"], "job_id": raw["job_id"]}
    return diagnose


def _summary(status: str, reason: str, **extra) -> dict:
    return {"status": status, "reason": reason, "no_order": True, "size_zero_required": True,
            "live_authority_granted": False, **extra}


def _make_shadow_reader(binding: dict, identity: dict, revision: str):
    """Read producer evidence only; never create receipts or start observation."""
    from quant_platform_kit.strategy_lifecycle.forward_observation import ForwardObservationPolicy
    from quant_platform_kit.strategy_lifecycle.paired_shadow_adapter import collect_paired_shadow_for_promotion

    if not isinstance(binding, dict) or not binding.get("observation_path") or not binding.get("forward_policy"):
        raise ValueError("shadow_provider_not_configured")
    interval = binding["retry_after_seconds"]
    if type(interval) is not int or not 60 <= interval <= 86400:
        raise ValueError("shadow_read_interval_invalid")

    policy = ForwardObservationPolicy(**binding["forward_policy"])
    if (policy.strategy_profile != PROFILE or policy.domain != "cn_equity"
            or policy.observation_calendar != "XSHG" or policy.observation_window_type != "fixed"
            or tuple(policy.automatic_non_live_modes) != ("shadow",)
            or tuple(policy.non_live_evidence_modes) != ("shadow_decision",)):
        raise ValueError("shadow_policy_invalid")
    calendar_path = Path(binding["calendar_path"])
    calendar_bytes = _read_bytes(calendar_path)
    calendar = _json(calendar_bytes)
    if hashlib.sha256(calendar_bytes).hexdigest() != binding["calendar_sha256"]:
        raise ValueError("shadow_calendar_mismatch")
    if not isinstance(calendar, list) or calendar != sorted(set(calendar)):
        raise ValueError("shadow_calendar_invalid")
    sessions = [date.fromisoformat(day).isoformat() for day in calendar]
    start = sessions.index(policy.observation_start_session)
    expected = sessions[start:start + policy.required_trading_sessions]
    if len(expected) != policy.required_trading_sessions:
        raise ValueError("shadow_calendar_incomplete")
    path = Path(binding["observation_path"])
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("shadow_source_invalid")
    frozen = binding["frozen_dependency_digests"]
    if (not isinstance(frozen, dict)
            or set(frozen) != {"p2_config", "p3_evidence", "risk_policy", "strategy_release", "plugin_bundle"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in frozen.values())
            or not isinstance(binding["baseline_id"], str) or not binding["baseline_id"].strip()
            or binding["baseline_id"] == policy.candidate_id):
        raise ValueError("shadow_source_dependencies_missing")

    def read(proposal):
        guard = {"passed": False, "no_order": True, "live_authority_granted": False}
        pending = {**guard, "status": "pending", "retry_at": _now().timestamp() + interval}
        try:
            # The existing CN next-open contract freezes decisions before 09:25.
            # A candidate created later cannot count that session as forward.
            if _timestamp(proposal.computed_at) >= _timestamp(expected[0] + "T09:25:00+08:00"):
                raise ValueError("shadow_candidate_created_after_window_start")
            if not path.exists():
                return pending
            payload = _read_json(path)
            if (payload["strategy_profile"] != PROFILE or payload["domain"] != "cn_equity"
                    or proposal.strategy_profile != PROFILE or proposal.domain != "cn_equity"
                    or payload["source_revision"] != revision or payload["research_identity"] != identity
                    or payload["current_params"] != dict(proposal.current_params)
                    or payload["proposed_params"] != dict(proposal.proposed_params)):
                raise ValueError("shadow_candidate_mismatch")
            observations = payload["observations"]
            if not isinstance(observations, list) or len(observations) > len(expected):
                raise ValueError("shadow_observation_count_invalid")
            previous_evidence = previous_receipt = None
            for index, raw in enumerate(observations, 1):
                receipt = raw["forward_observation_receipt"]
                if (receipt["observation_index"] != index or receipt["observation_session"] != expected[index - 1]
                        or raw["baseline_id"] != binding["baseline_id"]
                        or raw["input_snapshot_sha256"] != receipt["dependency_digests"]["p1_manifest"]
                        or any(receipt["dependency_digests"].get(key) != value for key, value in frozen.items())
                        or not _timestamp(proposal.computed_at) <= _timestamp(raw["observed_at"]) <= _now()
                        or _timestamp(raw["observed_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != expected[index - 1]
                        or _timestamp(raw["observed_at"]) < _timestamp(expected[index - 1] + "T15:00:00+08:00")):
                    raise ValueError("shadow_observation_binding_invalid")
                observation = {"policy": policy, **{key: raw[key] for key in (
                    "forward_observation_receipt", "baseline_id", "observed_at", "input_snapshot_sha256", "candidate", "baseline")},
                    "previous_evidence": previous_evidence, "previous_forward_observation_receipt": previous_receipt}
                record = collect_paired_shadow_for_promotion(observation)
                previous_evidence, previous_receipt = record["evidence"], receipt
            if len(observations) != policy.required_trading_sessions:
                return pending
            # QPK repeats the canonical paired validation before saving complete.
            return {"status": "complete", "observation": observation}
        except (ValueError, TypeError, KeyError, IndexError):
            return {**guard, "status": "failed", "reason": "shadow_observation_invalid"}
        except OSError:
            # An unreadable existing source is unknown, never a fresh attempt.
            raise ValueError("shadow_observation_unavailable") from None
    return read


def _console_bindings(binding: dict | None):
    from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import (
        make_console_research_promotion_pull, make_console_research_promotion_sync,
    )
    if not binding:
        raise ValueError("console_not_configured")
    from urllib.parse import urlsplit
    endpoints = [urlsplit(binding[key]) for key in ("sync_url", "pull_url")]
    if (any(url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment
            for url in endpoints) or endpoints[0].netloc != endpoints[1].netloc):
        raise ValueError("console_url_invalid")
    token_path = Path(binding["token_path"])
    _protected_file(token_path, secret=True)
    if token_path.stat().st_size > 4096:
        raise ValueError("console_token_unavailable")
    token = token_path.read_text().strip()
    if not token or "\n" in token or "\r" in token:
        raise ValueError("console_token_unavailable")
    pull = make_console_research_promotion_pull(endpoint_url=binding["pull_url"], sync_token=token,
                                               printer=lambda *_, **__: None, raise_on_unavailable=True)
    sync = make_console_research_promotion_sync(endpoint_url=binding["sync_url"], sync_token=token,
                                               pull_console=pull, printer=lambda *_, **__: None)
    return sync, pull


def run_from_watcher(watcher: dict, *, policy_path: Path = POLICY_PATH, dry_run: bool = False) -> dict:
    try:
        policy = _read_policy(policy_path)
    except FileNotFoundError:
        return _summary("parked", "cn_research_not_configured")
    except (ValueError, OSError):
        return _summary("parked", "cn_research_policy_invalid")
    if policy["enabled"] is not True:
        return _summary("parked", "cn_research_disabled")
    if not _workflow_authenticated():
        return _summary("parked", "cn_research_workflow_auth_required")
    try:
        # Old observations are passed unchanged only so QPK can locate a saved
        # shadow checkpoint. QPK still forbids creating/restarting stale research.
        task = _select_task(watcher, policy["code_revision"], _now(), allow_saved_shadow=True)
        drift = _read_drift(policy["drift"], _now(), allow_saved_shadow=True)
        runtime = _load_runtime(policy)
        if policy["candidate_id"] != PROFILE or policy["domain"] != "cn_equity":
            raise ValueError("policy_candidate_invalid")
        # The shared evaluator owns thresholds. Never derive score/date from a task.
        health = runtime.probe(strategy_profile=PROFILE, domain="cn_equity", as_of=drift["as_of"],
                               drift_score=drift["drift_score"])
        saved_shadow_only = health.get("reason") == "observation_stale"
        if ((not saved_shadow_only and health["actionable"] is not True)
                or (health.get("risk_status") if saved_shadow_only else health["status"]) != drift["status"]):
            raise ValueError("observation_not_actionable")
        inputs = {name: runtime.read_input(Path(policy["inputs"][name]["path"]),
                    expected_manifest_sha256=policy["inputs"][name]["manifest_sha256"])
                  for name in ("development", "validation")}
        plan = policy["plan"]
        arguments = dict(development_input=inputs["development"], validation_input=inputs["validation"],
            trusted_input_roots={name: policy["inputs"][name]["manifest_sha256"] for name in inputs},
            development_start=date.fromisoformat(plan["development_start"]),
            development_end=date.fromisoformat(plan["development_end"]),
            folds=tuple(runtime.fold(**{key: date.fromisoformat(value) for key, value in fold.items()}) for fold in plan["folds"]),
            locked_oos_start=date.fromisoformat(plan["locked_oos_start"]), locked_oos_end=date.fromisoformat(plan["locked_oos_end"]),
            purge_days=plan["purge_days"], embargo_days=plan["embargo_days"], code_revision=policy["code_revision"],
            config=runtime.execution_config(**policy["execution_config"]), cost_model=runtime.cost_model(**policy["cost_model"]))
        identity = runtime.cn.preflight_index_etf_research_job(**arguments)
        if set(identity) != _IDENTITY_FIELDS or identity != policy["research_identity"]:
            raise ValueError("frozen_research_identity_mismatch")
        if dry_run:
            return _summary("dry_run", "validated_without_execution", task_id=task["task_id"])
        shadow = _make_shadow_reader(policy["shadow"], identity, policy["code_revision"])
        sync, pull = _console_bindings(policy["console"])
        active_drift = runtime.drift(strategy_profile=PROFILE, domain="cn_equity",
            as_of=date.fromisoformat(drift["as_of"]), status=runtime.status(drift["status"]),
            drift_score=drift["drift_score"], source_revision=drift["source_revision"])
        model_diagnose = _diagnosis(runtime, active_drift, policy["code_revision"])
        window_start = _timestamp(policy["shadow"]["forward_policy"]["observation_start_session"] + "T09:25:00+08:00")

        def diagnose(*args):
            # A quota-deferred ticket may resume after the observation window
            # became impossible. Completed stages never call this again.
            if _now() >= window_start:
                return {"optimization_needed": False, "reason": "forward_window_start_elapsed"}
            return model_diagnose(*args)

        def admit_new(ticket_dir: Path, created_at: str) -> bool:
            # QPK calls this only for a new ticket, under its existing lock.
            # An existing pending/awaiting ticket keeps its original identity.
            return _timestamp(created_at) < window_start and admit_one_new_experiment(ticket_dir, created_at)

        result = runtime.cn.run_index_etf_research_job(**arguments,
            ticket_dir=STATE_ROOT / "research_promotion_tickets", store_root=STATE_ROOT,
            as_of=drift["as_of"], drift_score=drift["drift_score"], source_revision=drift["source_revision"],
            record_shadow=shadow, read_pending_shadow=shadow, sync_console=sync, pull_console=pull,
            diagnose=diagnose, admit_new_research=admit_new)
        return _summary(result["status"], result["reason"], task_id=task["task_id"],
            observation_as_of=drift["as_of"], observation_source_revision=drift["source_revision"],
            **{key: result[key] for key in ("research_key", "resumed", "console_synced", "retry_at") if key in result})
    except Exception:
        return _summary("parked", "cn_research_preflight_unavailable")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watcher-result", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_from_watcher(_read_json(Path(args.watcher_result)), dry_run=args.dry_run)
    except Exception:
        result = _summary("parked", "cn_research_watcher_unavailable")
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "dry_run" or "research_key" in result else 3


if __name__ == "__main__":
    raise SystemExit(main())
