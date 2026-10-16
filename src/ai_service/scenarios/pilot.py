"""Opt-in pilot caller: frozen ScenarioTaskSpec inside the unchanged V2 envelope."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json

from jsonschema import Draft202012Validator, ValidationError

from . import ContractError, compile_task_spec, load_instance


def digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    except (ValueError, TypeError):
        raise ContractError('pilot requires finite JSON') from None
    return hashlib.sha256(raw).hexdigest()


def object_schema(properties):
    return {'type': 'object', 'additionalProperties': False, 'required': list(properties), 'properties': properties}


@dataclass(frozen=True)
class PilotTask:
    spec: dict
    request: dict


def domain_for(instance, manifest):
    """Keep the existing series fixture and the fixed text probe independently validated."""
    from . import pilot_domain, protocol_probe
    if instance['template'] != protocol_probe.TEMPLATE:
        raise ContractError('unsupported pilot template')
    if isinstance(manifest, dict) and manifest.get('schema_version') == 'synthetic.protocol-input.v1':
        return pilot_domain
    return protocol_probe


def prepare(instance, manifest, *, occurrence, submitted_at):
    """Compile an already approved instance. Disabled samples cannot submit."""
    instance = load_instance(instance)
    domain = domain_for(instance, manifest)
    if (len(instance['stages']) != 1
            or instance['stages'][0]['worker_type'] != 'dot'
            or instance['inputs']['data_classification'] not in domain.DATA_CLASSIFICATIONS):
        raise ContractError('pilot requires its single Dot stage and allowed input classification')
    result_schema = domain.result_contract(manifest)
    stage = instance['stages'][0]
    spec = compile_task_spec(instance, occurrence=occurrence, phase=stage['phase'], role=stage['role'],
                             input_manifest_sha256=digest(manifest), submitted_at=submitted_at)
    output_schema = object_schema({
        'schema_version': {'const': 'scenario.pilot-result.v1'},
        'operation_key': {'const': spec['operation_key']},
        'input_manifest_sha256': {'const': spec['input_manifest_sha256']},
        'result': result_schema,
    })
    # JSON round-trip takes a snapshot: later edits to caller inputs cannot change it.
    request = json.loads(json.dumps({
        'mode': 'agent', 'profile': stage['route_ref'], 'model': stage['model_ref'],
        'objective': domain.OBJECTIVE, 'materials': [{'scenario_task': spec, 'input_manifest': manifest}],
        'output_schema': output_schema, 'timeout_seconds': stage['limits']['timeout_seconds'],
    }, allow_nan=False))
    if len(json.dumps(request, ensure_ascii=False).encode()) > stage['limits']['max_request_bytes']:
        raise ContractError('pilot request exceeds instance limit')
    return PilotTask(spec, request)


def submit(client, task):
    """One submission only. On an unknown outcome retain the key; never invent another."""
    deadline = datetime.fromisoformat(task.spec['deadline'].replace('Z', '+00:00'))
    if datetime.now(timezone.utc) >= deadline:
        raise ContractError('pilot submission deadline passed')
    return client.submit(task.request, task.spec['operation_key'])


def verify(task, record, *, task_id, provider):
    """Verify caller readback, including exact request, attempt, revision and result."""
    if (record.get('id') != task_id or record.get('owner') != task.spec['project']
            or record.get('provider') != provider or record.get('status') != 'completed'
            or record.get('result_kind') != 'advisory' or not record.get('attempt')
            or not record.get('worker') or record.get('revision') != 1
            or digest(record.get('request')) != digest(task.request)):
        raise ContractError('pilot readback does not match its submitted operation')
    try:
        Draft202012Validator(task.request['output_schema']).validate(record.get('output'))
    except ValidationError:
        raise ContractError('pilot result does not match its frozen output contract') from None
    return {'task_id': task_id, 'operation_key': task.spec['operation_key'],
            'input_manifest_sha256': task.spec['input_manifest_sha256'],
            'output_sha256': digest(record['output']), 'provider': provider,
            'attempt': record['attempt'], 'revision': record['revision'], 'status': 'verified'}
