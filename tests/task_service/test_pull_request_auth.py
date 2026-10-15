import copy
import json
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_service.auth import AuthenticationError, JWTAuthorizer
from ai_service.transport import HttpReply


class PullRequestAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid='test-key', use='sig', alg='RS256')

    def setUp(self):
        self.project = 'QuantStrategyLab/CryptoStrategies'
        self.issuer = 'https://token.actions.githubusercontent.com'
        self.resource = 'https://service.example/mcp'
        self.ref = 'refs/pull/42/merge'
        self.sha = 'a' * 40
        self.grant = {
            'issuer': self.issuer, 'subject': f'repo:{self.project}:pull_request',
            'identity': 'crypto-evidence', 'project': self.project,
            'scopes': ['tasks:read', 'tasks:submit'],
            'pull_request_workflow': '.github/workflows/evidence-gate.yml',
            'claims': {'repository': self.project, 'repository_id': '123',
                       'repository_visibility': 'public', 'event_name': 'pull_request', 'base_ref': 'main'},
        }
        self.claims = {
            'iss': self.issuer, 'sub': self.grant['subject'], 'aud': self.resource,
            'iat': int(time.time()), 'exp': int(time.time()) + 60,
            **self.grant['claims'], 'ref': self.ref, 'sha': self.sha,
            'workflow_ref': f'{self.project}/.github/workflows/evidence-gate.yml@{self.ref}',
        }
        self.pull = {'number': 42, 'state': 'open', 'merge_commit_sha': self.sha,
                     'head': {'repo': {'id': 123, 'full_name': self.project}},
                     'base': {'ref': 'main', 'repo': {'id': 123, 'full_name': self.project}}}
        self.reply = None
        self.requests = []

        class Transport:
            def request(inner, url, **kwargs):
                self.requests.append((url, kwargs))
                if url.endswith('/jwks'):
                    return HttpReply(200, json.dumps({'keys': [self.jwk]}).encode())
                if isinstance(self.reply, Exception):
                    raise self.reply
                return self.reply or HttpReply(200, json.dumps(self.pull).encode())

        self.transport = Transport()

    def authorizer(self, grant=None):
        return JWTAuthorizer(self.resource, {
            self.issuer: {'jwks_url': self.issuer + '/jwks', 'require_scope': False}},
            [grant or self.grant], transport=self.transport)

    def token(self, claims=None):
        return 'Bearer ' + jwt.encode(claims or self.claims, self.key, algorithm='RS256', headers={'kid': 'test-key'})

    def test_signed_same_repository_current_merge_is_task_only(self):
        principal = self.authorizer().authenticate(self.token())
        self.assertEqual(principal.project, self.project)
        self.assertEqual(principal.scopes, frozenset({'tasks:read', 'tasks:submit'}))
        self.assertIsNone(principal.provider)
        self.assertEqual(self.requests[-1][0], f'https://api.github.com/repos/{self.project}/pulls/42')
        self.assertEqual(self.requests[-1][1]['method'], 'GET')
        self.assertNotIn('Authorization', self.requests[-1][1]['headers'])

    def test_wrong_signed_identity_event_base_ref_workflow_and_revision_are_rejected(self):
        bad = {'sub': 'another', 'repository': 'another/repo', 'repository_id': '999',
               'repository_visibility': 'private', 'event_name': 'pull_request_target',
               'base_ref': 'release', 'ref': 'refs/heads/main', 'sha': 'not-a-sha',
               'workflow_ref': f'{self.project}/.github/workflows/other.yml@{self.ref}'}
        for field, value in bad.items():
            with self.subTest(field=field), self.assertRaises(AuthenticationError):
                self.authorizer().authenticate(self.token(dict(self.claims, **{field: value})))

    def test_missing_required_claims_are_rejected(self):
        for field in ('ref', 'sha', 'workflow_ref', 'repository_id', 'event_name', 'base_ref'):
            claims = dict(self.claims)
            del claims[field]
            with self.subTest(field=field), self.assertRaises(AuthenticationError):
                self.authorizer().authenticate(self.token(claims))

    def test_fork_deleted_head_wrong_base_and_stale_merge_are_rejected(self):
        original = copy.deepcopy(self.pull)
        mutations = [
            lambda p: p['head'].update(repo={'id': 999, 'full_name': 'contributor/CryptoStrategies'}),
            lambda p: p['head'].update(repo=None),
            lambda p: p['base'].update(ref='release'),
            lambda p: p['base'].update(repo={'id': 999, 'full_name': self.project}),
            lambda p: p.update(merge_commit_sha='b' * 40),
            lambda p: p.update(state='closed'),
            lambda p: p.update(number=43),
        ]
        for mutate in mutations:
            self.pull = copy.deepcopy(original)
            mutate(self.pull)
            with self.subTest(response=list(self.pull)), self.assertRaises(AuthenticationError):
                self.authorizer().authenticate(self.token())

    def test_public_api_failure_rate_limit_redirect_and_malformed_response_fail_closed(self):
        for reply in (HttpReply(403, b'{}'), HttpReply(404, b'{}'), HttpReply(302, b'{}'),
                      HttpReply(200, b'invalid'), HttpReply(200, b'{}'), RuntimeError('offline')):
            self.reply = reply
            with self.subTest(reply=type(reply).__name__), self.assertRaises(AuthenticationError) as error:
                self.authorizer().authenticate(self.token())
            self.assertEqual(str(error.exception), 'bearer token rejected')

    def test_unsafe_pull_request_grant_configuration_is_rejected(self):
        mutations = [lambda g: g.update(scopes=['tasks:work']),
                     lambda g: g.update(subject=f'repo:{self.project}:ref:refs/heads/main'),
                     lambda g: g.update(pull_request_workflow='../other.yml'),
                     lambda g: g['claims'].update(event_name='pull_request_target'),
                     lambda g: g['claims'].pop('repository_id'),
                     lambda g: g['claims'].update(repository_visibility='private')]
        for mutate in mutations:
            grant = copy.deepcopy(self.grant)
            mutate(grant)
            with self.subTest(keys=list(grant)), self.assertRaises(ValueError):
                self.authorizer(grant)

    def test_existing_exact_main_grant_does_not_call_pull_request_api(self):
        grant = copy.deepcopy(self.grant)
        del grant['pull_request_workflow']
        grant['subject'] = f'repo:{self.project}:ref:refs/heads/main'
        grant['claims'] = {'repository': self.project, 'ref': 'refs/heads/main',
                           'workflow_ref': f'{self.project}/.github/workflows/drift-check.yml@refs/heads/main'}
        claims = dict(self.claims, sub=grant['subject'], **grant['claims'])
        authorizer = self.authorizer(grant)
        self.assertEqual(authorizer.authenticate(self.token(claims)).project, self.project)
        self.assertEqual(len(self.requests), 1)
        with self.assertRaises(AuthenticationError):
            authorizer.authenticate(self.token(dict(claims, ref=self.ref)))


if __name__ == '__main__':
    unittest.main()
