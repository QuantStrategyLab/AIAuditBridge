"""One connection may work an explicit list without gaining submit privileges."""
import base64
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_service.auth import AuthenticationError, JWTAuthorizer
from ai_service.events import DotEventProvider, EventSubscriptions
from ai_service.runtime import Policy, TaskRuntime
from ai_service.server import Application, validate_configuration
from ai_service.transport import HttpReply
from test_dot_events import Callback
from test_task_runtime import CallbackProvider


class WorkerProjectAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid='test-key', use='sig', alg='RS256')

    def setUp(self):
        class Transport:
            def request(inner, *args, **kwargs):
                return HttpReply(200, json.dumps({'keys': [self.jwk]}).encode())
        self.authorizer = JWTAuthorizer('https://service.example/mcp',
            {'https://issuer.example': {'jwks_url': 'https://issuer.example/jwks'}},
            [{'issuer': 'https://issuer.example', 'subject': 'bot-1', 'identity': 'worker-1',
              'project': 'owner/repo', 'provider': 'dot', 'scopes': ['tasks:read', 'tasks:work', 'events:subscribe'],
              'claims': {'connection': 'dedicated-worker'}}], transport=Transport())
        self.claims = {'iss': 'https://issuer.example', 'sub': 'bot-1', 'aud': 'https://service.example/mcp',
                       'iat': int(time.time()), 'exp': int(time.time()) + 60,
                       'scope': 'tasks:read tasks:work', 'connection': 'dedicated-worker'}
        self.grant = self.authorizer.grants[0]
        self.grant.pop('project')
        self.grant['projects'] = ['owner/repo', 'owner/second']
        self.grant['claims'] = {'azp': 'worker-client'}
        self.claims['azp'] = 'worker-client'

    def token(self, claims):
        return 'Bearer ' + jwt.encode(claims, self.key, algorithm='RS256', headers={'kid': 'test-key'})

    def test_one_token_authenticates_for_both_projects_with_scope_intersection(self):
        principal = self.authorizer.authenticate(self.token(dict(self.claims, scope='tasks:work tasks:submit')))
        self.assertIsNone(principal.project)
        self.assertEqual(principal.allowed_projects, frozenset({'owner/repo', 'owner/second'}))
        self.assertEqual(principal.scopes, frozenset({'tasks:work'}))
        self.assertTrue(self.authorizer.can_access_events('worker-1', 'owner/second', 'dot'))
        self.grant['projects'].remove('owner/second')
        self.assertFalse(self.authorizer.can_access_events('worker-1', 'owner/second', 'dot'))

    def test_invalid_lists_and_non_worker_multi_project_grants_are_rejected(self):
        variants = [{'projects': []}, {'projects': 'owner/repo'}, {'projects': ['owner/*']},
                    {'projects': ['owner/repo', 'other/repo']},
                    {'projects': ['owner/repo', 'owner/repo']}, {'projects': [None]},
                    {'project': 'owner/repo'}, {'provider': None}, {'claims': {}},
                    {'claims': {'azp': None}}, {'claims': {'azp': ''}},
                    {'scopes': ['tasks:work', 'tasks:submit']}, {'scopes': ['tasks:read']},
                    {'issuer': 'https://token.actions.githubusercontent.com'}]
        for change in variants:
            grant = {**self.grant, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                JWTAuthorizer(self.authorizer.resource, self.authorizer.issuers, [grant])

    def test_duplicate_matching_grants_still_fail_closed(self):
        self.authorizer.grants.append(copy.deepcopy(self.grant))
        with self.assertRaises(AuthenticationError):
            self.authorizer.authenticate(self.token(self.claims))

    def test_wrong_connection_and_audience_are_rejected(self):
        for claims in [dict(self.claims, azp='other-client'), dict(self.claims, aud='other-service')]:
            with self.assertRaises(AuthenticationError):
                self.authorizer.authenticate(self.token(claims))

    def test_unadmitted_list_member_is_rejected_before_state_is_opened(self):
        config = {'enabled': True, 'resource': self.authorizer.resource,
                  'issuers': self.authorizer.issuers, 'grants': [self.grant],
                  'state_path': '/unused/state.sqlite', 'providers': {'dot': {'type': 'dot_events'}},
                  'projects': {'owner/repo': {'providers': {'agent': 'dot'}, 'models': {'dot': ['configured']}}}}
        with self.assertRaises(ValueError):
            validate_configuration(config)


class WorkerProjectServerTests(unittest.TestCase):
    def setUp(self):
        from ai_service.auth import Principal
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        projects = ['owner/repo', 'owner/second', 'other/repo']
        self.runtime = TaskRuntime(Path(temporary.name) / 'state.sqlite', {},
            {project: Policy({'agent': 'dot', 'agent:other': 'grok'},
                             {'dot': {'configured'}, 'grok': {'configured'}}) for project in projects})
        self.transport = Callback()
        self.grant = {'issuer': 'https://issuer.example', 'subject': 'user', 'identity': 'worker',
                      'projects': projects[:2], 'provider': 'dot', 'claims': {'azp': 'worker-client'},
                      'scopes': ['tasks:read', 'tasks:work', 'events:subscribe']}
        auth = JWTAuthorizer('https://service.example/mcp', {}, [self.grant])
        self.principal = Principal('worker', None, frozenset(self.grant['scopes']), 'dot',
                                   projects=frozenset(projects[:2]))
        auth.authenticate = lambda _: self.principal
        self.events = EventSubscriptions(self.runtime, transport=self.transport, can_access=auth.can_access_events)
        self.runtime.providers.update(dot=DotEventProvider(self.events), grok=CallbackProvider())
        self.app = Application(self.runtime, auth, self.events)
        self.request = {'mode': 'agent', 'model': 'configured', 'objective': 'synthetic',
                        'output_schema': {'type': 'object'}}
        self.secret = 'whsec_' + base64.b64encode(b'x' * 32).decode()

    def rpc(self, method, params):
        return self.app.dispatch('POST', '/mcp', {'Authorization': 'test'},
            {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})[1]

    def tool(self, name, **arguments):
        return self.rpc('tools/call', {'name': name, 'arguments': arguments})

    def subscribe(self, project):
        return self.rpc('events/subscribe', {'name': 'task.requested', 'arguments': {'project': project},
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example/mcp', 'secret': self.secret}, 'ttlMs': 900000})

    def queue(self, project, **changes):
        task = self.runtime.submit(project, 'task-' + str(changes), {**self.request, **changes})
        self.runtime.tick()
        return task

    def test_each_project_can_subscribe_claim_and_report_through_one_connection(self):
        tools = self.rpc('tools/list', {})['result']['tools']
        for tool in tools:
            self.assertIn('project', tool['inputSchema']['required'])
            self.assertEqual(tool['inputSchema']['properties']['project']['enum'], ['owner/repo', 'owner/second'])
        for project in ['owner/repo', 'owner/second']:
            subscription = self.subscribe(project)['result']['id']
            task = self.queue(project)
            claimed = self.tool('claim_task', project=project, task_id=task['id'], attempt_id='attempt')
            self.assertEqual(claimed['result']['structuredContent']['owner'], project)
            reported = self.tool('report_task_result', project=project, task_id=task['id'], attempt_id='attempt',
                                 revision=1, status='completed', output={})
            self.assertEqual(reported['result']['structuredContent']['status'], 'completed')
            status = self.tool('get_event_subscription', project=project, subscription_id=subscription)
            self.assertEqual(status['result']['structuredContent']['status'], 'active')
            deleted = self.tool('delete_event_subscription', project=project, subscription_id=subscription)
            self.assertEqual(deleted['result']['structuredContent']['status'], 'gone')

    def test_wrong_project_provider_missing_selector_and_submit_are_denied(self):
        self.subscribe('owner/repo')
        task = self.queue('owner/repo')
        grok = self.queue('owner/repo', profile='other')
        for arguments in [{'task_id': task['id']}, {'project': 'owner/second', 'task_id': task['id']},
                          {'project': 'other/repo', 'task_id': task['id']},
                          {'project': 'owner/repo', 'task_id': grok['id']}]:
            self.assertIn('error', self.tool('get_task', **arguments))
        with self.assertRaises(PermissionError):
            self.app.dispatch('POST', '/v1/tasks?project=owner/repo',
                              {'Authorization': 'test', 'Idempotency-Key': 'forbidden'}, self.request)
        self.assertIn('error', self.tool('submit_task', project='owner/repo', **self.request))

    def test_subscription_is_bound_to_project_and_current_grant(self):
        subscription = self.subscribe('owner/repo')['result']['id']
        self.assertIn('error', self.subscribe('other/repo'))
        self.assertIn('error', self.tool('delete_event_subscription', project='owner/second', subscription_id=subscription))
        self.queue('owner/repo')
        self.queue('other/repo')
        self.events.flush()
        deliveries = [payload for _, payload, _ in self.transport.calls if payload.get('name') == 'task.requested']
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]['data']['project'], 'owner/repo')
        self.grant['projects'].remove('owner/repo')
        self.events.flush()
        self.assertFalse(self.events.has_active('owner/repo'))

    def test_rest_requires_one_explicit_admitted_project(self):
        self.subscribe('owner/repo')
        task = self.queue('owner/repo')
        target = '/v1/tasks/' + task['id']
        self.assertEqual(self.app.dispatch('GET', target + '?project=owner/repo', {'Authorization': 'test'})[1]['id'], task['id'])
        for query in ['', '?project=other/repo', '?project=owner/repo&project=owner/second']:
            with self.assertRaises((ValueError, PermissionError)):
                self.app.dispatch('GET', target + query, {'Authorization': 'test'})

    def test_policy_removal_denies_existing_project_selection(self):
        self.runtime.policies.pop('owner/second')
        self.assertIn('error', self.subscribe('owner/second'))

    def test_grok_worker_uses_same_list_without_dot_event_or_submit_permissions(self):
        from dataclasses import replace
        self.principal = replace(self.principal, identity='grok-worker', provider='grok',
                                 scopes=frozenset({'tasks:read', 'tasks:work'}))
        self.assertIn('error', self.subscribe('owner/repo'))
        self.assertEqual({tool['name'] for tool in self.rpc('tools/list', {})['result']['tools']},
                         {'get_task', 'claim_task', 'report_task_result'})
        for project in ['owner/repo', 'owner/second']:
            task = self.queue(project, profile='other')
            result = self.tool('claim_task', project=project, task_id=task['id'], attempt_id='grok-attempt')
            self.assertEqual(result['result']['structuredContent']['worker'], 'grok-worker')
            result = self.tool('report_task_result', project=project, task_id=task['id'],
                               attempt_id='grok-attempt', revision=1, status='completed', output={})
            self.assertEqual(result['result']['structuredContent']['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
