"""Q04's existing caller, with real Application/TaskRuntime and no external IO."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pytest

from ai_service.auth import AuthenticationError, Principal
from ai_service.client import TaskClient
from ai_service.events import DotEventProvider, EventSubscriptions
from ai_service.runtime import Policy, TaskRuntime
from ai_service.scenarios.pilot import digest
from ai_service.server import Application

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tests/task_service'))
from test_pilot import AppTransport, SignedCallbackFixture, fixture  # noqa: E402

spec = importlib.util.spec_from_file_location('q04_health_cycle', ROOT / 'ops/quant-monitor/scripts/health_cycle.py')
HEALTH = importlib.util.module_from_spec(spec)
spec.loader.exec_module(HEALTH)
PROJECT = 'QuantStrategyLab/QuantAIService'


class Authorizer:
    resource = 'https://service.example/mcp'
    principals = {
        'Bearer synthetic-submitter': Principal('submitter', PROJECT, frozenset({'tasks:read', 'tasks:submit'})),
        'Bearer synthetic-dot': Principal('dot-worker', None, frozenset({'tasks:read', 'tasks:work', 'events:subscribe'}), 'dot', owner='QuantStrategyLab'),
        'Bearer synthetic-grok': Principal('grok-worker', PROJECT, frozenset({'tasks:read', 'tasks:work'}), 'grok'),
        'Bearer synthetic-other-owner': Principal('other-worker', 'OtherOrg/OtherProject', frozenset({'tasks:read', 'tasks:work'}), 'dot'),
    }

    def authenticate(self, header):
        if header not in self.principals:
            raise AuthenticationError('fixture identity required')
        return self.principals[header]


class CallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.receipt = self.root / 'q04.intent'
        self.now = datetime.now(timezone.utc)
        self.instance = fixture('instance.json')
        self.instance.update(project=PROJECT, enabled=True)
        self.instance['approvals'] = {'activation_ref': 'explicit-offline-synthetic-approval'}
        self.runtime = TaskRuntime(self.root / 'runtime.sqlite', {}, {
            PROJECT: Policy({'agent': 'dot'}, {'dot': {'configured-dot'}}, max_timeout_seconds=600),
            'OtherOrg/OtherProject': Policy({'agent': 'dot'}, {'dot': {'configured-dot'}}),
        })
        self.callback = SignedCallbackFixture()
        self.events = EventSubscriptions(self.runtime, transport=self.callback,
            can_access=lambda identity, project, provider: identity == 'dot-worker' and project == PROJECT and provider == 'dot')
        self.runtime.providers['dot'] = DotEventProvider(self.events)
        self.app = Application(self.runtime, Authorizer(), self.events)
        self.transport = AppTransport(self.app)
        self.client = TaskClient('https://service.example', 'synthetic-submitter', transport=self.transport)
        self.event_client = TaskClient('https://service.example', 'synthetic-dot', transport=self.transport)
        self.subscription, self.creation = self.create_subscription()
        self.requests = []
        self.after_submit = self.complete
        self.readback = lambda value: value
        self.lose_submit = False
        self.lose_cancel = False
        self.fail_delete = False
        original = self.transport.request

        def request(url, *, method, headers, body):
            self.requests.append((method, url, json.loads(body) if body else None, headers.get('Idempotency-Key')))
            if self.fail_delete and b'delete_event_subscription' in body:
                raise TimeoutError('private cleanup response')
            reply = original(url, method=method, headers=headers, body=body)
            if method == 'POST' and url.endswith('/v1/tasks'):
                self.after_submit(json.loads(reply.body))
                if self.lose_submit:
                    raise TimeoutError('private submit response')
            if url.endswith('/cancel') and self.lose_cancel:
                raise TimeoutError('private cancellation response')
            if method == 'GET' and '/v1/tasks/' in url:
                reply = type(reply)(reply.status, json.dumps(self.readback(json.loads(reply.body))).encode())
            return reply

        self.transport.request = request

    def create_subscription(self):
        # The normal offline creation boundary fixes the actual native request.
        return self.capture_subscription({
            'name': 'task.requested', 'arguments': {'project': PROJECT}, 'ttlMs': 600000,
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example/q04',
                         'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()},
        })

    def capture_subscription(self, params):
        # Capture the request actually sent through the existing native interface.
        sent = copy.deepcopy(params)
        reply = self.event_client.request('POST', '/mcp', {
            'jsonrpc': '2.0', 'id': 1, 'method': 'events/subscribe', 'params': sent,
        })
        response = reply['result']
        state = HEALTH._rehearsal_subscription(self.event_client, response['id'], 'get_event_subscription')
        redacted = copy.deepcopy(sent)
        redacted['delivery'].pop('secret')
        return response['id'], {
            'kind': 'offline.fixture', 'worker': 'dot-worker', 'provider': 'dot',
            'request': redacted, 'response': response,
            'created_at': state['created_at'], 'expires_at': state['expires_at'],
        }

    def rpc(self, name, args, identity='synthetic-dot'):
        return self.app.dispatch('POST', '/mcp', {'Authorization': 'Bearer ' + identity},
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
             'params': {'name': name, 'arguments': {'project': PROJECT, **args}}})[1]

    def complete(self, record):
        self.runtime.tick()
        self.events.flush()
        events = [item for item in self.callback.calls if item.get('data', {}).get('task_id') == record['id']]
        self.assertTrue(events)
        self.assertEqual(len({event['eventId'] for event in events}), 1)
        claimed = self.rpc('claim_task', {'task_id': events[0]['data']['task_id'], 'attempt_id': 'q04-fixed-attempt'})['result']['structuredContent']
        manifest = claimed['request']['materials'][0]['input_manifest']
        self.assertEqual(manifest, fixture('input.json'))
        task = claimed['request']['materials'][0]['scenario_task']
        self.output = {
            'schema_version': 'scenario.pilot-result.v1', 'operation_key': task['operation_key'],
            'input_manifest_sha256': digest(manifest),
            'result': {'synthetic': True, 'no_order': True, 'adoption_allowed': False,
                       'sample_count': 4, 'difference_sum': 5},
        }
        self.report_args = {'task_id': record['id'], 'attempt_id': claimed['attempt_id'],
                            'revision': claimed['revision'], 'status': 'completed', 'output': self.output}
        self.assertEqual(self.rpc('report_task_result', self.report_args)['result']['structuredContent']['status'], 'completed')

    def run_caller(self, **changes):
        args = dict(instance=self.instance, occurrence='q04-fixed-occurrence', submitted_at=self.now,
                    receipt_path=self.receipt, client=self.client, event_client=self.event_client,
                    subscription_id=self.subscription, expected_worker='dot-worker',
                    expected_attempt='q04-fixed-attempt', subscription_creation=self.creation)
        args.update(changes)
        return HEALTH.run_historical_diagnosis_rehearsal(**args)

    def assert_gone(self):
        self.assertEqual(self.events.status('dot-worker', self.subscription, owner=PROJECT)['status'], 'gone')

    def assert_retained(self):
        self.assertEqual(self.events.status('dot-worker', self.subscription, owner=PROJECT)['status'], 'active')
        self.assertFalse(any(row[2].get('params', {}).get('name') == 'delete_event_subscription'
                             for row in self.requests if isinstance(row[2], dict)))

    def test_existing_caller_submits_fixed_contract_verifies_and_cleans(self):
        result = self.run_caller()
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['output_sha256'], digest(self.output))
        self.assertEqual(result['attempt'], 'q04-fixed-attempt')
        self.assertEqual(result['revision'], 1)
        self.assertEqual(result['provider'], 'dot')
        self.assertEqual(result['project'], PROJECT)
        self.assertEqual(result['cleanup'], 'gone')
        self.assertTrue(Path(str(self.receipt) + '.verified').exists())
        self.assert_gone()

    def test_async_pending_dispatch_and_resume(self):
        self.after_submit = lambda record: None
        first = self.run_caller()
        self.assertEqual(first['status'], 'deferred')
        self.assertEqual(self.events.status('dot-worker', self.subscription, owner=PROJECT)['status'], 'active')
        self.assertFalse(any(item.get('data') for item in self.callback.calls))
        self.assertEqual(self.runtime.get(PROJECT, first['task_id'])['status'], 'queued')
        self.runtime.tick()
        self.events.flush()
        event = next(item for item in self.callback.calls if item.get('data', {}).get('task_id') == first['task_id'])
        self.rpc('claim_task', {'task_id': event['data']['task_id'], 'attempt_id': 'q04-fixed-attempt'})
        running = self.run_caller()
        self.assertEqual(running['attempt'], 'q04-fixed-attempt')
        self.assertEqual(running['cleanup'], 'retained')
        self.complete({'id': first['task_id']})
        restored = TaskClient('https://service.example', 'synthetic-submitter', transport=self.transport)
        final = self.run_caller(client=restored)
        self.assertEqual(final['status'], 'verified')
        self.assertEqual(first['task_id'], final['task_id'])
        self.assertEqual(len([row for row in self.requests if row[1].endswith('/v1/tasks')]), 1)
        self.assert_gone()
        print('Q04_ASYNC_EVIDENCE ' + json.dumps(final, sort_keys=True))

    def test_interrupted_submitted_write_preserves_bound_unknown(self):
        from ai_service.scenarios import pilot_cli
        original = pilot_cli.write_private
        def interrupted(path, value):
            if str(path).endswith('.submitted'):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    stream.write('{')
                raise OSError('fixture interruption after actual submit')
            return original(path, value)
        self.after_submit = lambda record: None
        with patch.object(pilot_cli, 'write_private', interrupted):
            first = self.run_caller()
        second = self.run_caller()
        self.assertEqual(second['reason'], 'submit_outcome_unknown')
        self.assertEqual(second['operation_key'], first['operation_key'])
        self.assertEqual(second['request_sha256'], first['request_sha256'])
        self.assertEqual(len([row for row in self.requests if row[1].endswith('/v1/tasks')]), 1)
        with self.runtime.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_claim_cannot_regress_to_empty_cancel_identity_across_resume(self):
        def claim(record):
            self.runtime.tick()
            self.rpc('claim_task', {'task_id': record['id'], 'attempt_id': 'q04-fixed-attempt'})
        self.after_submit = claim
        first = self.run_caller()
        def regress(record):
            if any(row[1].endswith('/cancel') for row in self.requests):
                record.update(status='cancelled', attempt=None, worker='different-worker')
            return record
        self.readback = regress
        result = self.run_caller(cancel=True)
        self.assertNotEqual(result.get('reason'), 'cancellation_confirmed')
        self.assertEqual(self.runtime.get(PROJECT, first['task_id'])['status'], 'cancel_requested')

    def test_one_second_creation_cannot_pass_fixed_ttl(self):
        self.events.unsubscribe('dot-worker', self.subscription, owner=PROJECT)
        self.subscription = self.events.subscribe('dot-worker', {
            'name': 'task.requested', 'arguments': {'project': PROJECT}, 'ttlMs': 1000,
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example/q04',
                         'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()},
        })['id']
        self.assertNotEqual(self.run_caller()['status'], 'verified')
        self.assertFalse(any(row[1].endswith('/v1/tasks') for row in self.requests))

    def test_durable_receipt_contains_complete_caller_binding(self):
        result = self.run_caller()
        saved = json.loads(Path(str(self.receipt) + '.verified').read_text())
        self.assertEqual(saved, result)
        record = self.client.get(result['task_id'])
        task = record['request']['materials'][0]['scenario_task']
        self.assertEqual(result['request_sha256'], digest(record['request']))
        self.assertEqual(result['input_manifest_sha256'], task['input_manifest_sha256'])
        self.assertEqual(result['operation_key'], task['operation_key'])
        self.assertEqual((record['attempt'], record['revision'], record['provider']),
                         (result['attempt'], result['revision'], result['provider']))
        print('Q04_LOCAL_EVIDENCE ' + json.dumps(result, sort_keys=True))

    def test_duplicate_caller_and_events_keep_task_key_attempt_and_receipt(self):
        first = self.run_caller()
        self.assertEqual(first['status'], 'verified')
        again = self.run_caller()
        self.assertEqual(first, again)
        submits = [row for row in self.requests if row[0] == 'POST' and row[1].endswith('/v1/tasks')]
        self.assertEqual(len(submits), 1)
        self.assertEqual(submits[0][3], first['operation_key'])
        self.assertEqual(self.rpc('claim_task', {'task_id': first['task_id'], 'attempt_id': first['attempt']})['result']['structuredContent']['attempt'], first['attempt'])
        self.assertEqual(self.rpc('report_task_result', self.report_args)['result']['structuredContent']['output'], self.output)
        self.assertIn('error', self.rpc('claim_task', {'task_id': first['task_id'], 'attempt_id': 'another-attempt'}))
        for bad in ({'attempt_id': 'wrong'}, {'revision': 2}):
            self.assertIn('error', self.rpc('report_task_result', {**self.report_args, **bad}))
        for identity in ('synthetic-grok', 'synthetic-other-owner'):
            self.assertIn('error', self.rpc('get_task', {'task_id': first['task_id']}, identity))
        with self.assertRaises(PermissionError):
            self.event_client.submit(submits[0][2], 'worker-cannot-submit')
        for suffix in ('', '.submitted', '.verified'):
            self.assertEqual(Path(str(self.receipt) + suffix).stat().st_mode & 0o777, 0o600)

    def test_lost_submit_response_keeps_intent_and_does_not_resubmit_or_change_key(self):
        self.lose_submit = True
        first = self.run_caller()
        self.assertEqual(first['reason'], 'submit_outcome_unknown')
        self.assertEqual(self.run_caller()['reason'], 'submit_outcome_unknown')
        self.assertNotEqual(self.run_caller(occurrence='new-key')['status'], 'verified')
        self.assertEqual(len([row for row in self.requests if row[1].endswith('/v1/tasks')]), 1)
        self.assertEqual(json.loads(self.receipt.read_text())['operation_key'], first['operation_key'])
        self.assertFalse(Path(str(self.receipt) + '.submitted').exists())
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())
        self.assert_retained()

    def test_runtime_unknown_is_read_only_on_resume(self):
        def unknown(record):
            with self.runtime.connection() as db:
                db.execute("UPDATE tasks SET status='outcome_unknown' WHERE id=?", (record['id'],))
        self.after_submit = unknown
        first = self.run_caller()
        self.assertEqual(first['status'], 'deferred')
        self.assertEqual(self.run_caller()['task_id'], first['task_id'])
        self.assertEqual(len([row for row in self.requests if row[1].endswith('/v1/tasks')]), 1)
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())
        self.assert_retained()

    def test_expired_input_never_submits_and_cleans(self):
        self.assertNotEqual(self.run_caller(submitted_at=self.now - timedelta(seconds=601))['status'], 'verified')
        self.assertFalse(any(row[1].endswith('/v1/tasks') for row in self.requests))
        self.assert_gone()

    def test_deadline_cancels_once_and_preserves_uncertainty_without_worker_confirmation(self):
        self.after_submit = lambda record: self.runtime.tick()
        first = self.run_caller()
        self.assertEqual(first['status'], 'deferred')
        class Future(datetime):
            @classmethod
            def now(cls, tz=None):
                return self.now + timedelta(seconds=601)
        with patch.object(HEALTH, 'datetime', Future):
            for _ in range(2):
                result = self.run_caller()
                self.assertEqual(result['reason'], 'cancellation_unconfirmed')
        self.assertEqual(len([row for row in self.requests if row[1].endswith('/cancel')]), 1)
        self.assertEqual(self.runtime.get(PROJECT, first['task_id'])['status'], 'cancel_requested')
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())
        self.assert_gone()

    def test_lost_cancel_response_is_never_replayed(self):
        self.after_submit = lambda record: self.runtime.tick()
        first = self.run_caller()
        self.lose_cancel = True
        for _ in range(2):
            self.assertEqual(self.run_caller(cancel=True)['reason'], 'cancellation_unconfirmed')
        self.assertEqual(self.runtime.get(PROJECT, first['task_id'])['status'], 'cancel_requested')
        self.assertEqual(len([row for row in self.requests if row[1].endswith('/cancel')]), 1)
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())
        self.assert_retained()

    def test_completion_after_cancellation_intent_never_verifies(self):
        self.after_submit = lambda record: self.runtime.tick()
        first = self.run_caller()
        self.rpc('claim_task', {'task_id': first['task_id'], 'attempt_id': 'q04-fixed-attempt'})
        self.assertEqual(self.run_caller(cancel=True)['status'], 'deferred')
        record = self.runtime.get(PROJECT, first['task_id'])
        task = record['request']['materials'][0]['scenario_task']
        output = {'schema_version': 'scenario.pilot-result.v1', 'operation_key': task['operation_key'],
                  'input_manifest_sha256': task['input_manifest_sha256'],
                  'result': {'synthetic': True, 'no_order': True, 'adoption_allowed': False,
                             'sample_count': 4, 'difference_sum': 5}}
        self.rpc('report_task_result', {'task_id': first['task_id'], 'attempt_id': 'q04-fixed-attempt',
                                      'revision': 1, 'status': 'completed', 'output': output})
        self.assertEqual(self.run_caller()['reason'], 'cancellation_unconfirmed')
        self.assertEqual(len([row for row in self.requests if row[1].endswith('/cancel')]), 1)
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())

    def test_completion_after_deadline_never_verifies(self):
        class Future(datetime):
            @classmethod
            def now(cls, tz=None):
                return self.now + timedelta(seconds=601)
        complete = self.after_submit
        def complete_late(record):
            complete(record)
            clock = patch.object(HEALTH, 'datetime', Future)
            clock.start()
            self.addCleanup(clock.stop)
        self.after_submit = complete_late
        result = self.run_caller()
        self.assertEqual(result['reason'], 'cancellation_unconfirmed')
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())
        self.assert_gone()

    def test_only_worker_confirmation_proves_cancelled(self):
        self.after_submit = lambda record: self.runtime.tick()
        first = self.run_caller()
        self.rpc('claim_task', {'task_id': first['task_id'], 'attempt_id': 'q04-fixed-attempt'})
        self.assertEqual(self.run_caller(cancel=True)['status'], 'deferred')
        self.rpc('report_task_result', {'task_id': first['task_id'], 'attempt_id': 'q04-fixed-attempt',
                                      'revision': 1, 'status': 'cancelled'})
        self.assertEqual(self.run_caller(cancel=True)['status'], 'cancelled')
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())

    def test_cleanup_failure_prevents_verified_receipt(self):
        self.fail_delete = True
        result = self.run_caller()
        self.assertEqual(result['reason'], 'subscription_cleanup_unconfirmed')
        self.assertEqual(result['cleanup'], 'unknown')
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())
        self.assertNotIn('private', json.dumps(result))

    def test_cleanup_requires_gone_readback_after_delete(self):
        original = self.event_client.request
        gets = 0
        def request(method, path, body):
            nonlocal gets
            reply = original(method, path, body)
            if body['params']['name'] == 'get_event_subscription':
                gets += 1
                if gets == 3:
                    reply['result']['structuredContent']['status'] = 'active'
            return reply
        self.event_client.request = request
        result = self.run_caller()
        self.assertEqual(result['reason'], 'subscription_cleanup_unconfirmed')
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())
        self.assert_gone()

    def test_default_event_ttl_is_rejected_before_submit(self):
        with self.runtime.connection() as db:
            db.execute('UPDATE subscriptions SET expires=expires+3000 WHERE id=?', (self.subscription,))
            db.execute('UPDATE subscription_lifecycle SET expires=expires+3000 WHERE id=?', (self.subscription,))
        self.assertNotEqual(self.run_caller()['status'], 'verified')
        self.assertFalse(any(row[1].endswith('/v1/tasks') for row in self.requests))
        self.assert_gone()

    def test_expired_subscription_never_submits_and_is_cleaned(self):
        with self.runtime.connection() as db:
            db.execute('UPDATE subscriptions SET expires=0 WHERE id=?', (self.subscription,))
        self.assertNotEqual(self.run_caller()['status'], 'verified')
        self.assertFalse(any(row[1].endswith('/v1/tasks') for row in self.requests))
        self.assert_gone()

    def test_changed_operation_receipt_never_submits_another_task(self):
        self.assertEqual(self.run_caller()['status'], 'verified')
        result = self.run_caller(occurrence='a-different-occurrence')
        self.assertNotEqual(result['status'], 'verified')
        self.assertEqual(len([row for row in self.requests if row[1].endswith('/v1/tasks')]), 1)

    def test_failed_worker_report_never_writes_verified(self):
        def fail(record):
            self.runtime.tick()
            self.rpc('claim_task', {'task_id': record['id'], 'attempt_id': 'q04-fixed-attempt'})
            self.rpc('report_task_result', {'task_id': record['id'], 'attempt_id': 'q04-fixed-attempt',
                                          'revision': 1, 'status': 'failed'})
        self.after_submit = fail
        self.assertNotEqual(self.run_caller()['status'], 'verified')
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())
        self.assert_gone()

    def test_invalid_worker_output_never_writes_verified(self):
        def invalid(record):
            self.runtime.tick()
            self.rpc('claim_task', {'task_id': record['id'], 'attempt_id': 'q04-fixed-attempt'})
            self.assertEqual(self.rpc('report_task_result', {
                'task_id': record['id'], 'attempt_id': 'q04-fixed-attempt', 'revision': 1,
                'status': 'completed', 'output': {},
            })['result']['structuredContent']['status'], 'invalid_output')
        self.after_submit = invalid
        self.assertNotEqual(self.run_caller()['status'], 'verified')
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())
        self.assert_gone()

    def test_cleanup_leaves_other_exact_subscription_alone(self):
        other = self.events.subscribe('dot-worker', {
            'name': 'task.requested', 'arguments': {'project': PROJECT}, 'ttlMs': 600000,
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example/unrelated',
                         'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()},
        })['id']
        result = self.run_caller()
        self.assertEqual(result['status'], 'verified')
        self.assert_gone()
        self.assertEqual(self.events.status('dot-worker', other, owner=PROJECT)['status'], 'active')
        calls = [row[2]['params'] for row in self.requests if row[1].endswith('/mcp')]
        self.assertTrue(all(call['arguments'] == {'project': PROJECT, 'subscription_id': self.subscription} for call in calls))
        self.assertEqual([call['name'] for call in calls][-2:], ['delete_event_subscription', 'get_event_subscription'])

    def test_missing_dot_never_uses_environment_cli_api_or_vps(self):
        self.after_submit = lambda record: self.runtime.tick()
        self.runtime.providers['dot'].submit = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('Dot unavailable'))
        with patch('ai_service.client.client_from_env', side_effect=AssertionError('fallback forbidden')):
            self.assertEqual(self.run_caller()['status'], 'deferred')
            self.assertEqual(self.run_caller()['status'], 'deferred')
        self.assertEqual(len([row for row in self.requests if row[1].endswith('/v1/tasks')]), 1)
        self.assertFalse(Path(str(self.receipt) + '.verified').exists())
        self.assert_retained()


@pytest.fixture
def caller():
    case = CallerTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


@pytest.mark.parametrize('field,value', [
    ('id', 'wrong-task'), ('owner', 'OtherOrg/OtherProject'), ('provider', 'grok'),
    ('attempt', 'wrong-attempt'), ('attempt', None), ('worker', 'wrong-worker'),
    ('revision', 2), ('revision', True), ('result_kind', 'verified'),
    ('output', {}), ('status', 'failed'),
])
def test_returned_record_binding_rejects_tampering(caller, field, value):
    caller.readback = lambda record: {**record, field: value}
    result = caller.run_caller()
    assert result['status'] != 'verified'
    assert not Path(str(caller.receipt) + '.verified').exists()
    if field in {'result_kind', 'output', 'status'}:
        caller.assert_gone()  # Bound terminal record; invalid output is a terminal failure.
    else:
        caller.assert_retained()  # Invalid identity cannot establish a terminal state.
        caller.run_caller(end=True)
        caller.assert_gone()


@pytest.mark.parametrize('part', ['request', 'operation_key', 'input_manifest_sha256',
                                  'synthetic', 'no_order', 'adoption_allowed', 'sample_count', 'difference_sum'])
def test_frozen_request_input_and_output_binding(caller, part):
    def tamper(record):
        if part == 'request':
            record['request']['model'] = 'other-model'
        elif part in {'operation_key', 'input_manifest_sha256'}:
            record['output'][part] = 'wrong'
        else:
            record['output']['result'][part] = 0
        return record
    caller.readback = tamper
    assert caller.run_caller()['status'] != 'verified'
    assert not Path(str(caller.receipt) + '.verified').exists()
    if part == 'request':
        caller.assert_retained()
        caller.run_caller(end=True)
    caller.assert_gone()


@pytest.mark.parametrize('part', ['disabled', 'unapproved', 'owner', 'provider', 'template', 'ttl', 'timeout'])
def test_unapproved_or_wrong_instance_never_submits(caller, part):
    instance = copy.deepcopy(caller.instance)
    if part == 'disabled':
        instance['enabled'] = False
    elif part == 'unapproved':
        instance['approvals']['activation_ref'] = None
    elif part == 'owner':
        instance['project'] = 'OtherOrg/OtherProject'
    elif part == 'provider':
        instance['stages'][0]['worker_type'] = 'api'
    elif part == 'template':
        instance['template'] = 'investigation'
    elif part == 'ttl':
        instance['inputs']['fields']['ttl_seconds'] = 3600
    else:
        instance['stages'][0]['limits']['timeout_seconds'] = 3600
    assert caller.run_caller(instance=instance)['status'] != 'verified'
    assert not any(row[1].endswith('/v1/tasks') for row in caller.requests)
    caller.assert_gone()


@pytest.mark.parametrize('sidecar', ['missing', 'partial', 'unreadable'])
def test_existing_intent_retains_unknown_for_unusable_submitted(caller, sidecar):
    caller.after_submit = lambda record: None
    first = caller.run_caller()
    path = Path(str(caller.receipt) + '.submitted')
    if sidecar == 'missing':
        path.unlink()
    elif sidecar == 'partial':
        path.write_text('{')
    original = Path.read_text
    def read(value, *args, **kwargs):
        if value == path and sidecar == 'unreadable':
            raise PermissionError('fixture submitted receipt cannot be read')
        return original(value, *args, **kwargs)
    with patch.object(Path, 'read_text', read):
        result = caller.run_caller()
    assert result['reason'] == 'submit_outcome_unknown'
    assert (result['operation_key'], result['request_sha256']) == (first['operation_key'], first['request_sha256'])
    assert sum(row[1].endswith('/v1/tasks') for row in caller.requests) == 1
    caller.assert_retained()
    assert caller.run_caller(end=True)['cleanup'] == 'gone'


@pytest.mark.parametrize('fields', [
    {'attempt': None, 'worker': None}, {'attempt': None, 'worker': 'wrong-worker'},
    {'attempt': 'q04-fixed-attempt', 'worker': None}, {'attempt': 'other-attempt'},
    {'worker': 'other-worker'}, {'revision': 2}, {'revision': True},
    {'provider': 'grok'}, {'owner': 'OtherOrg/OtherProject'}, {'id': 'other-task'},
])
def test_cancel_readback_binds_observed_identity_across_recovery(caller, fields):
    def claim(record):
        caller.runtime.tick()
        caller.rpc('claim_task', {'task_id': record['id'], 'attempt_id': 'q04-fixed-attempt'})
    caller.after_submit = claim
    first = caller.run_caller()
    assert first['attempt'] == 'q04-fixed-attempt'
    caller.lose_cancel = True
    assert caller.run_caller(cancel=True)['reason'] == 'cancellation_unconfirmed'
    caller.readback = lambda record: {**record, 'status': 'cancelled', **fields}
    result = caller.run_caller(cancel=True)
    assert result['reason'] == 'cancellation_unconfirmed'
    assert result['status'] != 'cancelled'
    assert caller.runtime.get(PROJECT, first['task_id'])['status'] == 'cancel_requested'
    assert sum(row[1].endswith('/cancel') for row in caller.requests) == 1
    assert not Path(str(caller.receipt) + '.verified').exists()
    caller.assert_retained()


def test_unclaimed_queue_cancellation_confirms_actual_double_empty_identity(caller):
    caller.after_submit = lambda record: None
    first = caller.run_caller()
    result = caller.run_caller(cancel=True)
    assert result['status'] == 'cancelled'
    assert result['reason'] == 'cancellation_confirmed'
    assert result['attempt'] is None and result['worker'] is None
    assert caller.runtime.get(PROJECT, first['task_id'])['status'] == 'cancelled'
    caller.assert_gone()


def test_unclaimed_empty_attempt_with_wrong_worker_never_confirms(caller):
    caller.after_submit = lambda record: None
    caller.run_caller()
    caller.readback = lambda record: {**record, 'status': 'cancelled', 'worker': 'wrong-worker'}
    result = caller.run_caller(cancel=True)
    assert result['reason'] == 'cancellation_unconfirmed'
    assert result['status'] != 'cancelled'
    caller.assert_retained()


@pytest.mark.parametrize('variant', ['one_second', 'default', 'wrong_id', 'wrong_project', 'wrong_worker', 'stale'])
def test_subscription_creation_request_and_incarnation_are_bound(caller, variant):
    params = {
        'name': 'task.requested', 'arguments': {'project': PROJECT}, 'ttlMs': 600000,
        'delivery': {'mode': 'webhook', 'url': 'https://callback.example/q04',
                     'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()},
    }
    if variant in {'one_second', 'default'}:
        caller.events.unsubscribe('dot-worker', caller.subscription, owner=PROJECT)
        if variant == 'one_second':
            params['ttlMs'] = 1000
        else:
            params.pop('ttlMs')
        caller.subscription, caller.creation = caller.capture_subscription(params)
    elif variant == 'wrong_id':
        caller.creation['response']['id'] = 'different-subscription'
    elif variant == 'wrong_project':
        caller.creation['request']['arguments']['project'] = 'OtherOrg/OtherProject'
    elif variant == 'wrong_worker':
        caller.creation['worker'] = 'other-worker'
    else:
        # The same subscription ID can be reused; timestamps bind this creation.
        caller.events.unsubscribe('dot-worker', caller.subscription, owner=PROJECT)
        caller.subscription, _new_creation = caller.create_subscription()
    result = caller.run_caller()
    assert result['status'] != 'verified'
    assert not any(row[1].endswith('/v1/tasks') for row in caller.requests)
    assert not Path(str(caller.receipt) + '.verified').exists()
    caller.assert_gone()


def test_creation_evidence_is_actual_request_and_saved_in_receipt(caller):
    result = caller.run_caller()
    assert result['status'] == 'verified'
    creation = result['subscription_creation']
    assert creation['request']['ttlMs'] == 600000
    assert creation['response']['id'] == caller.subscription
    assert creation['response']['refreshBefore'] == creation['expires_at']
    assert result['subscription_creation_sha256'] == digest(creation)
    assert json.loads(caller.receipt.read_text())['subscription_creation'] == creation
    assert 'secret' not in creation['request']['delivery']


@pytest.mark.parametrize('operation', ['submit', 'cancel'])
@pytest.mark.parametrize('fields', [
    {'attempt': None, 'worker': 'different-worker'}, {'revision': True}, {'provider': 'grok'},
])
def test_submit_and_cancel_responses_use_same_binding_checks(caller, operation, fields):
    caller.after_submit = lambda record: None
    if operation == 'cancel':
        first = caller.run_caller()
        caller.runtime.tick()
        caller.rpc('claim_task', {'task_id': first['task_id'], 'attempt_id': 'q04-fixed-attempt'})
        caller.run_caller()  # Persist the actual observed claim before recovery.
    original = getattr(caller.client, operation)
    def changed(*args):
        return {**original(*args), **fields}
    setattr(caller.client, operation, changed)
    result = caller.run_caller(cancel=operation == 'cancel')
    assert result['reason'] == ('submit_outcome_unknown' if operation == 'submit' else 'cancellation_unconfirmed')
    assert result['status'] != 'cancelled'
    caller.assert_retained()


@pytest.mark.parametrize('key', ['worker', 'attempt'])
def test_missing_identity_field_is_not_explicit_empty(caller, key):
    caller.after_submit = lambda record: None
    caller.run_caller()
    def missing(record):
        record.update(status='cancelled')
        record.pop(key)
        return record
    caller.readback = missing
    assert caller.run_caller(cancel=True)['reason'] == 'cancellation_unconfirmed'
    caller.assert_retained()


@pytest.mark.parametrize('cleanup', ['gone', 'unavailable'])
def test_submit_unknown_survives_gone_subscription(caller, cleanup):
    caller.after_submit = lambda record: None
    first = caller.run_caller()
    Path(str(caller.receipt) + '.submitted').write_text('{')
    if cleanup == 'gone':
        caller.events.unsubscribe('dot-worker', caller.subscription, owner=PROJECT)
    else:
        caller.event_client.request = lambda *args: (_ for _ in ()).throw(TimeoutError('fixture subscription read unavailable'))
    result = caller.run_caller()
    assert result['reason'] == 'submit_outcome_unknown'
    assert result['operation_key'] == first['operation_key']
    assert result['request_sha256'] == first['request_sha256']
    assert result['cleanup'] == ('gone' if cleanup == 'gone' else 'unknown')
    assert sum(row[1].endswith('/v1/tasks') for row in caller.requests) == 1


@pytest.mark.parametrize('fault', ['partial_create', 'unreadable_existing'])
@pytest.mark.parametrize('changed', [False, True])
def test_primary_intent_fault_recovers_original_binding(caller, fault, changed):
    from ai_service.scenarios import pilot_cli
    from ai_service.scenarios.pilot import prepare
    task = prepare(caller.instance, fixture('input.json'), occurrence='q04-fixed-occurrence', submitted_at=caller.now)
    caller.after_submit = lambda record: None
    original_write = pilot_cli.write_private
    original_read = Path.read_text
    def interrupted(path, value):
        if path == caller.receipt:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write('{')
            raise OSError('fixture primary intent interruption')
        return original_write(path, value)
    def unreadable(path, *args, **kwargs):
        if path == caller.receipt:
            raise PermissionError('fixture primary intent unreadable')
        return original_read(path, *args, **kwargs)
    if fault == 'partial_create':
        with patch.object(pilot_cli, 'write_private', interrupted):
            caller.run_caller()
    else:
        caller.lose_submit = True
        caller.run_caller()
    with patch.object(Path, 'read_text', unreadable if fault == 'unreadable_existing' else original_read):
        result = caller.run_caller(**({'occurrence': 'different-occurrence'} if changed else {}))
    bound = (result.get('operation_key') == task.spec['operation_key']
             and result.get('request_sha256') == digest(task.request))
    assert result.get('reason') == 'submit_outcome_unknown' and bound
    assert sum(row[1].endswith('/v1/tasks') for row in caller.requests) == (0 if fault == 'partial_create' else 1)
    caller.assert_retained()


def test_entry_redacts_raw_creation_before_receipts_and_return(caller):
    # Use the same public synthetic signing fixture; never print its value.
    params = {'name': 'task.requested', 'arguments': {'project': PROJECT}, 'ttlMs': 600000,
              'delivery': {'mode': 'webhook', 'url': 'https://callback.example/q04',
                           'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()}}
    caller.events.unsubscribe('dot-worker', caller.subscription, owner=PROJECT)
    caller.subscription, creation = caller.capture_subscription(params)
    creation['request'] = copy.deepcopy(params)
    creation['Authorization'] = params['delivery']['secret']
    creation['request']['headers'] = {'Authorization': params['delivery']['secret']}
    creation['request']['delivery']['Authorization'] = params['delivery']['secret']
    creation['response']['extra'] = {'Authorization': params['delivery']['secret']}
    marker = object()  # Raw context cannot even be serialized as a whole.
    creation['extra'] = marker
    result = caller.run_caller(subscription_creation=creation)
    returned = params['delivery']['secret'] in json.dumps(result)
    persisted = any(params['delivery']['secret'] in path.read_text() for path in caller.root.glob('q04.intent*') if path.is_file())
    print('Q04_REDACTION_EVIDENCE ' + json.dumps({'returned': returned, 'persisted': persisted}))
    assert not returned and not persisted
    assert result['status'] == 'verified'
    assert creation['extra'] is marker  # The caller must not mutate its input.


@pytest.mark.parametrize('fault', ['partial', 'missing', 'unreadable', 'checksum'])
def test_unrecoverable_binding_is_unknown_and_never_uses_new_occurrence(caller, fault):
    caller.after_submit = lambda record: None
    caller.run_caller()
    binding_path = Path(str(caller.receipt) + '.binding')
    if fault == 'partial':
        binding_path.write_text('{')
    elif fault == 'missing':
        binding_path.unlink()
    elif fault == 'checksum':
        value = json.loads(binding_path.read_text())
        value['binding']['operation_key'] = 'different-key'
        binding_path.write_text(json.dumps(value))
    original = Path.read_text
    def read(path, *args, **kwargs):
        if fault == 'unreadable' and path == binding_path:
            raise PermissionError('fixture immutable binding unreadable')
        return original(path, *args, **kwargs)
    with patch.object(Path, 'read_text', read):
        result = caller.run_caller(occurrence='different-occurrence')
    assert result['reason'] == 'submit_outcome_unknown'
    assert result['binding_unknown'] is True
    assert 'operation_key' not in result and 'request_sha256' not in result
    assert sum(row[1].endswith('/v1/tasks') for row in caller.requests) == 1
    caller.assert_retained()


def test_binding_is_complete_private_and_immutable_before_submit(caller):
    original = caller.after_submit
    def submitted(record):
        envelope = json.loads(Path(str(caller.receipt) + '.binding').read_text())
        intent = json.loads(caller.receipt.read_text())
        assert envelope['binding_sha256'] == digest(envelope['binding'])
        assert envelope['binding']['operation_key'] == intent['operation_key']
        assert envelope['binding']['request_sha256'] == digest(record['request'])
        original(record)
    caller.after_submit = submitted
    assert caller.run_caller()['status'] == 'verified'
    binding_path = Path(str(caller.receipt) + '.binding')
    before = binding_path.read_bytes()
    result = caller.run_caller(occurrence='different-occurrence')
    assert result['reason'] == 'submit_outcome_unknown'
    assert result['operation_key'] == json.loads(before)['binding']['operation_key']
    assert binding_path.read_bytes() == before
    for path in caller.root.glob('q04.intent*'):
        assert path.stat().st_mode & 0o777 == 0o600


def test_lost_main_intent_does_not_authorize_another_submit(caller):
    caller.after_submit = lambda record: None
    first = caller.run_caller()
    caller.receipt.unlink()
    result = caller.run_caller(occurrence='different-occurrence')
    assert result['reason'] == 'submit_outcome_unknown'
    assert result['operation_key'] == first['operation_key']
    assert result['request_sha256'] == first['request_sha256']
    assert sum(row[1].endswith('/v1/tasks') for row in caller.requests) == 1
    caller.assert_retained()


def test_partial_binding_creation_never_submits_or_invents_identity(caller):
    from ai_service.scenarios import pilot_cli
    original = pilot_cli.write_private
    def interrupted(path, value):
        if str(path).endswith('.binding'):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write('{')
            raise OSError('fixture binding create interrupted')
        return original(path, value)
    with patch.object(pilot_cli, 'write_private', interrupted):
        first = caller.run_caller()
    second = caller.run_caller(occurrence='different-occurrence')
    for result in (first, second):
        assert result['reason'] == 'submit_outcome_unknown'
        assert result['binding_unknown'] is True
        assert 'operation_key' not in result and 'request_sha256' not in result
    assert not any(row[1].endswith('/v1/tasks') for row in caller.requests)
    assert Path(str(caller.receipt) + '.binding').stat().st_mode & 0o777 == 0o600
    caller.assert_retained()


@pytest.mark.parametrize('field', ['arguments', 'scalar'])
def test_sensitive_extra_cannot_change_protocol_field_shapes(caller, field):
    creation = copy.deepcopy(caller.creation)
    if field == 'arguments':
        creation['request']['arguments']['Authorization'] = 'synthetic-sensitive-marker'
    else:
        creation['response']['id'] = {'Authorization': 'synthetic-sensitive-marker'}
    result = caller.run_caller(subscription_creation=creation)
    assert result['status'] != 'verified'
    leaked = 'synthetic-sensitive-marker' in json.dumps(result)
    persisted = any('synthetic-sensitive-marker' in path.read_text() for path in caller.root.glob('q04.intent*'))
    print('Q04_REDACTION_EVIDENCE ' + json.dumps({'returned': leaked, 'persisted': persisted}))
    assert not leaked and not persisted
    assert not any(row[1].endswith('/v1/tasks') for row in caller.requests)


if __name__ == '__main__':
    unittest.main()
