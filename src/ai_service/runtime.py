"""Durable, project-scoped tasks. Business decisions belong to callers."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError


class Conflict(ValueError):
    pass


class NotFound(LookupError):
    pass


class SubmissionRejected(ValueError):
    """Provider has positively confirmed that no external run started."""


@dataclass(frozen=True)
class Policy:
    providers: dict[str, str]
    models: dict[str, set[str]]
    max_active: int = 4
    max_timeout_seconds: int = 3600
    max_request_bytes: int = 2_000_000


@dataclass(frozen=True)
class ProviderReply:
    status: str
    output: Any = None
    artifacts: list[dict] = field(default_factory=list)


TERMINAL = {'completed', 'failed', 'invalid_output', 'cancelled'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _check_schema(schema):
    if not isinstance(schema, dict):
        raise ValueError('output_schema must be an object')
    # The validator must never fetch caller-selected URLs.
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'$ref', '$dynamicRef'} and (not isinstance(item, str) or not item.startswith('#')):
                    raise ValueError('external schema references are forbidden')
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(schema)
    Draft202012Validator.check_schema(schema)


class TaskRuntime:
    def __init__(self, path: Path, providers: dict, policies: dict[str, Policy]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError('state database must not be a symbolic link')
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        self.providers = providers
        self.policies = policies
        # A private state directory is required; never use a public temp directory.
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, idem TEXT NOT NULL,
                    digest TEXT NOT NULL, provider TEXT NOT NULL, request TEXT NOT NULL,
                    status TEXT NOT NULL, handle TEXT, output TEXT, artifacts TEXT NOT NULL DEFAULT '[]',
                    error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    deadline REAL NOT NULL, cancel_sent INTEGER NOT NULL DEFAULT 0,
                    worker TEXT, attempt TEXT, revision INTEGER NOT NULL DEFAULT 1, report_digest TEXT,
                    UNIQUE(owner, idem));
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    task_id TEXT NOT NULL, idem TEXT NOT NULL, body TEXT NOT NULL,
                    revision INTEGER NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(task_id, idem));
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _record(self, row):
        result = dict(row)
        for key in ('request', 'handle', 'output', 'artifacts'):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        for key in ('idem', 'digest', 'cancel_sent', 'report_digest'):
            result.pop(key, None)
        result['result_kind'] = 'advisory'
        return result

    def _state(self, db, task_id, status, **fields):
        fields.update(status=status, updated=time.time())
        db.execute('UPDATE tasks SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE id=?', (*fields.values(), task_id))
        db.execute('INSERT INTO events(task_id,status,created) VALUES(?,?,?)', (task_id, status, time.time()))

    def submit(self, owner, idempotency_key, request):
        policy = self.policies.get(owner)
        if policy is None:
            raise PermissionError('project is not admitted')
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise ValueError('Idempotency-Key is required (1..200 characters)')
        allowed = {'mode', 'profile', 'objective', 'model', 'materials', 'output_schema', 'timeout_seconds'}
        if not isinstance(request, dict) or set(request) - allowed:
            raise ValueError('unknown task fields')
        if not {'mode', 'objective', 'model', 'output_schema'} <= set(request):
            raise ValueError('mode, objective, model and output_schema are required')
        if not isinstance(request['objective'], str) or not request['objective'].strip():
            raise ValueError('objective is required')
        mode, model = request['mode'], request['model']
        if not isinstance(mode, str) or not isinstance(model, str):
            raise ValueError('mode and model must be strings')
        profile = request.get('profile', 'default')
        if not isinstance(profile, str) or not profile or len(profile) > 100 or ':' in profile:
            raise ValueError('profile must be a nonempty configured profile name')
        route = mode if profile == 'default' else mode + ':' + profile
        provider_name = policy.providers.get(route)
        provider = self.providers.get(provider_name)
        if mode not in {'api', 'agent'} or provider is None or provider.mode != mode:
            raise PermissionError('execution mode is unavailable')
        if model not in policy.models.get(provider_name, set()):
            raise PermissionError('model is not admitted')
        timeout = request.get('timeout_seconds', policy.max_timeout_seconds)
        if type(timeout) is not int or not 1 <= timeout <= policy.max_timeout_seconds:
            raise ValueError('timeout_seconds exceeds project policy')
        if not isinstance(request.get('materials', []), list):
            raise ValueError('materials must be an array')
        _check_schema(request['output_schema'])
        body = _json(request)
        if len(body.encode()) > policy.max_request_bytes:
            raise ValueError('request too large')
        digest = hashlib.sha256(body.encode()).hexdigest()
        now, task_id = time.time(), str(uuid.uuid4())
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM tasks WHERE owner=? AND idem=?', (owner, idempotency_key)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise Conflict('idempotency key already binds a different request')
                return self._record(old)
            active = db.execute("SELECT COUNT(*) FROM tasks WHERE owner=? AND status NOT IN ('completed','failed','invalid_output','cancelled')", (owner,)).fetchone()[0]
            if active >= policy.max_active:
                raise Conflict('project active task quota exhausted')
            db.execute('INSERT INTO tasks(id,owner,idem,digest,provider,request,status,created,updated,deadline) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (task_id, owner, idempotency_key, digest, provider_name, body, 'queued', now, now, now + timeout))
            db.execute('INSERT INTO events(task_id,status,created) VALUES(?,?,?)', (task_id, 'queued', now))
        return self.get(owner, task_id)

    def get(self, owner, task_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=? AND owner=?', (task_id, owner)).fetchone()
            if row is None:
                raise NotFound('task not found')
            return self._record(row)

    def claim(self, owner, task_id, provider_name, worker, attempt_id):
        if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 200:
            raise ValueError('a stable attempt_id is required')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._agent_row(db, owner, task_id, provider_name)
            if row['attempt'] and (row['worker'], row['attempt']) != (worker, attempt_id):
                raise Conflict('task already belongs to another execution attempt')
            if not row['attempt']:
                if row['status'] not in {'submitting', 'running', 'outcome_unknown'}:
                    raise Conflict('task cannot be claimed in its current state')
                if time.time() >= row['deadline']:
                    raise Conflict('task deadline passed')
                self._state(db, task_id, 'running', worker=worker, attempt=attempt_id, error=None)
        record = self.get(owner, task_id)
        return {**record, 'attempt_id': attempt_id, 'messages': self.messages(owner, task_id)}

    def _agent_row(self, db, owner, task_id, provider_name):
        row = db.execute('SELECT * FROM tasks WHERE id=? AND owner=?', (task_id, owner)).fetchone()
        if row is None:
            raise NotFound('task not found')
        provider = self.providers.get(row['provider'])
        if row['provider'] != provider_name or not getattr(provider, 'callback_driven', False):
            raise PermissionError('worker is not assigned to this task')
        return row

    def report(self, owner, task_id, provider_name, worker, attempt_id, revision, status, output=None, artifacts=None):
        if status not in {'completed', 'failed', 'cancelled'}:
            raise ValueError('report status must be completed, failed or cancelled')
        artifacts = [] if artifacts is None else artifacts
        if not isinstance(artifacts, list):
            raise ValueError('artifacts must be an array')
        body = _json({'revision': revision, 'status': status, 'output': output, 'artifacts': artifacts})
        if len(body.encode()) > self.policies[owner].max_request_bytes:
            raise ValueError('report too large')
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._agent_row(db, owner, task_id, provider_name)
            if not row['attempt'] or (row['worker'], row['attempt']) != (worker, attempt_id):
                raise PermissionError('report does not match the task owner and attempt')
            if type(revision) is not int or revision != row['revision']:
                raise Conflict('materials changed; read the current revision before reporting')
            if row['report_digest']:
                if row['report_digest'] != digest:
                    raise Conflict('a different final report already exists')
                return self._record(row)
            if row['status'] not in {'running', 'cancel_requested'}:
                raise Conflict('task is not accepting results')
            error = None
            if status == 'completed':
                try:
                    Draft202012Validator(json.loads(row['request'])['output_schema']).validate(output)
                except (ValidationError, ValueError, TypeError, RecursionError):
                    status, output, error = 'invalid_output', None, 'output_schema_mismatch'
            self._state(db, task_id, status, output=_json(output), artifacts=_json(artifacts),
                        report_digest=digest, error=error)
        return self.get(owner, task_id)

    def message(self, owner, task_id, idempotency_key, message):
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise ValueError('Idempotency-Key is required')
        if not isinstance(message, dict) or set(message) - {'text', 'materials'} or not isinstance(message.get('text'), str):
            raise ValueError('message requires text and optional materials')
        if not isinstance(message.get('materials', []), list):
            raise ValueError('materials must be an array')
        body = _json(message)
        if len(body.encode()) > self.policies[owner].max_request_bytes:
            raise ValueError('message too large')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE owner=? AND id=?', (owner, task_id)).fetchone()
            if row is None:
                raise NotFound('task not found')
            old = db.execute('SELECT body FROM messages WHERE task_id=? AND idem=?', (task_id, idempotency_key)).fetchone()
            if old:
                if old['body'] != body:
                    raise Conflict('message key binds different content')
            else:
                if row['status'] in TERMINAL | {'cancel_requested'} or json.loads(row['request'])['mode'] != 'agent':
                    raise Conflict('task does not accept follow-ups')
                revision = row['revision'] + 1
                db.execute('INSERT INTO messages(task_id,idem,body,revision,created) VALUES(?,?,?,?,?)',
                           (task_id, idempotency_key, body, revision, time.time()))
                db.execute('UPDATE tasks SET revision=?,updated=? WHERE id=?', (revision, time.time(), task_id))
                db.execute('INSERT INTO events(task_id,status,created) VALUES(?,?,?)', (task_id, 'message', time.time()))
        return self.get(owner, task_id)

    def messages(self, owner, task_id):
        self.get(owner, task_id)
        with self.connection() as db:
            return [{'revision': row['revision'], 'created': row['created'], **json.loads(row['body'])}
                    for row in db.execute('SELECT * FROM messages WHERE task_id=? ORDER BY revision', (task_id,))]

    def events(self, owner, task_id, after=0):
        self.get(owner, task_id)
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT seq,status,created FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 100', (task_id, after))]

    def cancel(self, owner, task_id):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=? AND owner=?', (task_id, owner)).fetchone()
            if row is None:
                raise NotFound('task not found')
            if row['status'] == 'queued':
                self._state(db, task_id, 'cancelled')
            elif row['status'] not in TERMINAL | {'outcome_unknown', 'cancel_requested'}:
                self._state(db, task_id, 'cancel_requested')
        return self.get(owner, task_id)

    def tick(self):
        # One dispatcher per database, including across processes. A restart can
        # safely classify abandoned submissions only after owning this lock.
        lock_path = self.path.with_suffix('.worker.lock')
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            with self.connection() as db:
                abandoned = db.execute("SELECT id FROM tasks WHERE attempt IS NULL AND (status='submitting' OR (status='cancel_requested' AND handle IS NULL))").fetchall()
                for row in abandoned:
                    self._state(db, row['id'], 'outcome_unknown', error='acceptance_unknown')
                rows = db.execute("SELECT * FROM tasks WHERE status IN ('queued','running','cancel_requested') ORDER BY created").fetchall()
            for row in rows:
                self._advance(row)
        finally:
            os.close(fd)

    def _advance(self, row):
        task_id, owner = row['id'], row['owner']
        # Re-read after concurrent cancellation. Removed policy blocks new work,
        # while existing handles still need polling/cancellation.
        current = self.get(owner, task_id)
        provider = self.providers.get(current['provider'])
        if provider is None:
            return
        if current['status'] == 'queued':
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                state = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
                if state != 'queued':
                    return
                if time.time() >= current['deadline']:
                    self._state(db, task_id, 'cancelled', error='deadline_before_submission')
                    return
                if owner not in self.policies:
                    self._state(db, task_id, 'failed', error='project_policy_removed')
                    return
                self._state(db, task_id, 'submitting')
            try:
                handle = provider.submit(current['request'], task_id=task_id, owner=owner)
                if not isinstance(handle, dict) or not handle:
                    raise ValueError('invalid provider handle')
                serialized = _json(handle)
            except SubmissionRejected:
                with self.connection() as db:
                    state = db.execute('SELECT status,attempt FROM tasks WHERE id=?', (task_id,)).fetchone()
                    if state['status'] in {'submitting', 'cancel_requested'} and not state['attempt']:
                        self._state(db, task_id, 'failed', error='submission_rejected')
                return
            except Exception:
                with self.connection() as db:
                    state = db.execute('SELECT status,attempt FROM tasks WHERE id=?', (task_id,)).fetchone()
                    if state['status'] in {'submitting', 'cancel_requested'} and not state['attempt']:
                        self._state(db, task_id, 'outcome_unknown', error='acceptance_unknown')
                return
            with self.connection() as db:
                state = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
                if state in {'submitting', 'cancel_requested'}:
                    self._state(db, task_id, 'cancel_requested' if state == 'cancel_requested' else 'running', handle=serialized)
                else:
                    db.execute('UPDATE tasks SET handle=? WHERE id=?', (serialized, task_id))
            return
        if current['status'] not in {'running', 'cancel_requested'}:
            return
        if time.time() >= current['deadline'] and current['status'] == 'running':
            self.cancel(owner, task_id)
            current = self.get(owner, task_id)
        if current['status'] == 'cancel_requested' and not row['cancel_sent']:
            # Record before sending. A lost cancellation response is not proof
            # of cancellation and is not replayed automatically.
            with self.connection() as db:
                db.execute('UPDATE tasks SET cancel_sent=1 WHERE id=?', (task_id,))
            try:
                provider.cancel(current['handle'])
            except Exception:
                pass
        if getattr(provider, 'callback_driven', False):
            return  # Native Bot results arrive through authenticated tools.
        try:
            reply = provider.poll(current['handle'])
        except Exception:
            return  # Read failures may retry; submissions never do.
        if reply.status not in TERMINAL:
            return
        status, output, error = reply.status, None, None
        if status == 'completed':
            try:
                Draft202012Validator(current['request']['output_schema']).validate(reply.output)
                output = _json(reply.output)
            except (ValidationError, ValueError, TypeError, RecursionError):
                status, error = 'invalid_output', 'output_schema_mismatch'
        if status == 'failed':
            error = 'provider_run_failed'
        with self.connection() as db:
            self._state(db, task_id, status, output=output, artifacts=_json(reply.artifacts), error=error)
