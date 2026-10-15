"""Exact owner grants include future repositories without permitting submission."""
import copy
import unittest
from dataclasses import replace

from ai_service.auth import JWTAuthorizer, grant_projects
from ai_service.runtime import Policy
from ai_service.server import validate_configuration
import test_worker_projects as fixtures


class OwnerAuthTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.WorkerProjectAuthTests.setUpClass.__func__)
    token = fixtures.WorkerProjectAuthTests.token

    def setUp(self):
        fixtures.WorkerProjectAuthTests.setUp(self)
        self.grant.pop('projects')
        self.grant['owner'] = 'owner'

    def test_owner_token_selects_current_and_future_repositories_only(self):
        principal = self.authorizer.authenticate(self.token(dict(self.claims, scope='tasks:work tasks:submit')))
        self.assertIsNone(principal.project)
        self.assertEqual(principal.owner, 'owner')
        self.assertEqual(principal.scopes, frozenset({'tasks:work'}))
        for project in ['owner/repo', 'owner/future', 'owner/.github']:
            self.assertTrue(principal.allows_project(project))
            self.assertTrue(self.authorizer.can_access_events('worker-1', project, 'dot'))
        for project in ['other/repo', 'owner-other/repo', 'owner/*', 'owner/repo/extra', 'owner/', 'owner/..']:
            self.assertFalse(principal.allows_project(project))
            self.assertFalse(self.authorizer.can_access_events('worker-1', project, 'dot'))
        self.assertFalse(self.authorizer.can_access_events('worker-1', 'owner/future', 'grok'))

    def test_owner_and_selector_abuse_rejected_at_startup(self):
        changes = [{'owner': '*'}, {'owner': '*/repo'}, {'owner': 'owner/*'},
                   {'owner': ['owner', 'other']}, {'owner': ''}, {'owner': None},
                   {'owner': 'owner/other'}, {'projects': ['owner/repo']}, {'project': 'owner/repo'},
                   {'scopes': ['tasks:work', 'tasks:submit']}, {'scopes': ['tasks:read']},
                   {'claims': {}}, {'provider': None}, {'issuer': 'https://token.actions.githubusercontent.com'}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                JWTAuthorizer(self.authorizer.resource, self.authorizer.issuers, [{**self.grant, **change}])

    def test_service_boundary_checks_both_grants_and_policies_without_state(self):
        config = {'enabled': True, 'owner': 'owner', 'resource': self.authorizer.resource,
                  'issuers': self.authorizer.issuers, 'grants': [copy.deepcopy(self.grant)],
                  'state_path': '/unused/state.sqlite', 'providers': {'dot': {'type': 'dot_events'}},
                  'projects': {'owner/repo': {'providers': {'agent': 'dot'}, 'models': {'dot': ['configured']}}}}
        validate_configuration(config)
        for change in [{'owner': 'other'}, {'owner': '*'},
                       {'projects': {'other/repo': config['projects']['owner/repo']}},
                       {'grants': [{**self.grant, 'owner': 'other'}]}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_configuration({**config, **change})
        config.pop('owner')
        config['projects']['other/repo'] = config['projects']['owner/repo']
        with self.assertRaises(ValueError):
            validate_configuration(config)

    def test_legacy_single_and_explicit_lists_remain_compatible(self):
        grant = {**self.grant, 'projects': ['owner/repo', 'owner/second']}
        grant.pop('owner')
        self.assertEqual(grant_projects(grant), frozenset({'owner/repo', 'owner/second'}))
        grant.pop('projects')
        grant['project'] = 'owner/repo'
        self.assertEqual(grant_projects(grant), frozenset({'owner/repo'}))


class OwnerServerTests(unittest.TestCase):
    rpc = fixtures.WorkerProjectServerTests.rpc
    tool = fixtures.WorkerProjectServerTests.tool
    subscribe = fixtures.WorkerProjectServerTests.subscribe
    queue = fixtures.WorkerProjectServerTests.queue

    def setUp(self):
        fixtures.WorkerProjectServerTests.setUp(self)
        self.grant.pop('projects')
        self.grant['owner'] = 'owner'
        self.principal = replace(self.principal, projects=frozenset(), owner='owner')

    def test_future_project_discovery_claim_report_and_cleanup_without_grant_edit(self):
        project = 'owner/future'
        self.runtime.policies[project] = Policy({'agent': 'dot'}, {'dot': {'configured'}})
        for tool in self.rpc('tools/list', {})['result']['tools']:
            self.assertIn('project', tool['inputSchema']['required'])
            self.assertIn(project, tool['inputSchema']['properties']['project']['enum'])
            self.assertNotIn('other/repo', tool['inputSchema']['properties']['project']['enum'])
        subscription = self.subscribe(project)['result']['id']
        task = self.queue(project)
        claimed = self.tool('claim_task', project=project, task_id=task['id'], attempt_id='stable')
        self.assertEqual(claimed['result']['structuredContent']['worker'], 'worker')
        result = self.tool('report_task_result', project=project, task_id=task['id'],
                          attempt_id='stable', revision=1, status='completed', output={})
        self.assertEqual(result['result']['structuredContent']['status'], 'completed')
        self.assertEqual(self.tool('get_task', project=project, task_id=task['id'])['result']['structuredContent']['output'], {})
        deleted = self.tool('delete_event_subscription', project=project, subscription_id=subscription)
        self.assertEqual(deleted['result']['structuredContent']['status'], 'gone')

    def test_cross_owner_missing_policy_wrong_task_and_worker_submit_denied(self):
        for project in ['other/repo', 'owner/*', 'owner/not-configured']:
            self.assertIn('error', self.subscribe(project))
        self.subscribe('owner/repo')
        task = self.queue('owner/repo')
        for project in [None, 'other/repo', 'owner/second']:
            args = {'task_id': task['id']}
            if project is not None:
                args['project'] = project
            self.assertIn('error', self.tool('get_task', **args))
        with self.assertRaises(PermissionError):
            self.app.dispatch('POST', '/v1/tasks?project=owner/repo',
                              {'Authorization': 'test', 'Idempotency-Key': 'denied'}, self.request)
        self.assertIn('error', self.tool('submit_task', project='owner/repo', **self.request))

    def test_grant_revocation_stops_owner_event_delivery(self):
        self.subscribe('owner/repo')
        self.grant['owner'] = 'other'
        self.events.flush()
        self.assertFalse(self.events.has_active('owner/repo'))

    def test_grok_owner_worker_reports_only_its_assigned_provider(self):
        self.principal = replace(self.principal, identity='grok-worker', provider='grok',
                                 scopes=frozenset({'tasks:read', 'tasks:work'}))
        self.assertIn('error', self.subscribe('owner/repo'))
        dot_task = self.queue('owner/repo')
        self.assertIn('error', self.tool('claim_task', project='owner/repo', task_id=dot_task['id'], attempt_id='wrong'))
        task = self.queue('owner/repo', profile='other')
        self.assertNotIn('error', self.tool('claim_task', project='owner/repo', task_id=task['id'], attempt_id='grok'))
        result = self.tool('report_task_result', project='owner/repo', task_id=task['id'],
                           attempt_id='grok', revision=1, status='completed', output={})
        self.assertEqual(result['result']['structuredContent']['status'], 'completed')
