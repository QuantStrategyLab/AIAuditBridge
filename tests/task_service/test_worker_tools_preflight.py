"""Offline owner-worker checks use the same schema builder as the live service."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_service.auth import Principal
from ai_service.server import Application, check_worker_tools, main, validate_configuration
from ai_service.worker_tools import tools_schema_sha256
from test_configuration_check import configuration


class WorkerToolsPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = configuration(self.root / 'never-created' / 'state.sqlite')
        policy = self.config['projects'].pop('owner/project')
        self.projects = ['example-org/Alpha', 'example-org/Beta']
        self.config['projects'] = {project: copy.deepcopy(policy) for project in self.projects}
        self.config['owner'] = 'example-org'
        self.config['grants'] = [{'issuer': 'https://issuer.example', 'subject': 'private-subject',
            'identity': 'example-worker', 'owner': 'example-org', 'provider': 'dot',
            'claims': {'azp': 'example-client'},
            'scopes': ['tasks:read', 'tasks:work', 'events:subscribe']}]

    def check(self, projects=None):
        return check_worker_tools(self.config, 'example-worker', self.projects if projects is None else projects)

    def invoke(self, *, mode=0o600, guard=None, selectors=None):
        path = self.root / 'private.json'
        path.write_text(json.dumps(self.config))
        path.chmod(mode)
        argv = ['ai-service', '--config', str(path), '--check-worker-tools',
                '--worker-identity', 'example-worker']
        for project in self.projects if selectors is None else selectors:
            argv.extend(['--expect-project', project])
        output = io.StringIO()
        with patch('sys.argv', argv), contextlib.redirect_stdout(output):
            try:
                main(guard)
                code = 0
            except SystemExit as error:
                code = error.code
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_no_database_provider_credential_network_server_or_config_mutation(self):
        before = copy.deepcopy(self.config)
        environment_get = os.environ.get
        def guarded_environment(name, *args):
            if name.startswith('PRIVATE_'):
                raise AssertionError('provider credential environment read')
            return environment_get(name, *args)
        with patch('ai_service.server.build_application', side_effect=AssertionError('runtime created')), \
             patch('ai_service.server.TaskRuntime', side_effect=AssertionError('state opened')), \
             patch('ai_service.server.make_server', side_effect=AssertionError('port bound')), \
             patch('ai_service.server.os.environ.get', side_effect=guarded_environment), \
             patch('socket.create_connection', side_effect=AssertionError('network used')), \
             patch('ai_service.transport.PublicHTTPS.request', side_effect=AssertionError('HTTP used')):
            code, result, raw = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(result['valid'])
        self.assertEqual(result['tool_count'], 5)
        self.assertFalse(result['real_oauth_verified'])
        self.assertFalse(result['running_process_verified'])
        self.assertRegex(result['config_sha256'], r'^[a-f0-9]{64}$')
        self.assertEqual(self.config, before)
        self.assertFalse((self.root / 'never-created').exists())
        for private in ['private-subject', 'example-org', 'example-worker', 'PRIVATE_', 'issuer.example']:
            self.assertNotIn(private, raw)

    def test_preflight_digest_matches_actual_tools_list_branch(self):
        # Only the existing pure branch is exercised; no Application constructor.
        app = Application.__new__(Application)
        _, policies = validate_configuration(self.config)
        from types import SimpleNamespace
        app.runtime = SimpleNamespace(policies=policies)
        app.events = SimpleNamespace(provider_name='dot')
        principal = Principal('example-worker', None, frozenset(self.config['grants'][0]['scopes']),
                              'dot', owner='example-org')
        tools = app.mcp(principal, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        self.assertEqual(self.check()['tools_schema_sha256'], tools_schema_sha256(tools))

    def test_missing_policy_shrinks_enum_and_new_policy_enters_without_worker_edit(self):
        grant = copy.deepcopy(self.config['grants'])
        policy = self.config['projects'].pop(self.projects[1])
        with self.assertRaises(ValueError):
            self.check()
        self.assertTrue(self.check(self.projects[:1])['valid'])
        self.config['projects'][self.projects[1]] = policy
        future = 'example-org/Future'
        self.config['projects'][future] = copy.deepcopy(policy)
        with self.assertRaises(ValueError):
            self.check()
        self.assertTrue(self.check(self.projects + [future])['valid'])
        self.assertEqual(self.config['grants'], grant)

    def test_rejects_cross_owner_submit_wrong_provider_owner_and_ambiguous_identity(self):
        before = copy.deepcopy(self.config)
        changes = [lambda c: c.update(owner='other'),
                   lambda c: c['projects'].update({'other/Repo': next(iter(c['projects'].values()))}),
                   lambda c: c['grants'][0].update(owner='other'),
                   lambda c: c['grants'][0]['scopes'].append('tasks:submit'),
                   lambda c: c['grants'][0].update(provider='grok'),
                   lambda c: c['grants'][0].update(identity='another-worker'),
                   lambda c: c['grants'].append(copy.deepcopy(c['grants'][0]))]
        for change in changes:
            self.config = copy.deepcopy(before)
            change(self.config)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.check()

    def test_exact_expected_set_and_owner_only_permissions_with_redacted_failures(self):
        for projects in [[], self.projects[:1], self.projects + ['other/Repo'], self.projects * 2]:
            with self.subTest(projects=projects), self.assertRaises(ValueError):
                self.check(projects)
        for mode, projects in [(0o640, self.projects), (0o600, ['private-project'])]:
            code, result, raw = self.invoke(mode=mode, selectors=projects)
            self.assertEqual(code, 1)
            self.assertFalse(result['valid'])
            self.assertNotIn('private-project', raw)
            self.assertNotIn('private-subject', raw)
        def reject(_):
            raise ValueError('private guard detail')
        code, result, raw = self.invoke(guard=reject)
        self.assertEqual(code, 1)
        self.assertNotIn('private guard', raw)

    def test_cli_requires_selectors_and_keeps_them_out_of_normal_startup(self):
        for argv in [[], ['--worker-identity', 'example-worker'],
                     ['--check-worker-tools'], ['--check-worker-tools', '--expect-project', 'example-org/Alpha'],
                     ['--check-worker-tools', '--check-config']]:
            with self.subTest(argv=argv), patch('sys.argv', ['ai-service', '--config', 'unused'] + argv), \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
