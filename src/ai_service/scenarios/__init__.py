"""Offline scenario contracts; this module never dispatches or authorizes work."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from importlib.resources import files
import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource


class ContractError(ValueError):
    """An invalid contract; messages never include caller-supplied values."""


def schema(kind):
    """Read a bundled Draft 2020-12 schema, also available in installed wheels."""
    if kind not in {'instance', 'task'}:
        raise ContractError('unknown scenario schema')
    return json.loads(files(__package__).joinpath(f'scenario-{kind}.v1.schema.json').read_text(encoding='utf-8'))


def _validate(value, kind):
    documents = [schema(name) for name in ('instance', 'task')]
    # No retrieval hook: only the two installed resources can resolve references.
    registry = Registry().with_resources((doc['$id'], Resource.from_contents(doc)) for doc in documents)
    try:
        Draft202012Validator(documents[1 if kind == 'task' else 0], registry=registry).validate(value)
    except ValidationError:
        raise ContractError('scenario contract does not match its schema') from None


def _copy_json(value):
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, OverflowError):
        raise ContractError('scenario configuration must contain finite JSON values') from None


def _defaults(value, definition, root):
    if '$ref' in definition:
        target = definition['$ref']
        if not target.startswith('#/$defs/'):
            raise ContractError('default schema reference is not local')
        definition = root['$defs'][target.rsplit('/', 1)[1]]
    if isinstance(value, dict):
        for key, child in definition.get('properties', {}).items():
            if key not in value and 'default' in child:
                value[key] = copy.deepcopy(child['default'])
            if key in value:
                _defaults(value[key], child, root)
    elif isinstance(value, list) and 'items' in definition:
        for item in value:
            _defaults(item, definition['items'], root)


def load_instance(value):
    """Apply safe defaults, then check fixed template structure and semantics."""
    value = _copy_json(value)
    # API stages have a tighter default deadline than assistant stages.
    if isinstance(value, dict) and isinstance(value.get('stages'), list):
        for stage in value['stages']:
            if isinstance(stage, dict) and stage.get('worker_type') == 'api':
                limits = stage.setdefault('limits', {})
                if isinstance(limits, dict):
                    limits.setdefault('timeout_seconds', 60)
    document = schema('instance')
    _defaults(value, document, document)
    _validate(value, 'instance')
    seen = set()
    for stage in value['stages']:
        identity = stage['phase'], stage['role']
        if identity in seen:
            raise ContractError('duplicate scenario phase and role')
        seen.add(identity)
    try:
        ZoneInfo(value['schedule']['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        raise ContractError('scenario timezone is not available') from None
    if value['enabled']:
        approvals = value['approvals']
        if not approvals['activation_ref']:
            raise ContractError('enabled instances require an existing activation reference')
        if value['inputs']['data_classification'] == 'restricted' and not approvals['data_use_ref']:
            raise ContractError('restricted inputs require an existing data-use reference')
        if any(stage['limits']['monthly_budget_usd'] > 0 for stage in value['stages']) and not approvals['cost_ref']:
            raise ContractError('nonzero budgets require an existing cost reference')
    return value


def operation_key(task):
    """Bind one operation to its frozen configuration, occurrence and role."""
    identity = [task[name] for name in ('project', 'instance_id', 'config_revision', 'occurrence', 'phase', 'role')]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return 'scn:' + hashlib.sha256(encoded).hexdigest()


def _binding(instance, phase, role):
    if not instance['enabled']:
        raise ContractError('scenario instance is disabled')
    stage = next((s for s in instance['stages'] if (s['phase'], s['role']) == (phase, role)), None)
    if stage is None:
        raise ContractError('scenario phase and role are not configured')
    return {
        **{name: instance[name] for name in ('project', 'template', 'template_version', 'config_revision', 'inputs', 'executor_ref', 'gate_refs')},
        **stage,
        'schema_version': 'scenario.task.v1', 'instance_id': instance['id'],
        'output_schema_ref': instance['output']['schema_ref'], 'sink_ref': instance['output']['sink_ref'],
        'approval_refs': sorted({ref for ref in instance['approvals'].values() if ref is not None}),
    }


def validate_task_spec(value, *, instance=None):
    """Check a task and optionally bind every configuration field to its producer."""
    value = _copy_json(value)
    _validate(value, 'task')
    try:
        deadline = datetime.fromisoformat(value['deadline'].replace('Z', '+00:00'))
        if deadline.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ContractError('task deadline requires an ISO timestamp with a timezone') from None
    if value['operation_key'] != operation_key(value):
        raise ContractError('task operation key does not match its identity')
    if value['worker_type'] == 'api':
        if value['limits']['monthly_budget_usd'] <= 0 or not value['approval_refs']:
            raise ContractError('API task requires a positive approved budget')
    if instance is not None:
        expected = _binding(load_instance(instance), value['phase'], value['role'])
        if any(value[name] != item for name, item in expected.items()):
            raise ContractError('task does not match its configured instance and stage')
    return value


def compile_task_spec(instance, *, occurrence, phase, role, input_manifest_sha256, submitted_at):
    """Build a frozen contract, without submitting tasks or reserving API budget."""
    instance = load_instance(instance)
    task = _copy_json(_binding(instance, phase, role))
    if not isinstance(submitted_at, datetime) or submitted_at.utcoffset() is None:
        raise ContractError('submission time requires an explicit timezone')
    deadline = submitted_at + timedelta(seconds=task['limits']['timeout_seconds'])
    task.update(occurrence=occurrence, input_manifest_sha256=input_manifest_sha256,
                deadline=deadline.isoformat().replace('+00:00', 'Z'))
    task['operation_key'] = operation_key(task)
    return validate_task_spec(task, instance=instance)


@dataclass(frozen=True)
class ServiceConfiguration:
    """Legacy service values and opt-in instances, kept separate from startup."""
    service: dict
    instances: tuple[dict, ...]


def load_service_config(source):
    """Read legacy JSON unchanged, or an explicit scenario.service.v1 envelope.

    The existing startup validator remains responsible for service/auth policy.
    Loading never starts a scheduler, reads credentials or dispatches tasks.
    """
    if isinstance(source, (str, Path)):
        try:
            source = json.loads(Path(source).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ContractError('service configuration cannot be read') from None
    source = _copy_json(source)
    if not isinstance(source, dict):
        raise ContractError('service configuration must be an object')
    if 'schema_version' not in source:
        return ServiceConfiguration(source, ())
    if (source.get('schema_version') != 'scenario.service.v1'
            or set(source) != {'schema_version', 'service', 'instances'}
            or not isinstance(source['service'], dict) or not isinstance(source['instances'], list)):
        raise ContractError('unsupported service configuration envelope')
    instances = tuple(load_instance(value) for value in source['instances'])
    identities = [(value['project'], value['id']) for value in instances]
    if len(set(identities)) != len(identities):
        raise ContractError('duplicate scenario instance in project')
    return ServiceConfiguration(source['service'], instances)
