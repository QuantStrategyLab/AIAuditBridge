"""Manual pilot caller. Dry-run is offline; credentials stay in the approved client."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path

from ai_service.client import client_from_env
from . import ContractError, load_instance
from .pilot import digest, domain_for, prepare, submit, verify


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['dry-run', 'submit', 'verify'])
    parser.add_argument('--instance', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--occurrence', required=True)
    parser.add_argument('--submitted-at', required=True)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--task-id')
    parser.add_argument('--provider', default='dot')
    args = parser.parse_args()
    try:
        instance = load_instance(json.loads(args.instance.read_text()))
        manifest = json.loads(args.input.read_text())
        domain_for(instance, manifest).result_contract(manifest)
        if args.command == 'dry-run' and not instance['enabled']:
            print(json.dumps({'dry_run': True, 'enabled': False, 'dispatchable': False,
                              'input_sha256': digest(manifest)}))
            return 0
        task = prepare(instance, manifest, occurrence=args.occurrence,
                       submitted_at=datetime.fromisoformat(args.submitted_at.replace('Z', '+00:00')))
        if args.command == 'dry-run':
            result = {'dry_run': True, 'operation_key': task.spec['operation_key'],
                      'request_sha256': digest(task.request)}
        elif args.command == 'submit':
            if not args.receipt:
                raise ContractError('an exclusive receipt path is required')
            # Write intent BEFORE sending. Unknown remote outcomes retain this key.
            write_private(args.receipt, {'status': 'intent', 'operation_key': task.spec['operation_key'],
                                        'request_sha256': digest(task.request)})
            record = submit(client_from_env(), task)
            result = {'status': record['status'], 'task_id': record['id'],
                      'operation_key': task.spec['operation_key']}
            write_private(Path(str(args.receipt) + '.submitted'), result)
        else:
            if not args.task_id:
                raise ContractError('reuse the original task id')
            record = client_from_env().get(args.task_id)
            result = verify(task, record, task_id=args.task_id, provider=args.provider)
            if args.receipt:
                write_private(args.receipt, result)
        print(json.dumps(result))
        return 0
    except Exception:
        # Never echo raw service responses, material or credential exceptions.
        print(json.dumps({'status': 'stopped', 'reason': 'pilot operation rejected; inspect private receipt; do not resubmit with a new key'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
