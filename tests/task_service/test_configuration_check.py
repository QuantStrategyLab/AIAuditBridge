"""Configuration checks must not start a dispatcher or consume credentials."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_service.server import build_application, main, validate_configuration


def configuration(state_path):
    return {'enabled': True, 'resource': 'https://service.example/mcp', 'issuers': {}, 'grants': [],
            'state_path': str(state_path), 'projects': {'owner/project': {
                'providers': {'agent': 'dot'}, 'models': {'dot': ['configured']}}},
            'providers': {'dot': {'type': 'dot_events'},
                          'grok': {'type': 'grok_webhook', 'url': 'https://callback.example/trigger', 'key_env': 'PRIVATE_KEY'},
                          'plan': {'type': 'chatgpt_plan_oauth', 'token_env': 'PRIVATE_TOKEN', 'plan_usage_authorized': True},
                          'gemini': {'type': 'gemini_api', 'enabled': True, 'model': 'fixed-model',
                                     'key_env': 'PRIVATE_GEMINI_KEY', 'api_usage_authorized': True}}}


class ConfigurationCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = configuration(self.root / 'never-created' / 'state.sqlite')

    def invoke(self, config, *, mode=0o600, guard=None):
        path = self.root / 'private.json'
        path.write_text(json.dumps(config))
        path.chmod(mode)
        output = io.StringIO()
        with patch('sys.argv', ['ai-service', '--check-config', '--config', str(path)]), contextlib.redirect_stdout(output):
            try:
                main(guard)
                code = 0
            except SystemExit as error:
                code = error.code
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_check_has_no_state_network_server_or_environment_access(self):
        before = copy.deepcopy(self.config)
        environment_get = os.environ.get
        def guarded_environment(name, *args):
            if name.startswith('PRIVATE_'):
                raise AssertionError('provider credential environment read')
            return environment_get(name, *args)
        with patch('ai_service.server.TaskRuntime', side_effect=AssertionError('state opened')), \
             patch('ai_service.server.make_server', side_effect=AssertionError('port bound')), \
             patch('ai_service.server.os.environ.get', side_effect=guarded_environment), \
             patch('ai_service.transport.PublicHTTPS.request', side_effect=AssertionError('network used')):
            code, summary, output = self.invoke(self.config)
        self.assertEqual(code, 0)
        self.assertEqual(summary, {'valid': True, 'projects': 1, 'providers': 4})
        self.assertEqual(self.config, before)
        self.assertFalse((self.root / 'never-created').exists())
        for private in ('PRIVATE', 'owner/project', 'service.example', 'callback.example'):
            self.assertNotIn(private, output)

    def test_rejects_invalid_configuration_without_disclosing_values(self):
        changes = [lambda c: c.update(enabled=False),
                   lambda c: c['providers'].update(second={'type': 'dot_events'}),
                   lambda c: c['providers']['dot'].update(type='private-unsupported-provider'),
                   lambda c: c['providers']['gemini'].update(api_usage_authorized=False),
                   lambda c: c['providers']['plan'].update(plan_usage_authorized=False),
                   lambda c: c['projects']['owner/project'].pop('models'),
                   lambda c: c.update(mcp_log_level='private-invalid-level'),
                   lambda c: c.update(issuers={'https://private.example': {'require_scope': False}})]
        for change in changes:
            config = copy.deepcopy(self.config)
            change(config)
            with self.subTest(change=change):
                code, summary, output = self.invoke(config)
                self.assertEqual(code, 1)
                self.assertFalse(summary['valid'])
                self.assertNotIn('private', output)
                with self.assertRaises((ValueError, KeyError)):
                    build_application(config)
                self.assertFalse((self.root / 'never-created').exists())

    def test_owner_only_permissions_and_guard_are_enforced(self):
        self.assertEqual(self.invoke(self.config, mode=0o640)[0], 1)
        def reject(_):
            raise ValueError('private guard details')
        code, summary, output = self.invoke(self.config, guard=reject)
        self.assertEqual(code, 1)
        self.assertNotIn('private guard', output)

    def test_malformed_json_and_missing_file_return_json_exit_one(self):
        path = self.root / 'broken.json'
        for content in ('not json', None):
            if content:
                path.write_text(content)
                path.chmod(0o600)
            elif path.exists():
                path.unlink()
            output = io.StringIO()
            with patch('sys.argv', ['ai-service', '--check-config', '--config', str(path)]), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as error:
                    main()
            self.assertEqual(error.exception.code, 1)
            self.assertFalse(json.loads(output.getvalue())['valid'])

    def test_validation_returns_the_same_authorizer_and_policies_used_by_build(self):
        self.config['providers'] = {'dot': {'type': 'dot_events'}}
        auth, policies = validate_configuration(self.config)
        with patch('ai_service.server.validate_configuration', return_value=(auth, policies)) as validate:
            app = build_application(self.config)
        validate.assert_called_once_with(self.config)
        self.assertIs(app.auth, auth)
        self.assertIs(app.runtime.policies, policies)
