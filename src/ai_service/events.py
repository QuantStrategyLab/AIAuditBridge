"""Durable MCP 2.0 webhook subscriptions for native Dot tasks."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .runtime import SubmissionRejected, _json
from .transport import PublicHTTPS, json_body


class CallbackEndpointError(ValueError):
    def __init__(self, reason):
        super().__init__('callback verification failed')
        self.reason = reason


def signing_key(secret):
    if not isinstance(secret, str) or not secret.startswith('whsec_'):
        raise ValueError('a Standard Webhooks signing secret is required')
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except ValueError:
        raise ValueError('invalid signing secret') from None
    if not 24 <= len(key) <= 64:
        raise ValueError('signing secret must decode to 24..64 bytes')
    return key


def signed_headers(subscription_id, secret, event_id, body, timestamp=None, *, previous_secret=None):
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    payload = event_id.encode() + b'.' + stamp.encode() + b'.' + body
    keys = [secret] + ([previous_secret] if previous_secret and previous_secret != secret else [])
    signatures = ['v1,' + base64.b64encode(hmac.new(signing_key(key), payload, hashlib.sha256).digest()).decode()
                  for key in keys]
    return {'Content-Type': 'application/json', 'webhook-id': event_id,
            'webhook-timestamp': stamp, 'webhook-signature': ' '.join(signatures),
            'X-MCP-Subscription-Id': subscription_id}


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')


class EventSubscriptions:
    name = 'task.requested'
    rotation_seconds = 60

    def __init__(self, runtime, *, provider_name='dot', transport=None, can_access=None, ttl_seconds=3600):
        self.runtime = runtime
        self.provider_name = provider_name
        self.transport = transport or PublicHTTPS()
        # Access must be rechecked from current server configuration, not old token claims.
        self.can_access = can_access or (lambda principal, owner, provider: False)
        self.ttl_seconds = ttl_seconds
        self._verified = {}
        self._subscription_lock = threading.Lock()
        self.max_subscriptions = 4
        self.max_pending_per_subscription = 128
        self.max_callbacks_per_flush = 4
        self.flush_budget_seconds = 5
        with runtime.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, owner TEXT NOT NULL,
                    url TEXT NOT NULL, secret TEXT NOT NULL, expires REAL NOT NULL,
                    start_seq INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS event_deliveries (
                    subscription TEXT NOT NULL, seq INTEGER NOT NULL, body BLOB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(subscription, seq));
                CREATE TABLE IF NOT EXISTS subscription_previous_keys (
                    subscription TEXT PRIMARY KEY, secret TEXT NOT NULL, expires REAL NOT NULL,
                    current_secret_digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS subscription_lifecycle (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, owner TEXT NOT NULL,
                    created REAL, expires REAL NOT NULL, deleted REAL);
            ''')
            self._prune(db, time.time())

    def catalog(self):
        return {'events': [{'name': self.name, 'description': 'A task was requested, updated or cancelled in this project.',
                            'delivery': ['webhook'],
                            'inputSchema': {'type': 'object', 'properties': {'project': {'type': 'string'}},
                                            'required': ['project'], 'additionalProperties': False},
                            'payloadSchema': {'type': 'object',
                                'properties': {'task_id': {'type': 'string'}, 'project': {'type': 'string'},
                                               'revision': {'type': 'integer'}, 'change': {'type': 'string'}},
                                'required': ['task_id', 'project', 'revision', 'change'], 'additionalProperties': False}}]}

    def subscribe(self, principal, params):
        # Serialize admission and challenge so concurrent callers cannot overbook.
        with self._subscription_lock:
            return self._subscribe(principal, params)

    @staticmethod
    def _prune(db, now):
        # Keep ownership and timestamps after pruning delivery state. Older writers
        # retain their original eight-column table; their creation time is unknown.
        db.execute('INSERT INTO subscription_lifecycle(id,principal,owner,expires) '
                   'SELECT id,principal,owner,expires FROM subscriptions WHERE 1 '
                   'ON CONFLICT(id) DO UPDATE SET expires=excluded.expires')
        db.execute("DELETE FROM event_deliveries WHERE subscription IN (SELECT id FROM subscriptions WHERE active=0 OR expires<=?)", (now,))
        db.execute("DELETE FROM subscriptions WHERE active=0 OR expires<=?", (now,))
        db.execute('DELETE FROM subscription_previous_keys WHERE expires<=? '
                   'OR subscription NOT IN (SELECT id FROM subscriptions)', (now,))

    def _subscribe(self, principal, params):
        if not isinstance(params, dict) or set(params) - {'name', 'arguments', 'delivery', 'cursor', 'ttlMs'}:
            raise ValueError('invalid subscription fields')
        args, delivery = params.get('arguments'), params.get('delivery')
        if params.get('name') != self.name or not isinstance(args, dict) or set(args) != {'project'}:
            raise ValueError('invalid event name or project filter')
        owner = args['project']
        if not isinstance(owner, str) or not self.can_access(principal, owner, self.provider_name):
            raise PermissionError('event access denied')
        if params.get('cursor') is not None:
            raise ValueError('this event does not support replay')
        if not isinstance(delivery, dict) or set(delivery) != {'mode', 'url', 'secret'} or delivery['mode'] != 'webhook':
            raise ValueError('webhook delivery is required')
        url, secret = delivery['url'], delivery['secret']
        if not isinstance(url, str):
            raise ValueError('callback URL is required')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('callback must be HTTPS without credentials or fragment')
        signing_key(secret)
        ttl = params.get('ttlMs')
        if ttl is None:
            # A no-expiration request still receives our finite default lifetime.
            ttl = self.ttl_seconds * 1000
        if type(ttl) is not int or ttl < 1000:
            raise ValueError('ttlMs must be an integer of at least 1000')
        expiry = time.time() + min(ttl / 1000, self.ttl_seconds)
        identity = _json([principal, url, self.name, args]).encode()
        subscription_id = 'sub_' + hashlib.sha256(identity).hexdigest()
        with self.runtime.connection() as db:
            self._prune(db, time.time())
            old = db.execute('SELECT id FROM subscriptions WHERE id=?', (subscription_id,)).fetchone()
            count = db.execute('SELECT COUNT(*) FROM subscriptions WHERE principal=? AND owner=?', (principal, owner)).fetchone()[0]
            if not old and count >= self.max_subscriptions:
                raise ValueError('subscription limit reached')
        challenge = secrets.token_urlsafe(32)
        event_id = 'msg_verification_' + secrets.token_hex(16)
        body = json_body({'type': 'verification', 'challenge': challenge})
        started = time.monotonic()
        cache_key = (principal, url, hashlib.sha256(secret.encode()).hexdigest())
        valid = self._verified.get(cache_key, 0) > started
        if not valid:
            try:
                reply = self.transport.request(url, body=body,
                    headers=signed_headers(subscription_id, secret, event_id, body))
                echoed = json.loads(reply.body).get('challenge')
                valid = (200 <= reply.status < 300 and isinstance(echoed, str)
                         and hmac.compare_digest(challenge, echoed) and time.monotonic() - started <= 30)
            except Exception:
                raise CallbackEndpointError('transport_failed') from None
        if not valid:
            raise CallbackEndpointError('challenge_failed')
        self._verified = {key: expiry for key, expiry in self._verified.items() if expiry > started}
        self._verified[cache_key] = time.monotonic() + 90
        # Check again after the network call; activation cannot race a revoked grant.
        if not self.can_access(principal, owner, self.provider_name):
            raise PermissionError('event access revoked')
        with self.runtime.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            self._prune(db, now)
            old = db.execute('SELECT active,start_seq,secret,expires FROM subscriptions WHERE id=?', (subscription_id,)).fetchone()
            start = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            if old and old['active'] and old['expires'] > now:
                start = old['start_seq']
                if old['secret'] != secret:
                    # At most the immediate previous key; never extend its original lifetime.
                    until = min(now + self.rotation_seconds, old['expires'], expiry)
                    db.execute('INSERT INTO subscription_previous_keys VALUES(?,?,?,?) '
                               'ON CONFLICT(subscription) DO UPDATE SET secret=excluded.secret,expires=excluded.expires,'
                               'current_secret_digest=excluded.current_secret_digest',
                               (subscription_id, old['secret'], until, hashlib.sha256(secret.encode()).hexdigest()))
                else:
                    # Same-key refresh preserves an existing deadline without adding a window.
                    db.execute('UPDATE subscription_previous_keys SET expires=MIN(expires,?) WHERE subscription=?',
                               (expiry, subscription_id))
            else:
                db.execute('DELETE FROM subscription_previous_keys WHERE subscription=?', (subscription_id,))
            # Refresh preserves queued deliveries; resubscribe after unsubscribe has no replay.
            if old and (not old['active'] or old['expires'] <= time.time()):
                db.execute('DELETE FROM event_deliveries WHERE subscription=?', (subscription_id,))
            db.execute('INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,1) '
                       'ON CONFLICT(id) DO UPDATE SET secret=excluded.secret,expires=excluded.expires,'
                       'active=1,start_seq=excluded.start_seq',
                       (subscription_id, principal, owner, url, secret, expiry, start))
            db.execute('INSERT INTO subscription_lifecycle VALUES(?,?,?,?,?,NULL) '
                       'ON CONFLICT(id) DO UPDATE SET created=CASE WHEN ? THEN subscription_lifecycle.created '
                       'ELSE excluded.created END,expires=excluded.expires,deleted=NULL',
                       (subscription_id, principal, owner, now, expiry, bool(old)))
        return {'id': subscription_id, 'refreshBefore': iso(expiry), 'cursor': None, 'truncated': False}

    def unsubscribe_request(self, principal, params):
        if not isinstance(params, dict) or set(params) != {'name', 'arguments', 'delivery'}:
            raise ValueError('invalid unsubscribe fields')
        args, delivery = params['arguments'], params['delivery']
        if params['name'] != self.name or not isinstance(args, dict) or set(args) != {'project'}:
            raise ValueError('invalid event name or project filter')
        owner = args['project']
        if not isinstance(owner, str) or not self.can_access(principal, owner, self.provider_name):
            raise PermissionError('event access denied')
        if (not isinstance(delivery, dict) or set(delivery) != {'mode', 'url'}
                or delivery['mode'] != 'webhook' or not isinstance(delivery['url'], str)):
            raise ValueError('webhook destination is required')
        identity = _json([principal, delivery['url'], self.name, args]).encode()
        subscription_id = 'sub_' + hashlib.sha256(identity).hexdigest()
        self.unsubscribe(principal, subscription_id, owner=owner)
        # Native MCP Events requires an empty, idempotent unsubscribe result.
        # Management tools expose the explicit gone state and deletion timestamps.
        return {}

    def _status(self, db, principal, subscription_id, owner):
        if not isinstance(subscription_id, str) or not 1 <= len(subscription_id) <= 200:
            raise ValueError('subscription ID is required (1..200 characters)')
        row = db.execute('SELECT l.*,s.active FROM subscription_lifecycle l '
                         'LEFT JOIN subscriptions s ON s.id=l.id WHERE l.id=?', (subscription_id,)).fetchone()
        if row and (row['principal'] != principal or (owner is not None and row['owner'] != owner)):
            raise PermissionError('subscription access denied')
        state = 'gone'
        if row and row['deleted'] is None:
            state = 'active' if (row['active'] and row['expires'] > time.time()
                and self.can_access(principal, row['owner'], self.provider_name)) else 'inactive'
        return {'id': subscription_id, 'status': state,
                'created_at': iso(row['created']) if row and row['created'] is not None else None,
                'expires_at': iso(row['expires']) if row else None,
                'deleted_at': iso(row['deleted']) if row and row['deleted'] is not None else None}

    def status(self, principal, subscription_id, *, owner=None):
        with self.runtime.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._prune(db, time.time())
            return self._status(db, principal, subscription_id, owner)

    def unsubscribe(self, principal, subscription_id, *, owner=None):
        # Wait for any in-flight send; no delivery can start after deletion returns.
        with self._subscription_lock, self.runtime.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._prune(db, time.time())
            record = self._status(db, principal, subscription_id, owner)
            deleted = record['status'] != 'gone'
            if deleted:
                db.execute('UPDATE subscription_lifecycle SET deleted=? WHERE id=?', (time.time(), subscription_id))
                db.execute('DELETE FROM subscriptions WHERE id=?', (subscription_id,))
                db.execute('DELETE FROM event_deliveries WHERE subscription=?', (subscription_id,))
                db.execute('DELETE FROM subscription_previous_keys WHERE subscription=?', (subscription_id,))
            return {**self._status(db, principal, subscription_id, owner), 'deleted': deleted}

    def has_active(self, owner):
        with self.runtime.connection() as db:
            rows = db.execute('SELECT * FROM subscriptions WHERE owner=? AND active=1 AND expires>?',
                              (owner, time.time())).fetchall()
        return any(self.can_access(row['principal'], owner, self.provider_name) for row in rows)

    def flush(self):
        """Called by the single service dispatcher, after runtime.tick()."""
        now = time.time()
        deadline = time.monotonic() + self.flush_budget_seconds
        with self.runtime.connection() as db:
            self._prune(db, now)
            db.execute("DELETE FROM event_deliveries WHERE status!='pending'")
            # Only events tied to this provider; no other project/provider notifications.
            subscriptions = db.execute('SELECT * FROM subscriptions WHERE active=1 AND expires>?', (now,)).fetchall()
            for sub in subscriptions:
                if not self.can_access(sub['principal'], sub['owner'], self.provider_name):
                    db.execute('UPDATE subscriptions SET active=0 WHERE id=?', (sub['id'],))
                    db.execute('DELETE FROM subscription_previous_keys WHERE subscription=?', (sub['id'],))
                    continue
                queued = db.execute("SELECT COUNT(*) FROM event_deliveries WHERE subscription=?", (sub['id'],)).fetchone()[0]
                capacity = self.max_pending_per_subscription - queued
                if capacity <= 0:
                    continue
                events = db.execute("SELECT e.seq,e.status,e.created,t.id,t.owner,t.revision FROM events e "
                    "JOIN tasks t ON t.id=e.task_id WHERE t.owner=? AND t.provider=? AND e.seq>? "
                    "AND e.status IN ('queued','message','cancel_requested') "
                    "AND t.status IN ('running','cancel_requested','outcome_unknown') ORDER BY e.seq LIMIT ?",
                    (sub['owner'], self.provider_name, sub['start_seq'], capacity)).fetchall()
                for event in events:
                    body = json_body({'eventId': 'evt_' + str(event['seq']), 'name': self.name,
                        'timestamp': iso(event['created']), 'cursor': None,
                        'data': {'task_id': event['id'], 'project': event['owner'],
                                 'revision': event['revision'], 'change': event['status']}})
                    db.execute('INSERT OR IGNORE INTO event_deliveries(subscription,seq,body) VALUES(?,?,?)',
                               (sub['id'], event['seq'], body))
                if events:
                    db.execute('UPDATE subscriptions SET start_seq=? WHERE id=?', (events[-1]['seq'], sub['id']))
            pending = db.execute("SELECT d.*,s.principal,s.owner,s.url,s.secret,s.expires,s.active "
                "FROM event_deliveries d JOIN subscriptions s ON s.id=d.subscription "
                "WHERE d.status='pending' AND d.next_attempt<=? ORDER BY d.next_attempt,d.seq LIMIT ?", (now, self.max_callbacks_per_flush)).fetchall()
        for row in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Serialize key selection/send against refresh, within the existing flush budget.
            if not self._subscription_lock.acquire(timeout=remaining):
                break
            status = 0
            try:
                with self.runtime.connection() as db:
                    current = db.execute('SELECT s.*,p.secret AS previous_secret,p.expires AS previous_expires,p.current_secret_digest '
                        'FROM subscriptions s LEFT JOIN subscription_previous_keys p ON p.subscription=s.id '
                        'WHERE s.id=?', (row['subscription'],)).fetchone()
                now = time.time()
                if (not current or not current['active'] or current['expires'] <= now
                        or not self.can_access(current['principal'], current['owner'], self.provider_name)):
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # An older rollback writer may change the current key while ignoring our small table.
                previous = current['previous_secret'] if ((current['previous_expires'] or 0) > now
                    and current['current_secret_digest'] == hashlib.sha256(current['secret'].encode()).hexdigest()) else None
                # Preserve notification ID/body on retry; execution claims still prevent duplicate work.
                event_id = 'evt_' + str(row['seq'])
                try:
                    reply = self.transport.request(current['url'], body=row['body'],
                        headers=signed_headers(row['subscription'], current['secret'], event_id, row['body'],
                                               previous_secret=previous),
                        timeout=min(2, remaining))
                    status = reply.status
                except Exception:
                    pass
            finally:
                self._subscription_lock.release()
            attempts = row['attempts'] + 1
            transient = status == 0 or status == 429 or status >= 500
            state = 'sent' if 200 <= status < 300 else 'pending' if transient and attempts < 5 else 'stopped'
            with self.runtime.connection() as db:
                db.execute('UPDATE event_deliveries SET status=?,attempts=?,next_attempt=? WHERE subscription=? AND seq=?',
                           (state, attempts, time.time() + min(2 ** attempts, 60), row['subscription'], row['seq']))


class DotEventProvider:
    mode = 'agent'
    callback_driven = True

    def __init__(self, subscriptions):
        self.subscriptions = subscriptions

    def submit(self, request, *, task_id, owner):
        if not self.subscriptions.has_active(owner):
            raise SubmissionRejected('no authorized active Dot subscription')
        # Durable runtime events are delivered by flush(); acceptance isn't task completion.
        return {'task_id': task_id, 'acceptance': 'event_pending', 'model_verification': 'unavailable'}

    def cancel(self, handle):
        return None
