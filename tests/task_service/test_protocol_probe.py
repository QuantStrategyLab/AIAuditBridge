"""Fixed Quant text probe uses only synthetic inputs and local protocol fixtures."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_service.client import TaskClient
from ai_service.events import DotEventProvider, EventSubscriptions
from ai_service.runtime import Policy, TaskRuntime
from ai_service.scenarios import ContractError, load_instance
from ai_service.scenarios.pilot import digest, prepare, submit, verify
from ai_service.server import Application
from test_pilot import AppTransport, SignedCallbackFixture, SyntheticAuthorizer, valid_output

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'examples/pilots/protocol-probe'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def approved():
    instance = fixture('instance.json')
    instance['enabled'] = True
    instance['approvals'] = {'activation_ref': 'offline-test-only'}
    return instance


def prepared(instance=None, manifest=None):
    return prepare(approved() if instance is None else instance,
                   fixture('input.json') if manifest is None else manifest,
                   occurrence='quant-probe-offline', submitted_at=datetime.now(timezone.utc))


class ProtocolProbeTests(unittest.TestCase):
    def test_disabled_instance_cannot_prepare_or_submit(self):
        self.assertFalse(load_instance(fixture('instance.json'))['enabled'])
        with self.assertRaises(ContractError):
            prepared(instance=fixture('instance.json'))

    def test_fixed_result_and_unchanged_v2_envelope(self):
        task = prepared()
        self.assertEqual(set(task.request), {'mode', 'profile', 'model', 'objective',
                                            'materials', 'output_schema', 'timeout_seconds'})
        self.assertEqual(task.request['mode'], 'agent')
        self.assertEqual(task.request['timeout_seconds'], 120)
        self.assertEqual(valid_output(task.request['output_schema'])['result'], {'probe': 'ok'})

    def test_arbitrary_materials_and_wrong_probe_are_rejected(self):
        for manifest in [None, [], {}, {'probe': 'ok'},
                         {**fixture('input.json'), 'url': 'https://example.com'},
                         {**fixture('input.json'), 'expected_output': {'probe': 'changed'}},
                         {**fixture('input.json'), 'expected_output': {'probe': 'ok', 'publish': True}}]:
            with self.subTest(manifest=manifest), self.assertRaises(ContractError):
                prepare(approved(), manifest, occurrence='quant-probe-invalid',
                        submitted_at=datetime.now(timezone.utc))

    def test_only_synthetic_single_dot_stage_is_accepted(self):
        for classification in ['public', 'sanitized', 'restricted']:
            instance = approved()
            instance['inputs']['data_classification'] = classification
            instance['approvals']['data_use_ref'] = 'offline-only'
            with self.subTest(classification=classification), self.assertRaises(ContractError):
                prepared(instance=instance)
        instance = approved()
        instance['stages'][0]['worker_type'] = 'api'
        with self.assertRaises(ContractError):
            prepared(instance=instance)
        instance = approved()
        instance['stages'][0]['worker_type'] = 'grok'
        with self.assertRaises(ContractError):
            prepared(instance=instance)

    def test_disabled_cli_dry_run_is_offline_and_not_dispatchable(self):
        from ai_service.scenarios import pilot_cli
        args = ['pilot', 'dry-run', '--instance', str(FIXTURES / 'instance.json'),
                '--input', str(FIXTURES / 'input.json'), '--occurrence', 'quant-probe-dry-run',
                '--submitted-at', '2026-10-16T00:00:00Z']
        with patch.object(sys, 'argv', args), patch.object(pilot_cli, 'client_from_env') as client, patch('builtins.print') as output:
            self.assertEqual(pilot_cli.main(), 0)
            self.assertFalse(json.loads(output.call_args.args[0])['dispatchable'])
            client.assert_not_called()

    def test_candidate_reuses_existing_rules_without_mutating_config(self):
        spec = importlib.util.spec_from_file_location('quant-probe_candidate', ROOT / 'scripts/prepare_pilot_config.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = json.loads((ROOT / 'config/quant.example.json').read_text())
        config['enabled'] = True
        config['projects']['example-org/ExampleProject'] = copy.deepcopy(config['projects']['example-org/ExampleResearch'])
        original = copy.deepcopy(config)
        grant = {'issuer': 'https://CONFIGURE_IDENTITY_PROVIDER', 'subject': 'synthetic-subject',
                 'identity': 'synthetic-submitter', 'project': 'example-org/ExampleProject',
                 'scopes': ['tasks:read', 'tasks:submit'], 'claims': {'azp': 'synthetic-client'}}
        candidate, instance = module.build(config, fixture('instance.json'), grant,
            owner='example-org', project=grant['project'], worker_identity='quant-dot',
            activation_ref='offline-only', revision='a' * 40)
        self.assertEqual(config, original)
        self.assertEqual(candidate['grants'][:-1], original['grants'])
        self.assertEqual(candidate['projects'], original['projects'])
        self.assertTrue(instance['enabled'])
        self.assertEqual(prepared(instance=instance).spec['template'], 'protocol_probe')

    def test_signed_event_claim_report_readback_and_cleanup_without_network(self):
        with tempfile.TemporaryDirectory() as directory, patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            runtime = TaskRuntime(Path(directory) / 'state.sqlite', {}, {
                'example-org/ExampleProject': Policy({'agent': 'dot'}, {'dot': {'configured-dot'}}),
                'other-org/OtherProject': Policy({'agent': 'dot'}, {'dot': {'configured-dot'}})})
            callback = SignedCallbackFixture()
            events = EventSubscriptions(runtime, transport=callback,
                can_access=lambda identity, project, provider: identity == 'dot-worker' and project == 'example-org/ExampleProject' and provider == 'dot')
            runtime.providers['dot'] = DotEventProvider(events)
            app = Application(runtime, SyntheticAuthorizer(), events)
            client = TaskClient('https://service.example', 'synthetic-submitter', transport=AppTransport(app))

            def rpc(name, args, identity='synthetic-dot'):
                return app.dispatch('POST', '/mcp', {'Authorization': 'Bearer ' + identity},
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name,
                     'arguments': {'project': 'example-org/ExampleProject', **args}}})[1]

            def call(name, args):
                reply = rpc(name, args)
                self.assertNotIn('error', reply)
                return reply['result']['structuredContent']

            # Fixed synthetic signing material exists only in the offline fixture.
            import base64
            subscription = events.subscribe('dot-worker', {'name': 'task.requested',
                'arguments': {'project': 'example-org/ExampleProject'}, 'ttlMs': 600000,
                'delivery': {'mode': 'webhook', 'url': 'https://callback.example/mcp',
                             'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()}})
            sub_id = subscription['id']
            try:
                self.assertEqual(call('get_event_subscription', {'subscription_id': sub_id})['status'], 'active')
                task = prepared()
                record = submit(client, task)
                self.assertEqual(submit(client, task)['id'], record['id'])
                runtime.tick()
                events.flush()
                self.assertTrue(any(record['id'] in json.dumps(value) for value in callback.calls))
                args = {'task_id': record['id'], 'attempt_id': 'quant-probe-stable-attempt'}
                claimed = call('claim_task', args)
                self.assertEqual(call('claim_task', args)['attempt_id'], claimed['attempt_id'])
                self.assertIn('error', rpc('claim_task', {**args, 'attempt_id': 'wrong'}))
                self.assertIn('error', rpc('get_task', {'task_id': record['id']}, 'synthetic-cross-owner'))
                self.assertIn('error', rpc('get_task', {'task_id': record['id']}, 'synthetic-wrong-provider'))
                output = valid_output(task.request['output_schema'])
                report = {**args, 'revision': claimed['revision'], 'status': 'completed', 'output': output}
                self.assertIn('error', rpc('report_task_result', {**report, 'revision': 2}))
                self.assertEqual(call('report_task_result', report)['status'], 'completed')
                readback = client.get(record['id'])
                self.assertEqual(verify(task, readback, task_id=record['id'], provider='dot')['output_sha256'], digest(output))
                for bad in [{'probe': 'wrong'}, {'probe': 'ok', 'publish': True}, {}]:
                    tampered = {**readback, 'output': {**output, 'result': bad}}
                    with self.subTest(result=bad), self.assertRaises(ContractError):
                        verify(task, tampered, task_id=record['id'], provider='dot')
            finally:
                self.assertEqual(call('delete_event_subscription', {'subscription_id': sub_id})['status'], 'gone')
                self.assertEqual(call('get_event_subscription', {'subscription_id': sub_id})['status'], 'gone')
                count = len(callback.calls)
                events.flush()
                self.assertEqual(len(callback.calls), count)
