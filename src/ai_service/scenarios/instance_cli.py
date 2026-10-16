"""Control one independently approved instance; never edit service authorization."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from . import ContractError, load_instance


class ControlError(ValueError):
    """A fixed diagnostic that cannot disclose private file contents."""


def _require(condition, reason):
    if not condition:
        raise ControlError(reason)


def _external(path):
    _require(path.is_absolute() and path.resolve() == path, 'absolute non-symlink paths required')
    for parent in (path, *path.parents):
        _require(not (parent / '.git').exists(), 'repository paths are not managed instances')
        _require(parent.name.lower() not in {'examples', 'fixtures', 'tests', 'testdata', 'offline'},
                 'fixture paths are not managed instances')


def _private_stat(info, *, directory=False):
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
             and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
             and (directory or info.st_nlink == 1), 'managed paths must be private and owned by the operator')


@contextmanager
def _private_file(path, flags, *, create=False):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_CREAT if create else 0), 0o600)
    try:
        _private_stat(os.fstat(fd))
        yield fd
    finally:
        os.close(fd)


def _read(path):
    with _private_file(path, os.O_RDONLY) as fd:
        with os.fdopen(os.dup(fd), 'rb') as source:
            return source.read()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append(fd, record):
    raw = (json.dumps(record, ensure_ascii=True, sort_keys=True) + '\n').encode()
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError('audit write failed')
        view = view[written:]
    os.fsync(fd)


def _replace(path, raw):
    fd, temporary = tempfile.mkstemp(prefix='.instance-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def control(action, *, state_dir, instance, project, instance_id, activation_ref, expected_sha256=None):
    """Toggle only enabled, under a lock, with a durable intent and actual readback.

    The operator supplies an existing approved instance. Matching a reference is
    not independent verification of its external approval; no approval is minted.
    """
    _require(action in {'readback', 'enable', 'disable'}, 'unsupported instance action')
    root, path = Path(state_dir), Path(instance)
    _external(root)
    _external(path)
    _require(path.parent == root and path.suffix == '.json', 'instance must be a direct JSON child of state directory')
    _private_stat(root.stat(), directory=True)
    _require(isinstance(activation_ref, str)
             and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', activation_ref) is not None,
             'explicit activation reference required')
    _require(re.search(r'(^|[_.:-])(offline|fixture|fixtures|example|sample|test|tests|dry[_.:-]?run)([_.:-]|$)',
                       activation_ref, re.IGNORECASE) is None, 'offline approvals are not accepted')
    if action != 'readback':
        _require(isinstance(expected_sha256, str)
                 and re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is not None,
                 'mutations require the expected instance SHA256')
    with _private_file(root / '.instance-control.lock', os.O_RDWR, create=True) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = _read(path)
        try:
            original = json.loads(raw)
            normalized = load_instance(original)
        except (ValueError, TypeError, KeyError):
            raise ControlError('invalid scenario instance') from None
        _require(normalized['project'] == project and normalized['id'] == instance_id,
                 'exact project and instance ID do not match')
        _require(normalized['approvals']['activation_ref'] == activation_ref,
                 'activation reference does not match the existing approved instance')
        _require(normalized['config_revision'] != '0' * 40, 'unreviewed fixture revision is not accepted')
        before = _sha(raw)
        _require(expected_sha256 is None or before == expected_sha256, 'instance SHA256 changed; read back before retrying')
        record = {'event': 'scenario_instance_control', 'action': action, 'project': project,
                  'instance_id': instance_id, 'activation_ref': activation_ref, 'uid': os.geteuid(),
                  'at': datetime.now(timezone.utc).isoformat(), 'before_sha256': before}
        with _private_file(root / 'instance-audit.jsonl', os.O_RDWR | os.O_APPEND, create=True) as audit:
            size = os.fstat(audit).st_size
            _require(not size or os.pread(audit, 1, size - 1) == b'\n',
                     'audit has an incomplete record; operator review required')
            if action != 'readback':
                changed = dict(original, enabled=action == 'enable')
                load_instance(changed)
                new_raw = (json.dumps(changed, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
                _append(audit, {**record, 'status': 'intent', 'intended_sha256': _sha(new_raw),
                                'enabled': changed['enabled']})
                _sync_directory(root)
                # Only cooperating operators may write this private directory.
                _require(_read(path) == raw, 'instance changed after intent; inspect audit before retrying')
                _replace(path, new_raw)
                raw = _read(path)
                _require(raw == new_raw, 'mutation outcome unknown; read back before retrying')
            result = {**record, 'status': 'readback', 'instance_sha256': _sha(raw),
                      'enabled': json.loads(raw).get('enabled', False),
                      'config_revision': normalized['config_revision'], 'approval_reference_matched': True,
                      'external_approval_verified': False}
            _append(audit, result)
            _sync_directory(root)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['readback', 'enable', 'disable'])
    for name in ('state-dir', 'instance', 'project', 'instance-id', 'activation-ref'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--expected-sha256', help='Required for enable/disable; use the last readback digest')
    args = parser.parse_args(argv)
    try:
        result = control(**vars(args))
    except ControlError as error:
        print(json.dumps({'status': 'stopped', 'reason': str(error)}))
        return 1
    except (OSError, ContractError, ValueError, TypeError):
        # A final audit failure can occur after replacement. Never claim rollback.
        print(json.dumps({'status': 'stopped', 'reason': 'instance control failed; read back state and inspect audit before retrying'}))
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
