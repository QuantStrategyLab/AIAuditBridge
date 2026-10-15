import base64
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from ai_service.events import CallbackEndpointError, DotEventProvider, EventSubscriptions, signed_headers
from ai_service.runtime import Policy, TaskRuntime
from ai_service.transport import HttpReply


class Callback:
    def __init__(self):
        self.calls, self.echo, self.status = [], True, 204

    def request(self, url, **kwargs):
        payload = json.loads(kwargs['body'])
        self.calls.append((url, payload, kwargs))
        if payload.get('type') == 'verification':
            return HttpReply(200, json.dumps({'challenge': payload['challenge'] if self.echo else 'wrong'}).encode())
        return HttpReply(self.status, b'')


class DotEventsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.transport = Callback()
        self.allowed = True
        self.runtime = TaskRuntime(Path(tmp.name) / 'state.sqlite', {},
            {'owner/repo': Policy({'agent': 'dot'}, {'dot': {'dot-configured'}})})
        self.events = EventSubscriptions(self.runtime, transport=self.transport,
            can_access=lambda principal, owner, provider: self.allowed and principal == 'worker' and owner == 'owner/repo' and provider == 'dot')
        self.runtime.providers['dot'] = DotEventProvider(self.events)
        self.secret = 'whsec_' + base64.b64encode(b'x' * 32).decode()
        self.params = {'name': 'task.requested', 'arguments': {'project': 'owner/repo'},
                       'delivery': {'mode': 'webhook', 'url': 'https://callback.example/mcp', 'secret': self.secret},
                       'cursor': None}
        self.request = {'mode': 'agent', 'objective': 'private request', 'materials': ['sensitive'],
                        'model': 'dot-configured', 'output_schema': {'type': 'object'}}

    def test_subscription_quota_preserves_refresh_and_rejects_before_challenge(self):
        from concurrent.futures import ThreadPoolExecutor
        def subscribe(index):
            params = {**self.params, 'delivery': {**self.params['delivery'], 'url': f'https://callback.example/{index}'}}
            try:
                return self.events.subscribe('worker', params)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(subscribe, range(8)))
        self.assertEqual(sum(result is not None for result in results), 4)
        self.assertEqual(len(self.transport.calls), 4)
        first = next(i for i, result in enumerate(results) if result is not None)
        self.assertIsNotNone(subscribe(first))
        with self.runtime.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0], 4)
            db.execute('UPDATE subscriptions SET expires=0')
        self.assertIsNotNone(subscribe(99))
        with self.runtime.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0], 1)

    def test_delivery_backlog_is_bounded_and_drains_without_replaying_sent_events(self):
        self.events.max_pending_per_subscription = 2
        self.events.max_callbacks_per_flush = 1
        self.events.subscribe('worker', self.params)
        task = self.runtime.submit('owner/repo', 'one', self.request)
        self.runtime.tick()
        for index in range(6):
            self.runtime.message('owner/repo', task['id'], str(index), {'text': 'synthetic update'})
        self.transport.status = 503
        self.events.flush()
        with self.runtime.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_deliveries').fetchone()[0], 2)
        self.transport.status = 204
        self.transport.calls.clear()
        for _ in range(10):
            with self.runtime.connection() as db:
                db.execute('UPDATE event_deliveries SET next_attempt=0')
            self.events.flush()
        notifications = [call for call in self.transport.calls if 'eventId' in call[1]]
        self.assertEqual(len(notifications), 7)
        self.assertEqual(len({call[1]['eventId'] for call in notifications}), 7)
        self.assertTrue(all(0 < call[2]['timeout'] <= 2 for call in notifications))
        with self.runtime.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_deliveries').fetchone()[0], 0)

    def test_expired_flush_budget_leaves_notification_pending_for_next_round(self):
        from unittest.mock import patch
        self.events.subscribe('worker', self.params)
        self.runtime.submit('owner/repo', 'one', self.request)
        self.runtime.tick()
        count = len(self.transport.calls)
        with patch('ai_service.events.time.monotonic', side_effect=[0, 6]):
            self.events.flush()
        self.assertEqual(len(self.transport.calls), count)
        self.events.flush()
        self.assertEqual(len(self.transport.calls), count + 1)

    def test_signed_bytes_match_standard_webhooks_construction(self):
        body = b'{"unicode":"\xe4\xb8\xad"}'
        headers = signed_headers('sub_1', self.secret, 'evt_1', body, 1234)
        expected = base64.b64encode(hmac.new(b'x' * 32, b'evt_1.1234.' + body, hashlib.sha256).digest()).decode()
        self.assertEqual(headers['webhook-signature'], 'v1,' + expected)

    def refresh_secret(self, byte):
        secret = 'whsec_' + base64.b64encode(byte * 32).decode()
        return {**self.params, 'delivery': {**self.params['delivery'], 'secret': secret}}

    def assert_delivery_keys(self, *keys):
        _, payload, request = self.transport.calls[-1]
        self.assertIn('eventId', payload)
        headers = request['headers']
        signed = (headers['webhook-id'] + '.' + headers['webhook-timestamp'] + '.').encode() + request['body']
        expected = ['v1,' + base64.b64encode(hmac.new(key * 32, signed, hashlib.sha256).digest()).decode() for key in keys]
        self.assertEqual(headers['webhook-signature'].split(' '), expected)

    def queue_delivery(self):
        self.runtime.submit('owner/repo', 'rotation-case', self.request)
        self.runtime.tick()
        self.transport.status = 503

    def retry_delivery(self, events=None):
        with self.runtime.connection() as db:
            db.execute('UPDATE event_deliveries SET next_attempt=0')
        (events or self.events).flush()

    def test_refresh_rotates_delivery_keys_for_same_subscription(self):
        with patch('ai_service.events.time.time', return_value=1000):
            first = self.events.subscribe('worker', self.params)
            self.queue_delivery()
            self.events.flush()
            self.assert_delivery_keys(b'x')
        with patch('ai_service.events.time.time', return_value=1010):
            refreshed = self.events.subscribe('worker', self.refresh_secret(b'y'))
            self.assertEqual(refreshed['id'], first['id'])
            # New-key callback must verify before application delivery can rotate.
            challenge = self.transport.calls[-1]
            self.assertEqual(challenge[1]['type'], 'verification')
            self.assertEqual(len(challenge[2]['headers']['webhook-signature'].split(' ')), 1)
            self.retry_delivery()
            self.assert_delivery_keys(b'y', b'x')
            with self.runtime.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0], 1)

    def test_same_key_refresh_cannot_extend_rotation_and_deadline_is_exclusive(self):
        with patch('ai_service.events.time.time', return_value=1000):
            self.events.subscribe('worker', self.params)
            self.queue_delivery()
            self.events.subscribe('worker', self.refresh_secret(b'y'))
        with patch('ai_service.events.time.time', return_value=1059):
            self.events.subscribe('worker', self.refresh_secret(b'y'))
            self.retry_delivery()
            self.assert_delivery_keys(b'y', b'x')
        with patch('ai_service.events.time.time', return_value=1060):
            self.retry_delivery()
            self.assert_delivery_keys(b'y')
            with self.runtime.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM subscription_previous_keys').fetchone()[0], 0)

    def test_consecutive_refresh_retains_only_immediately_previous_key(self):
        with patch('ai_service.events.time.time', return_value=1000):
            self.events.subscribe('worker', self.params)
            self.queue_delivery()
            self.events.subscribe('worker', self.refresh_secret(b'y'))
        with patch('ai_service.events.time.time', return_value=1010):
            self.events.subscribe('worker', self.refresh_secret(b'z'))
            self.retry_delivery()
            self.assert_delivery_keys(b'z', b'y')
            with self.runtime.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM subscription_previous_keys').fetchone()[0], 1)
        with patch('ai_service.events.time.time', return_value=1070):
            self.retry_delivery()
            self.assert_delivery_keys(b'z')

    def test_rotation_cannot_outlive_either_subscription_expiry(self):
        with patch('ai_service.events.time.time', return_value=1000):
            self.events.subscribe('worker', {**self.params, 'ttlMs': 3000})
            self.queue_delivery()
        with patch('ai_service.events.time.time', return_value=1001):
            self.events.subscribe('worker', self.refresh_secret(b'y'))
            self.retry_delivery()
            self.assert_delivery_keys(b'y', b'x')
        with patch('ai_service.events.time.time', return_value=1003):
            self.retry_delivery()
            self.assert_delivery_keys(b'y')
            # A later shortened same-key grant may not retain a previous key longer.
            self.events.subscribe('worker', self.refresh_secret(b'z'))
            self.events.subscribe('worker', {**self.refresh_secret(b'z'), 'ttlMs': 1000})
        with patch('ai_service.events.time.time', return_value=1004):
            count = len(self.transport.calls)
            self.retry_delivery()
            self.assertEqual(len(self.transport.calls), count)
            self.assertFalse(self.events.has_active('owner/repo'))

    def test_restart_keeps_rotation_deadline_and_legacy_subscription_schema(self):
        with patch('ai_service.events.time.time', return_value=1000):
            self.events.subscribe('worker', self.params)
            self.queue_delivery()
            self.events.flush()
            body = self.transport.calls[-1][2]['body']
            # Existing 2.0.6 schema: migrate without altering its positional INSERT.
            with self.runtime.connection() as db:
                db.execute('DROP TABLE IF EXISTS subscription_previous_keys')
            migrated = EventSubscriptions(self.runtime, transport=self.transport, can_access=self.events.can_access)
            migrated.subscribe('worker', self.refresh_secret(b'y'))
        with patch('ai_service.events.time.time', return_value=1030):
            restarted = EventSubscriptions(self.runtime, transport=self.transport, can_access=self.events.can_access)
            self.retry_delivery(restarted)
            self.assert_delivery_keys(b'y', b'x')
            self.assertEqual(self.transport.calls[-1][2]['body'], body)
            with self.runtime.connection() as db:
                db.execute('INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,1)',
                           ('legacy-case', 'worker', 'owner/repo', 'https://callback.example/legacy', self.secret, 4000, 0))
                self.assertEqual(len(db.execute('PRAGMA table_info(subscriptions)').fetchall()), 8)
        with patch('ai_service.events.time.time', return_value=1060):
            self.retry_delivery(restarted)
            # Inspect the original pending subscription, not the extra legacy fixture.
            original = next(call for call in self.transport.calls[::-1]
                            if call[1].get('eventId') and call[0] == self.params['delivery']['url'])
            saved = self.transport.calls
            self.transport.calls = [original]
            self.assert_delivery_keys(b'y')
            self.transport.calls = saved

    def test_legacy_writer_key_change_cannot_reuse_stale_rotation_after_restart(self):
        with patch('ai_service.events.time.time', return_value=1000):
            sub = self.events.subscribe('worker', self.params)
            self.queue_delivery()
            self.events.subscribe('worker', self.refresh_secret(b'y'))
            with self.runtime.connection() as db:
                # 2.0.6 updates only its original table during a rollback.
                db.execute('UPDATE subscriptions SET secret=? WHERE id=?',
                           (self.refresh_secret(b'z')['delivery']['secret'], sub['id']))
            restarted = EventSubscriptions(self.runtime, transport=self.transport, can_access=self.events.can_access)
            self.retry_delivery(restarted)
            self.assert_delivery_keys(b'z')

    def test_refresh_contention_keeps_delivery_pending_with_original_budget(self):
        with patch('ai_service.events.time.time', return_value=1000):
            self.events.subscribe('worker', self.params)
            self.queue_delivery()
            count = len(self.transport.calls)
            from unittest.mock import Mock
            busy = Mock()
            busy.acquire.return_value = False
            with patch.object(self.events, '_subscription_lock', busy):
                self.events.flush()
            self.assertEqual(len(self.transport.calls), count)
            self.assertTrue(0 < busy.acquire.call_args.kwargs['timeout'] <= self.events.flush_budget_seconds)
            with self.runtime.connection() as db:
                row = db.execute('SELECT status,attempts FROM event_deliveries').fetchone()
            self.assertEqual((row['status'], row['attempts']), ('pending', 0))
            self.retry_delivery()
            self.assert_delivery_keys(b'x')

    def test_failed_challenge_or_revoked_refresh_cannot_replace_keys(self):
        with patch('ai_service.events.time.time', return_value=1000):
            self.events.subscribe('worker', self.params)
            self.queue_delivery()
            self.transport.echo = False
            with self.assertRaises(CallbackEndpointError):
                self.events.subscribe('worker', self.refresh_secret(b'y'))
            self.transport.echo = True
            callback = self.transport.request
            def revoke_during_callback(url, **kwargs):
                reply = callback(url, **kwargs)
                self.allowed = False
                return reply
            with patch.object(self.transport, 'request', side_effect=revoke_during_callback):
                with self.assertRaises(PermissionError):
                    self.events.subscribe('worker', self.refresh_secret(b'z'))
            self.allowed = True
            self.retry_delivery()
            self.assert_delivery_keys(b'x')
            with self.runtime.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM subscription_previous_keys').fetchone()[0], 0)

    def test_rotated_subscription_unsubscribe_revocation_and_resubscribe(self):
        with patch('ai_service.events.time.time', return_value=1000):
            first = self.events.subscribe('worker', self.params)
            self.queue_delivery()
            self.events.subscribe('worker', self.refresh_secret(b'y'))
            with self.assertRaises(PermissionError):
                self.events.unsubscribe('other-worker', first['id'])
            self.retry_delivery()
            self.assert_delivery_keys(b'y', b'x')
            self.events.unsubscribe('worker', first['id'])
            count = len(self.transport.calls)
            self.retry_delivery()
            self.assertEqual(len(self.transport.calls), count)
            self.events.subscribe('worker', self.refresh_secret(b'z'))
            self.assertEqual(len(self.transport.calls[-1][2]['headers']['webhook-signature'].split(' ')), 1)
            with self.runtime.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM subscription_previous_keys').fetchone()[0], 0)
            self.allowed = False
            self.retry_delivery()
            self.assertFalse(self.events.has_active('owner/repo'))

    def test_standard_initialization_preserves_dot_discovery(self):
        from ai_service.auth import Principal
        from ai_service.server import Application
        principal = Principal('worker', 'owner/repo', frozenset({'tasks:work', 'events:subscribe'}), 'dot')
        app = Application(self.runtime, None, self.events)
        initialized = app.mcp(principal, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                       'clientInfo': {'name': 'test-client', 'version': '1'}}})
        self.assertEqual(initialized['result']['capabilities'], {'tools': {}})
        discovery = app.mcp(principal, {'jsonrpc': '2.0', 'id': 2, 'method': 'server/discover'})
        self.assertEqual(discovery['result'], {'resultType': 'complete',
            'supportedVersions': ['2026-07-28'], 'capabilities': {'tools': {}, 'events': {}}})
        self.assertIn('result', app.mcp(principal, {'jsonrpc': '2.0', 'id': 3, 'method': 'events/list'}))

    def test_native_mcp_metadata_allows_subscribe_and_unsubscribe(self):
        from ai_service.auth import Principal
        from ai_service.server import Application
        principal = Principal('worker', 'owner/repo', frozenset({'events:subscribe'}), 'dot')
        app = Application(self.runtime, None, self.events)
        metadata = {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}
        reply = app.mcp(principal, {'jsonrpc': '2.0', 'id': 1, 'method': 'events/subscribe',
                                   'params': {**self.params, '_meta': metadata}})
        self.assertTrue(self.events.has_active('owner/repo'))
        self.assertIn('id', reply['result'])
        reply = app.mcp(principal, {'jsonrpc': '2.0', 'id': 2, 'method': 'events/unsubscribe',
                                   'params': {'name': self.params['name'], 'arguments': self.params['arguments'],
                                              'delivery': {'mode': 'webhook', 'url': self.params['delivery']['url']},
                                              '_meta': metadata}})
        self.assertEqual(reply['result'], {})
        self.assertFalse(self.events.has_active('owner/repo'))
        self.assertEqual(app.mcp(principal, {'jsonrpc': '2.0', 'id': 3, 'method': 'events/unsubscribe',
            'params': {'name': self.params['name'], 'arguments': self.params['arguments'],
                       'delivery': {'mode': 'webhook', 'url': self.params['delivery']['url']}}})['result'], {})

    def test_unsubscribe_rejects_other_project_and_invalid_fields(self):
        from ai_service.auth import Principal
        from ai_service.server import Application
        principal = Principal('worker', 'owner/repo', frozenset({'events:subscribe'}), 'dot')
        app = Application(self.runtime, None, self.events)
        self.events.subscribe('worker', self.params)
        valid = {'name': self.params['name'], 'arguments': self.params['arguments'],
                 'delivery': {'mode': 'webhook', 'url': self.params['delivery']['url']}}
        for params in [dict(valid, arguments={'project': 'other/repo'}),
                       dict(valid, name='other.event'), dict(valid, delivery={'mode': 'webhook'}),
                       dict(valid, id='spoofed'), dict(valid, delivery={**valid['delivery'], 'secret': self.secret})]:
            reply = app.mcp(principal, {'jsonrpc': '2.0', 'id': 4, 'method': 'events/unsubscribe', 'params': params})
            self.assertIn('error', reply)
            self.assertTrue(self.events.has_active('owner/repo'))

    def test_callback_must_echo_challenge_before_subscription_is_active(self):
        self.transport.echo = False
        with self.assertRaises(CallbackEndpointError):
            self.events.subscribe('worker', self.params)
        self.assertFalse(self.events.has_active('owner/repo'))
        self.transport.echo = True
        result = self.events.subscribe('worker', self.params)
        self.assertTrue(self.events.has_active('owner/repo'))
        again = self.events.subscribe('worker', self.params)
        self.assertEqual(result['id'], again['id'])
        self.assertIsNone(result['cursor'])

    def test_null_ttl_grants_finite_default_and_refreshes_same_subscription(self):
        from unittest.mock import patch
        self.events.ttl_seconds = 60
        params = {**self.params, 'ttlMs': None}
        with patch('ai_service.events.time.time', return_value=1000):
            first = self.events.subscribe('worker', params)
            self.assertEqual(first['refreshBefore'], '1970-01-01T00:17:40Z')
            self.assertTrue(self.events.has_active('owner/repo'))
        with patch('ai_service.events.time.time', return_value=1005):
            refreshed = self.events.subscribe('worker', params)
            self.assertEqual(refreshed['id'], first['id'])
            self.assertEqual(refreshed['refreshBefore'], '1970-01-01T00:17:45Z')
            with self.runtime.connection() as db:
                rows = db.execute('SELECT active,expires FROM subscriptions').fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['active'], rows[0]['expires']), (1, 1065))
        self.assertEqual(len(self.transport.calls), 1)

    def test_non_null_invalid_ttl_still_fails_before_callback(self):
        for ttl in (True, False, 0, -1, 999, 1000.0, '1000'):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.events.subscribe('worker', {**self.params, 'ttlMs': ttl})
        self.assertEqual(self.transport.calls, [])
        with self.runtime.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0], 0)

    def test_unadmitted_filter_and_invalid_secret_fail_before_network(self):
        for params in [dict(self.params, arguments={'project': 'other/repo'}),
                       dict(self.params, delivery={**self.params['delivery'], 'secret': 'whsec_short'}),
                       dict(self.params, cursor='replay-not-supported')]:
            with self.assertRaises((ValueError, PermissionError)):
                self.events.subscribe('worker', params)
        self.assertEqual(self.transport.calls, [])

    def test_durable_delivery_contains_ids_only_and_preserves_id_on_retry(self):
        self.events.subscribe('worker', self.params)
        task = self.runtime.submit('owner/repo', 'one', self.request)
        self.runtime.tick()
        self.transport.status = 503
        self.events.flush()
        first = self.transport.calls[-1]
        self.assertNotIn('sensitive', str(first[1]))
        self.assertNotIn('private request', str(first[1]))
        self.assertEqual(first[2]['headers']['webhook-id'], first[1]['eventId'])
        with self.runtime.connection() as db:
            db.execute('UPDATE event_deliveries SET next_attempt=0')
        restarted = EventSubscriptions(self.runtime, transport=self.transport, can_access=self.events.can_access)
        self.transport.status = 204
        restarted.flush()
        self.assertEqual(first[1], self.transport.calls[-1][1])
        count = len(self.transport.calls)
        restarted.flush()
        self.assertEqual(len(self.transport.calls), count)
        self.assertEqual(self.runtime.get('owner/repo', task['id'])['status'], 'running')

    def test_unsubscribe_and_access_revocation_stop_delivery(self):
        sub = self.events.subscribe('worker', self.params)
        self.runtime.submit('owner/repo', 'one', self.request)
        self.runtime.tick()
        with self.assertRaises(PermissionError):
            self.events.unsubscribe('other-worker', sub['id'])
        self.assertTrue(self.events.has_active('owner/repo'))
        self.events.unsubscribe('worker', sub['id'])
        self.events.unsubscribe('worker', sub['id'])
        count = len(self.transport.calls)
        self.events.flush()
        self.assertEqual(len(self.transport.calls), count)
        self.events.subscribe('worker', self.params)
        self.allowed = False
        count = len(self.transport.calls)
        self.events.flush()
        self.assertEqual(len(self.transport.calls), count)
        self.assertFalse(self.events.has_active('owner/repo'))

    def test_expired_subscription_cannot_start_a_task(self):
        self.events.subscribe('worker', self.params)
        with self.runtime.connection() as db:
            db.execute('UPDATE subscriptions SET expires=?', (time.time() - 1,))
        task = self.runtime.submit('owner/repo', 'one', self.request)
        self.runtime.tick()
        self.assertEqual(self.runtime.get('owner/repo', task['id'])['status'], 'failed')

    def test_410_and_413_are_not_retried(self):
        self.events.subscribe('worker', self.params)
        for status in [410, 413]:
            self.runtime.submit('owner/repo', str(status), self.request)
            self.runtime.tick()
            self.transport.status = status
            self.events.flush()
            count = len(self.transport.calls)
            self.events.flush()
            self.assertEqual(len(self.transport.calls), count)

    def test_management_status_refresh_delete_and_unknown_timestamps(self):
        with patch('ai_service.events.time.time', return_value=1000):
            sub = self.events.subscribe('worker', {**self.params, 'ttlMs': 5000})
            active = self.events.status('worker', sub['id'], owner='owner/repo')
        self.assertEqual(active, {'id': sub['id'], 'status': 'active',
            'created_at': '1970-01-01T00:16:40Z', 'expires_at': '1970-01-01T00:16:45Z', 'deleted_at': None})
        with patch('ai_service.events.time.time', return_value=1001):
            self.events.subscribe('worker', {**self.params, 'ttlMs': 5000})
            refreshed = self.events.status('worker', sub['id'])
            self.assertEqual(refreshed['created_at'], active['created_at'])
            self.assertEqual(refreshed['expires_at'], '1970-01-01T00:16:46Z')
            deleted = self.events.unsubscribe('worker', sub['id'])
        self.assertEqual(deleted['status'], 'gone')
        self.assertTrue(deleted['deleted'])
        self.assertEqual(deleted['deleted_at'], '1970-01-01T00:16:41Z')
        with patch('ai_service.events.time.time', return_value=1002):
            self.assertEqual(self.events.unsubscribe('worker', sub['id']), {**deleted, 'deleted': False})
            restarted = EventSubscriptions(self.runtime, transport=self.transport, can_access=self.events.can_access)
            self.assertEqual(restarted.status('worker', sub['id']),
                             {key: value for key, value in deleted.items() if key != 'deleted'})
            unknown = restarted.unsubscribe('worker', 'never-existed')
            self.assertEqual(unknown, {'id': 'never-existed', 'status': 'gone', 'deleted': False,
                                      'created_at': None, 'expires_at': None, 'deleted_at': None})
            # A later explicit resubscribe creates a new lifetime for the same identity.
            self.assertEqual(restarted.subscribe('worker', self.params)['id'], sub['id'])
            recreated = restarted.status('worker', sub['id'])
            self.assertEqual(recreated['status'], 'active')
            self.assertEqual(recreated['created_at'], '1970-01-01T00:16:42Z')
            self.assertIsNone(recreated['deleted_at'])

    def test_management_denies_other_principal_and_project_even_with_event_access(self):
        self.events.can_access = lambda *_: True
        sub = self.events.subscribe('worker', self.params)
        for operation in [self.events.status, self.events.unsubscribe]:
            with self.assertRaises(PermissionError):
                operation('other-worker', sub['id'], owner='owner/repo')
            with self.assertRaises(PermissionError):
                operation('worker', sub['id'], owner='other/repo')
        self.assertEqual(self.events.status('worker', sub['id'])['status'], 'active')
        self.events.unsubscribe('worker', sub['id'])
        for operation in [self.events.status, self.events.unsubscribe]:
            with self.assertRaises(PermissionError):
                operation('other-worker', sub['id'])

    def test_expiry_is_inactive_at_deadline_and_survives_pruning_and_restart(self):
        with patch('ai_service.events.time.time', return_value=1000):
            sub = self.events.subscribe('worker', {**self.params, 'ttlMs': 1000})
            self.queue_delivery()
            self.events.flush()
            count = len(self.transport.calls)
        with patch('ai_service.events.time.time', return_value=1001):
            self.retry_delivery()
            self.assertEqual(len(self.transport.calls), count)
            self.assertFalse(self.events.has_active('owner/repo'))
            restarted = EventSubscriptions(self.runtime, transport=self.transport, can_access=self.events.can_access)
            record = restarted.status('worker', sub['id'])
            self.assertEqual(record['status'], 'inactive')
            self.assertEqual(record['expires_at'], sub['refreshBefore'])
            self.assertIsNone(record['deleted_at'])
            self.assertTrue(restarted.unsubscribe('worker', sub['id'])['deleted'])
            self.assertEqual(restarted.status('worker', sub['id'])['status'], 'gone')

    def test_delete_clears_queued_delivery_and_rotation_but_keeps_receipt(self):
        sub = self.events.subscribe('worker', self.params)
        self.queue_delivery()
        self.events.flush()
        self.events.subscribe('worker', self.refresh_secret(b'y'))
        count = len(self.transport.calls)
        self.events.unsubscribe('worker', sub['id'])
        self.retry_delivery()
        self.assertEqual(len(self.transport.calls), count)
        with self.runtime.connection() as db:
            for table in ['subscriptions', 'event_deliveries', 'subscription_previous_keys']:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM subscription_lifecycle').fetchone()[0], 1)
            self.assertEqual({row['name'] for row in db.execute('PRAGMA table_info(subscription_lifecycle)')},
                             {'id', 'principal', 'owner', 'created', 'expires', 'deleted'})

    def test_revoked_and_disabled_subscriptions_remain_inactive_without_delivery(self):
        for revoked in [True, False]:
            with self.subTest(revoked=revoked):
                self.allowed = True
                sub = self.events.subscribe('worker', self.params)
                self.queue_delivery()
                self.events.flush()
                self.allowed = not revoked
                if not revoked:
                    with self.runtime.connection() as db:
                        db.execute('UPDATE subscriptions SET active=0')
                self.assertEqual(self.events.status('worker', sub['id'])['status'], 'inactive')
                count = len(self.transport.calls)
                self.retry_delivery()
                self.retry_delivery()
                self.assertEqual(len(self.transport.calls), count)
                self.assertEqual(self.events.status('worker', sub['id'])['status'], 'inactive')

    def test_lifecycle_additive_upgrade_preserves_legacy_schema_and_unknown_creation_time(self):
        sub = self.events.subscribe('worker', self.params)
        with self.runtime.connection() as db:
            db.execute('DROP TABLE subscription_lifecycle')
        migrated = EventSubscriptions(self.runtime, transport=self.transport, can_access=self.events.can_access)
        record = migrated.status('worker', sub['id'])
        self.assertEqual(record['status'], 'active')
        self.assertIsNone(record['created_at'])
        with self.runtime.connection() as db:
            self.assertEqual(len(db.execute('PRAGMA table_info(subscriptions)').fetchall()), 8)
        migrated.subscribe('worker', self.params)
        self.assertIsNone(migrated.status('worker', sub['id'])['created_at'])
        self.assertTrue(migrated.unsubscribe('worker', sub['id'])['deleted'])

    def test_delete_serializes_with_inflight_delivery(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        sub = self.events.subscribe('worker', self.params)
        self.queue_delivery()
        sending, release, deleting = Event(), Event(), Event()
        callback = self.transport.request
        def blocked_send(url, **kwargs):
            sending.set()
            if not release.wait(3):
                raise RuntimeError('test delivery was not released')
            return callback(url, **kwargs)
        def delete():
            deleting.set()
            return self.events.unsubscribe('worker', sub['id'])
        with patch.object(self.transport, 'request', side_effect=blocked_send), ThreadPoolExecutor(2) as pool:
            delivery = pool.submit(self.events.flush)
            try:
                self.assertTrue(sending.wait(3))
                deletion = pool.submit(delete)
                self.assertTrue(deleting.wait(3))
                self.assertFalse(deletion.done())
            finally:
                release.set()
            self.assertEqual(deletion.result(timeout=3)['status'], 'gone')
            delivery.result(timeout=3)
        count = len(self.transport.calls)
        self.retry_delivery()
        self.assertEqual(len(self.transport.calls), count)

    def management_rpc(self, app, principal, name, arguments):
        return app.dispatch('POST', '/mcp', {}, {'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}, principal=principal)[1]

    def test_mcp_management_discovery_and_authorization(self):
        from ai_service.auth import Principal
        from ai_service.server import Application
        app = Application(self.runtime, None, self.events)
        self.events.can_access = lambda *_: True
        sub = self.events.subscribe('worker', self.params)
        args = {'subscription_id': sub['id']}
        owner = Principal('worker', 'owner/repo', frozenset({'events:subscribe'}), 'dot')
        tools = app.mcp(owner, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        self.assertEqual({tool['name'] for tool in tools}, {'get_event_subscription', 'delete_event_subscription'})
        denied = [Principal('other', owner.project, owner.scopes, 'dot'),
                  Principal('worker', 'other/repo', owner.scopes, 'dot'),
                  Principal('worker', owner.project, frozenset({'tasks:work'}), 'dot'),
                  Principal('worker', owner.project, owner.scopes, 'other-provider')]
        for principal in denied:
            for name in ['get_event_subscription', 'delete_event_subscription']:
                reply = self.management_rpc(app, principal, name, args)
                self.assertEqual(reply['error']['code'], -32003)
        for arguments in [{}, {'subscription_id': ''}, {'subscription_id': []}, {**args, 'project': 'other/repo'}]:
            self.assertEqual(self.management_rpc(app, owner, 'get_event_subscription', arguments)['error']['code'], -32602)
        record = self.management_rpc(app, owner, 'get_event_subscription', args)['result']['structuredContent']
        self.assertEqual(record['status'], 'active')
        self.assertNotIn(self.params['delivery']['url'], json.dumps(record))
        for expected in [True, False]:
            reply = self.management_rpc(app, owner, 'delete_event_subscription', args)['result']
            self.assertFalse(reply['isError'])
            self.assertEqual(reply['structuredContent']['status'], 'gone')
            self.assertEqual(reply['structuredContent']['deleted'], expected)

    def test_http_mcp_subscription_lifecycle_with_existing_auth(self):
        import threading
        import urllib.error
        import urllib.request
        from ai_service.auth import AuthenticationError, Principal
        from ai_service.server import Application, make_server
        principals = {name: Principal(name, 'owner/repo', frozenset({'events:subscribe'}), 'dot')
                      for name in ['worker', 'other-worker']}
        class Authorizer:
            resource = 'https://service.example/mcp'
            def authenticate(self, header):
                if header not in principals:
                    raise AuthenticationError('authentication required')
                return principals[header]
        app = Application(self.runtime, Authorizer(), self.events)
        server = make_server(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://localhost:' + str(server.server_port) + '/mcp'
        def rpc(method, params, identity='worker'):
            request = urllib.request.Request(url, data=json.dumps({
                'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode(),
                headers={'Authorization': identity, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                return json.load(response)
        sub = rpc('events/subscribe', {**self.params, 'ttlMs': 1000})['result']
        args = {'subscription_id': sub['id']}
        def tool(name, arguments=args, identity='worker'):
            return rpc('tools/call', {'name': name, 'arguments': arguments}, identity)
        active = tool('get_event_subscription')['result']['structuredContent']
        self.assertEqual(active['status'], 'active')
        for name in ['get_event_subscription', 'delete_event_subscription']:
            self.assertEqual(tool(name, identity='other-worker')['error']['code'], -32003)
        with patch('ai_service.events.time.time', return_value=time.time() + 2):
            self.assertEqual(tool('get_event_subscription')['result']['structuredContent']['status'], 'inactive')
            deleted = tool('delete_event_subscription')['result']['structuredContent']
        self.assertEqual(deleted['status'], 'gone')
        self.assertTrue(deleted['deleted'])
        self.assertIsNotNone(deleted['created_at'])
        self.assertIsNotNone(deleted['deleted_at'])
        self.assertEqual(tool('delete_event_subscription')['result']['structuredContent']['deleted'], False)
        self.assertEqual(tool('get_event_subscription')['result']['structuredContent']['status'], 'gone')
        unknown = {'subscription_id': 'never-existed'}
        self.assertEqual(tool('delete_event_subscription', unknown)['result']['structuredContent']['status'], 'gone')
        self.assertEqual(tool('get_event_subscription', unknown)['result']['structuredContent']['status'], 'gone')
        with self.assertRaises(urllib.error.HTTPError) as error:
            tool('get_event_subscription', identity='unauthenticated')
        self.assertEqual(error.exception.code, 401)

    def test_native_unsubscribe_is_visible_to_management_readback(self):
        from ai_service.auth import Principal
        from ai_service.server import Application
        principal = Principal('worker', 'owner/repo', frozenset({'events:subscribe'}), 'dot')
        app = Application(self.runtime, None, self.events)
        sub = self.events.subscribe('worker', self.params)
        params = {'name': self.params['name'], 'arguments': self.params['arguments'],
                  'delivery': {'mode': 'webhook', 'url': self.params['delivery']['url']}}
        for _ in range(2):
            reply = app.mcp(principal, {'jsonrpc': '2.0', 'id': 1, 'method': 'events/unsubscribe', 'params': params})
            self.assertEqual(reply['result'], {})
        record = self.management_rpc(app, principal, 'get_event_subscription', {'subscription_id': sub['id']})['result']['structuredContent']
        self.assertEqual(record['status'], 'gone')
        self.assertIsNotNone(record['deleted_at'])


if __name__ == '__main__':
    unittest.main()
