"""Offline submitter/Dot fixtures exercise the real client, API, runtime and events."""
import base64
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from ai_service.auth import AuthenticationError, Principal
from ai_service.client import TaskClient
from ai_service.events import DotEventProvider, EventSubscriptions
from ai_service.runtime import Conflict, Policy, TaskRuntime
from ai_service.scenarios import ContractError, load_instance
from ai_service.scenarios.pilot import digest, prepare, submit, verify
from ai_service.server import Application
from ai_service.transport import HttpReply

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)


def fixture(name):
    return json.loads((ROOT / 'examples/pilots' / name).read_text())


def approved():
    value = fixture('instance.json')
    value['enabled'] = True
    value['approvals'] = {'activation_ref': 'offline-test-only'}
    return value


def prepared(value=None, manifest=None):
    return prepare(approved() if value is None else value, fixture('input.json') if manifest is None else manifest,
                   occurrence='offline-fixture-1', submitted_at=NOW)


class SyntheticAuthorizer:
    resource = 'https://service.example/mcp'
    principals = {
        'Bearer synthetic-submitter': Principal('submitter', 'example-org/ExampleProject', frozenset({'tasks:read', 'tasks:submit'})),
        'Bearer synthetic-dot': Principal('dot-worker', None, frozenset({'tasks:read', 'tasks:work', 'events:subscribe'}), 'dot', owner='example-org'),
        'Bearer synthetic-wrong-provider': Principal('wrong-provider', None, frozenset({'tasks:read', 'tasks:work'}), 'grok', owner='example-org'),
        'Bearer synthetic-cross-owner': Principal('cross-owner', 'other-org/OtherProject', frozenset({'tasks:read', 'tasks:work'}), 'dot'),
    }
    def authenticate(self, header):
        if header not in self.principals:
            raise AuthenticationError('synthetic identity required')
        return self.principals[header]


class AppTransport:
    def __init__(self, app):
        self.app = app
    def request(self, url, *, method, headers, body):
        parsed = urlsplit(url)
        status, result = self.app.dispatch(method, parsed.path, headers, json.loads(body) if body else None)
        return HttpReply(status, json.dumps(result).encode())


class SignedCallbackFixture:
    def __init__(self):
        self.calls = []
    def request(self, url, **kwargs):
        headers, raw = kwargs['headers'], kwargs['body']
        message = (headers['webhook-id'] + '.' + headers['webhook-timestamp'] + '.').encode() + raw
        expected = 'v1,' + base64.b64encode(hmac.new(b'x' * 32, message, hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(expected, headers['webhook-signature']):
            raise AssertionError('invalid event signature')
        payload = json.loads(raw)
        self.calls.append(payload)
        return HttpReply(200, json.dumps({'challenge': payload['challenge']} if payload.get('type') == 'verification' else {}).encode())


def valid_output(schema):
    if 'const' in schema:
        return schema['const']
    if 'enum' in schema:
        return schema['enum'][0]
    if schema.get('type') == 'object':
        return {key: valid_output(value) for key, value in schema['properties'].items()}
    if schema.get('type') == 'array':
        return [valid_output(value) for value in schema['prefixItems']]
    return 'Synthetic evidence reviewed; advisory only.'


class PilotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.network = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.runtime = TaskRuntime(Path(self.tmp.name) / 'state.sqlite', {}, {
            'example-org/ExampleProject': Policy({'agent': 'dot'}, {'dot': {'configured-dot'}}),
            'other-org/OtherProject': Policy({'agent': 'dot'}, {'dot': {'configured-dot'}})})
        self.callback = SignedCallbackFixture()
        self.events = EventSubscriptions(self.runtime, transport=self.callback,
            can_access=lambda identity, project, provider: identity == 'dot-worker' and project == 'example-org/ExampleProject' and provider == 'dot')
        self.runtime.providers['dot'] = DotEventProvider(self.events)
        self.app = Application(self.runtime, SyntheticAuthorizer(), self.events)
        self.client = TaskClient('https://service.example', 'synthetic-submitter', transport=AppTransport(self.app))

    def rpc(self, name, args, identity='synthetic-dot'):
        reply = self.app.dispatch('POST', '/mcp', {'Authorization': 'Bearer ' + identity},
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
             'params': {'name': name, 'arguments': {'project': 'example-org/ExampleProject', **args}}})[1]
        return reply

    def call(self, name, args):
        reply = self.rpc(name, args)
        self.assertNotIn('error', reply)
        return reply['result']['structuredContent']

    def test_submit_signed_event_dot_claim_report_caller_verify_and_cleanup(self):
        params = {'name': 'task.requested', 'arguments': {'project': 'example-org/ExampleProject'},
                  'delivery': {'mode': 'webhook', 'url': 'https://callback.example/mcp',
                               'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()}, 'ttlMs': 600000}
        subscription = self.app.dispatch('POST', '/mcp', {'Authorization': 'Bearer synthetic-dot'},
            {'jsonrpc': '2.0', 'id': 1, 'method': 'events/subscribe', 'params': params})[1]['result']
        sub_id = subscription['id']
        self.assertEqual(self.call('get_event_subscription', {'subscription_id': sub_id})['status'], 'active')
        try:
            task = prepared()
            record = submit(self.client, task)
            self.assertEqual(submit(self.client, task)['id'], record['id'])
            self.runtime.tick()
            self.events.flush()
            self.assertTrue(any(record['id'] in json.dumps(value) for value in self.callback.calls))
            claimed = self.call('claim_task', {'task_id': record['id'], 'attempt_id': 'stable-offline-attempt'})
            # Repeated delivery reuses the same task and attempt.
            self.assertEqual(self.call('claim_task', {'task_id': record['id'], 'attempt_id': 'stable-offline-attempt'})['id'], record['id'])
            self.assertIn('error', self.rpc('claim_task', {'task_id': record['id'], 'attempt_id': 'different-attempt'}))
            output = valid_output(task.request['output_schema'])
            args = {'task_id': record['id'], 'attempt_id': claimed['attempt_id'], 'revision': claimed['revision'], 'status': 'completed', 'output': output}
            self.assertIn('error', self.rpc('report_task_result', {**args, 'revision': 2}))
            self.assertIn('error', self.rpc('report_task_result', {**args, 'attempt_id': 'wrong'}))
            self.assertIn('error', self.rpc('get_task', {'task_id': record['id']}, 'synthetic-wrong-provider'))
            self.assertIn('error', self.rpc('get_task', {'task_id': record['id']}, 'synthetic-cross-owner'))
            with self.assertRaises(PermissionError):
                TaskClient('https://service.example', 'synthetic-dot', transport=AppTransport(self.app)).submit(task.request, 'forbidden')
            self.assertEqual(self.call('report_task_result', args)['status'], 'completed')
            self.assertEqual(self.call('report_task_result', args)['status'], 'completed')
            readback = self.client.get(record['id'])
            receipt = verify(task, readback, task_id=record['id'], provider='dot')
            self.assertEqual(receipt['output_sha256'], digest(output))
            for key, value in [('id', 'different'), ('owner', 'other-org/OtherProject'), ('provider', 'grok'),
                               ('revision', 2), ('attempt', None), ('status', 'outcome_unknown'), ('output', {})]:
                with self.subTest(key=key), self.assertRaises(ContractError):
                    verify(task, {**readback, key: value}, task_id=record['id'], provider='dot')
            changed = copy.deepcopy(fixture('input.json'))
            changed['series'][-1] += 1
            with self.assertRaises(Conflict):
                submit(self.client, prepared(manifest=changed))
        finally:
            self.assertEqual(self.call('delete_event_subscription', {'subscription_id': sub_id})['status'], 'gone')
            self.assertEqual(self.call('get_event_subscription', {'subscription_id': sub_id})['status'], 'gone')
            self.assertFalse(self.events.has_active('example-org/ExampleProject'))
            count = len(self.callback.calls)
            self.events.flush()
            self.assertEqual(len(self.callback.calls), count)

    def test_disabled_sample_and_invalid_output_never_verify(self):
        self.assertFalse(load_instance(fixture('instance.json'))['enabled'])
        with self.assertRaises(ContractError):
            prepared(fixture('instance.json'))
        task = prepared()
        record = submit(self.client, task)
        # No subscription means no execution; the provider fails rather than polling.
        self.runtime.tick()
        with self.assertRaises(ContractError):
            verify(task, self.client.get(record['id']), task_id=record['id'], provider='dot')

    def test_cli_unknown_outcome_keeps_durable_intent_and_blocks_resubmission(self):
        import sys
        from unittest.mock import Mock
        from ai_service.scenarios import pilot_cli
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            instance_path, input_path, receipt = directory / 'instance.json', directory / 'input.json', directory / 'intent.json'
            instance_path.write_text(json.dumps(approved()))
            input_path.write_text(json.dumps(fixture('input.json')))
            args = ['pilot', 'submit', '--instance', str(instance_path), '--input', str(input_path),
                    '--occurrence', 'unknown-submit', '--submitted-at', NOW.isoformat(), '--receipt', str(receipt)]
            client = Mock()
            client.submit.side_effect = TimeoutError('synthetic response lost')
            with patch.object(sys, 'argv', args), patch.object(pilot_cli, 'client_from_env', return_value=client), patch('builtins.print'):
                self.assertEqual(pilot_cli.main(), 1)
                self.assertEqual(json.loads(receipt.read_text())['status'], 'intent')
                self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                self.assertFalse(Path(str(receipt) + '.submitted').exists())
                self.assertEqual(pilot_cli.main(), 1)
                self.assertEqual(client.submit.call_count, 1)

    def test_expired_spec_is_not_submitted(self):
        from unittest.mock import Mock
        task = prepare(approved(), fixture('input.json'), occurrence='expired',
                       submitted_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
        client = Mock()
        with self.assertRaises(ContractError):
            submit(client, task)
        client.submit.assert_not_called()

    def test_candidate_preserves_config_and_rejects_scope_escalation(self):
        spec = importlib.util.spec_from_file_location('candidate', ROOT / 'scripts/prepare_pilot_config.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = json.loads((ROOT / 'config/quant.example.json').read_text())
        config['enabled'] = True
        grant = {'issuer': 'https://CONFIGURE_IDENTITY_PROVIDER', 'subject': 'synthetic-submit-subject',
                 'identity': 'synthetic-pilot-submitter', 'project': 'example-org/ExampleProject',
                 'scopes': ['tasks:read', 'tasks:submit'], 'claims': {'azp': 'synthetic-submit-client'}}
        original = copy.deepcopy(config)
        kw = dict(owner='example-org', project='example-org/ExampleProject', worker_identity='quant-dot',
                  activation_ref='offline-test-only', revision='a' * 40)
        candidate, activated = module.build(config, fixture('instance.json'), grant, **kw)
        self.assertEqual(config, original)
        self.assertEqual(candidate['grants'][:-1], original['grants'])
        self.assertTrue(activated['enabled'])
        self.assertEqual({key: candidate['projects'][key] for key in original['projects']}, original['projects'])
        for bad in [{**grant, 'scopes': ['tasks:read', 'tasks:submit', 'tasks:work']},
                    {**grant, 'project': 'other-org/OtherProject'}, {**grant, 'claims': {}},
                    {**grant, 'provider': 'dot'}]:
            with self.assertRaises(ValueError):
                module.build(config, fixture('instance.json'), bad, **kw)

    def test_candidate_cli_dry_run_write_hash_pin_and_no_overwrite(self):
        import os
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            config = json.loads((ROOT / 'config/quant.example.json').read_text())
            config['enabled'] = True
            grant = {'issuer': 'https://CONFIGURE_IDENTITY_PROVIDER', 'subject': 'synthetic-sensitive-subject',
                     'identity': 'synthetic-pilot-submitter', 'project': 'example-org/ExampleProject',
                     'scopes': ['tasks:read', 'tasks:submit'], 'claims': {'azp': 'synthetic-submit-client'}}
            original = json.dumps(config).encode()
            config_path, grant_path = directory / 'live.json', directory / 'grant.json'
            config_path.write_bytes(original)
            grant_path.write_text(json.dumps(grant))
            for path in [config_path, grant_path]:
                os.chmod(path, 0o600)
            candidate, instance = directory / 'candidate.json', directory / 'instance.json'
            command = [sys.executable, str(ROOT / 'scripts/prepare_pilot_config.py'),
                '--config', str(config_path), '--expected-config-sha256', hashlib.sha256(original).hexdigest(),
                '--instance', str(ROOT / 'examples/pilots/instance.json'), '--submitter-grant', str(grant_path),
                '--owner', 'example-org', '--project', 'example-org/ExampleProject', '--worker-identity', 'quant-dot',
                '--activation-ref', 'offline-only', '--revision', 'a' * 40,
                '--output-config', str(candidate), '--output-instance', str(instance)]
            dry = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertFalse(candidate.exists())
            self.assertFalse(instance.exists())
            written = subprocess.run(command + ['--write-candidates'], capture_output=True, text=True)
            self.assertEqual(written.returncode, 0, written.stderr)
            self.assertEqual(json.loads(dry.stdout)['candidate_config_sha256'], hashlib.sha256(candidate.read_bytes()).hexdigest())
            self.assertEqual(candidate.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config_path.read_bytes(), original)
            self.assertNotIn(grant['subject'], written.stdout)
            self.assertNotEqual(subprocess.run(command + ['--write-candidates'], capture_output=True).returncode, 0)
            command[command.index('--expected-config-sha256') + 1] = '0' * 64
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_invalid_worker_result_is_rejected_by_runtime_before_readback(self):
        params = {'name': 'task.requested', 'arguments': {'project': 'example-org/ExampleProject'},
                  'delivery': {'mode': 'webhook', 'url': 'https://callback.example/mcp',
                               'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()}}
        subscription = self.events.subscribe('dot-worker', params)
        try:
            task = prepared()
            record = submit(self.client, task)
            self.runtime.tick()
            self.call('claim_task', {'task_id': record['id'], 'attempt_id': 'invalid-result-attempt'})
            result = self.call('report_task_result', {'task_id': record['id'], 'attempt_id': 'invalid-result-attempt',
                'revision': 1, 'status': 'completed', 'output': {}})
            self.assertEqual(result['status'], 'invalid_output')
            self.assertIsNone(result['output'])
            with self.assertRaises(ContractError):
                verify(task, self.client.get(record['id']), task_id=record['id'], provider='dot')
        finally:
            self.events.unsubscribe('dot-worker', subscription['id'])


class SyntheticFixtureTests(unittest.TestCase):
    def test_deterministic_calculation_rejects_orders_and_real_data(self):
        result = valid_output(prepared().request['output_schema'])['result']
        self.assertEqual(result['difference_sum'], 5)
        self.assertEqual(result['sample_count'], 4)
        self.assertTrue(result['no_order'])
        for value in [{**fixture('input.json'), 'no_order': False},
                      {**fixture('input.json'), 'symbol': 'REAL'},
                      {**fixture('input.json'), 'series': [1, True]},
                      {**fixture('input.json'), 'series': [1, float('nan')]}]:
            with self.assertRaises(ContractError):
                prepared(manifest=value)
