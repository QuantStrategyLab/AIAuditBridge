"""Cursor account roster and operator-reviewed subscription research admission.

CLI discovery proves model availability, never remaining quota or paid usage.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from service.model_catalog import catalog_path, load_catalog

_STAGE_LEVELS = {'research_summary': 0, 'drift_analysis': 1, 'optimization': 2, 'promotion_review': 3}
_EFFORTS = ('low', 'medium', 'high', 'xhigh')


def _finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def cursor_research_route(payload: dict[str, Any], usage: dict[str, Any], *, now: float) -> dict[str, Any]:
    deferred = {'action': 'defer', 'provider': 'cursor', 'reason': 'cursor_research_unavailable'}
    if os.environ.get('AI_GATEWAY_CURSOR_ENABLED', '').lower() != 'true':
        return {**deferred, 'reason': 'cursor_disabled'}
    try:
        policy_path = Path(os.environ['AI_GATEWAY_CURSOR_POLICY_PATH'])
        if policy_path.is_symlink():
            return deferred
        policy = json.loads(policy_path.read_text())
        roster = load_catalog(catalog_path()).subscription_rosters.get('cursor', {})
    except (OSError, KeyError, TypeError, ValueError):
        return deferred
    return resolve_cursor_route(payload, usage, policy=policy, roster=roster, now=now)


def resolve_cursor_route(payload, usage, *, policy, roster, now):
    deferred = {'action': 'defer', 'provider': 'cursor', 'reason': 'cursor_research_unavailable'}
    if not isinstance(policy, dict) or not isinstance(roster, dict) or not _finite(now):
        return deferred
    until = policy.get('valid_until')
    if (policy.get('on_demand_disabled_verified') is not True or not _finite(until) or until <= now):
        return {**deferred, 'reason': 'cursor_spend_policy_unavailable'}
    updated = roster.get('updated_at')
    if (roster.get('status') != 'available' or roster.get('source') != 'cursor_cli_account'
        or not _finite(updated) or not 0 <= now - updated <= 86400):
        return {**deferred, 'reason': 'cursor_roster_stale'}
    daily = policy.get('max_daily_calls')
    used = usage.get('cursor_calls', 0)
    if type(daily) is not int or not 0 < daily <= 100 or type(used) is not int or used < 0 or used >= daily:
        return {**deferred, 'reason': 'cursor_capacity_unavailable'}
    stage = payload.get('research_stage')
    complexity = payload.get('complexity') or 'low'
    if stage not in _STAGE_LEVELS or complexity not in ('low', 'medium', 'high') or payload.get('mode', 'review_only') != 'review_only':
        return deferred
    # Same deterministic stage/complexity floors as Codex, without assuming
    # identically named models across subscriptions have equal quality.
    level = max(_STAGE_LEVELS[stage], ('low', 'medium', 'high').index(complexity))
    requested = payload.get('model') or ''
    effort = payload.get('reasoning_effort') or 'auto'
    if effort == 'auto':
        effort = _EFFORTS[level]
    if effort not in _EFFORTS or _EFFORTS.index(effort) < level:
        return deferred
    actual_models = roster.get('models')
    reviewed = policy.get('models')
    if not isinstance(actual_models, list) or not isinstance(reviewed, dict):
        return deferred
    candidates = []
    for model in actual_models:
        if (not isinstance(model, str) or model == 'auto'
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}', model)
            or (requested not in ('', 'auto') and requested != model)):
            continue
        spec = reviewed.get(model)
        if (not isinstance(spec, dict) or type(spec.get('quality_level')) is not int
            or not level <= spec['quality_level'] <= 3
            or not isinstance(spec.get('supported_reasoning_efforts'), list)
            or spec['supported_reasoning_efforts'] != [effort]):
            continue
        candidates.append((spec['quality_level'], model))
    if not candidates:
        return {**deferred, 'reason': 'cursor_model_capability_unavailable'}
    model = min(candidates)[1]
    return {'action': 'run', 'provider': 'cursor', 'model': model, 'reasoning_effort': effort}


def parse_cursor_models(output: str) -> list[str]:
    """Parse the account CLI's observed `id - label` roster, not API IDs."""
    output = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output)
    if len(output) > 1_000_000:
        raise ValueError('cursor model roster too large')
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or lines[0].rstrip(':') != 'Available models':
        raise ValueError('cursor model roster format unavailable')
    models = []
    for line in lines[1:]:
        if line.startswith('Tip:'):
            break
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9._-]{0,150}) - .+', line)
        if not match:
            raise ValueError('cursor model roster format unavailable')
        model = match[1]
        if model in models:
            raise ValueError('cursor model roster contains duplicates')
        models.append(model)
    if not models:
        raise ValueError('cursor model roster empty')
    return models


def discover_cursor_roster() -> dict[str, Any] | None:
    from service.adapters.codex_adapter import _codex_env
    executable = shutil.which(os.environ.get('AI_GATEWAY_CURSOR_BIN', 'agent'))
    if not executable:
        return None
    env = {k: v for k, v in _codex_env().items() if not k.startswith(('AI_GATEWAY_', 'CURSOR_'))}
    try:
        status = subprocess.run([executable, '--disable-auto-update', 'status'], capture_output=True, text=True, timeout=30, check=False, env=env)
        # Authentication is consumed only by the official CLI; account identity
        # and raw diagnostics are never copied into our catalog or logs.
        if status.returncode != 0 or not re.search(r'(?m)^\s*✓ Logged in as \S', re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', status.stdout)):
            return None
        result = subprocess.run([executable, '--disable-auto-update', 'models'], capture_output=True, text=True, timeout=30, check=False, env=env)
        if result.returncode != 0:
            return None
        models = parse_cursor_models(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return {'status': 'available', 'source': 'cursor_cli_account', 'updated_at': time.time(), 'models': models}
