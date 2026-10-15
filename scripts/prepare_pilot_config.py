#!/usr/bin/env python3
"""Offline candidate preparation, dry-run by default. Never replaces live config."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat

from ai_service.auth import project_belongs_to
from ai_service.scenarios import load_instance
from ai_service.server import validate_configuration


def private_json(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('private regular file required')
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def build(config, instance, grant, *, owner, project, worker_identity, activation_ref, revision):
    candidate = copy.deepcopy(config)
    if candidate.get('owner') != owner or not project_belongs_to(project, owner):
        raise ValueError('exact owner and project required')
    workers = [g for g in candidate['grants'] if g['identity'] == worker_identity]
    if (len(workers) != 1 or workers[0].get('owner') != owner
            or set(workers[0]['scopes']) != {'tasks:read', 'tasks:work', 'events:subscribe'}):
        raise ValueError('existing owner-wide Dot events worker required')
    worker = workers[0]
    provider = worker.get('provider')
    if candidate['providers'].get(provider, {}).get('type') != 'dot_events':
        raise ValueError('worker must use Dot events')
    if (grant.get('project') != project or 'projects' in grant or 'owner' in grant
            or 'provider' in grant or set(grant.get('scopes', [])) != {'tasks:read', 'tasks:submit'}
            or grant.get('identity') == worker_identity
            or not isinstance(grant.get('claims'), dict)
            or not grant['claims'].get('azp') and grant.get('issuer') != 'https://token.actions.githubusercontent.com'):
        raise ValueError('separate exact-project connection-bound submitter required')
    matches = [g for g in candidate['grants'] if g['identity'] == grant.get('identity')]
    if matches and matches != [grant]:
        raise ValueError('existing identity must not be overwritten')
    if not matches:
        candidate['grants'].append(copy.deepcopy(grant))
    activated = load_instance(instance)
    from ai_service.scenarios.pilot_domain import DATA_CLASSIFICATIONS
    if activated['enabled'] or activated['inputs']['data_classification'] not in DATA_CLASSIFICATIONS:
        raise ValueError('start from a disabled pilot instance with allowed input classification')
    activated.update(project=project, enabled=True, config_revision=revision)
    activated['approvals']['activation_ref'] = activation_ref
    activated = load_instance(activated)
    from ai_service.scenarios.pilot_domain import TEMPLATE
    if (activated['template'] != TEMPLATE or len(activated['stages']) != 1
            or activated['stages'][0]['worker_type'] != 'dot'):
        raise ValueError('one configured pilot Dot stage required')
    stage = activated['stages'][0]
    route = 'agent' if stage['route_ref'] == 'default' else 'agent:' + stage['route_ref']
    policy = candidate['projects'].get(project)
    if policy is None:
        candidate['projects'][project] = {'providers': {route: provider},
            'models': {provider: [stage['model_ref']]}, 'max_active': 1,
            'max_timeout_seconds': stage['limits']['timeout_seconds']}
    elif (policy['providers'].get(route) != provider
          or stage['model_ref'] not in policy['models'].get(provider, [])
          or stage['limits']['timeout_seconds'] > policy.get('max_timeout_seconds', 3600)):
        raise ValueError('existing project policy does not match pilot; do not overwrite')
    validate_configuration(candidate)
    try:
        from quant_ai_service.server import validate_configuration as quant_guard
    except ImportError:
        quant_guard = None
    if quant_guard:
        quant_guard(candidate)
    return candidate, activated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['config', 'instance', 'submitter-grant', 'output-config', 'output-instance']:
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ['expected-config-sha256', 'owner', 'project', 'worker-identity', 'activation-ref', 'revision']:
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--write-candidates', action='store_true')
    args = parser.parse_args()
    try:
        config, current_digest = private_json(args.config)
        if current_digest != args.expected_config_sha256:
            raise ValueError('live config changed since review')
        instance = json.loads(args.instance.read_text())
        grant, _ = private_json(args.submitter_grant)
        candidate, activated = build(config, instance, grant, owner=args.owner, project=args.project,
            worker_identity=args.worker_identity, activation_ref=args.activation_ref, revision=args.revision)
        paths = [args.output_config, args.output_instance]
        if any(path.exists() or path.is_symlink() for path in paths) or paths[0].absolute() == paths[1].absolute():
            raise ValueError('new distinct candidate paths required')
        payloads = [encode(candidate), encode(activated)]
        if args.write_candidates:
            for path, raw in zip(paths, payloads):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
        print(json.dumps({'dry_run': not args.write_candidates, 'live_config_changed': False,
            'candidate_config_sha256': hashlib.sha256(payloads[0]).hexdigest(),
            'candidate_instance_sha256': hashlib.sha256(payloads[1]).hexdigest()}))
        return 0
    except Exception:
        print(json.dumps({'status': 'rejected', 'live_config_changed': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
