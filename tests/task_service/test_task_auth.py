import json
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_service.auth import AuthenticationError, JWTAuthorizer
from ai_service.transport import HttpReply


class AuthTests(unittest.TestCase):
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

    def token(self, claims):
        return 'Bearer ' + jwt.encode(claims, self.key, algorithm='RS256', headers={'kid': 'test-key'})

    def test_login_discovery_excludes_workload_only_github_issuer(self):
        authorizer = JWTAuthorizer('https://service.example/mcp', {
            'https://issuer.example': {'jwks_url': 'https://issuer.example/jwks'},
            'https://token.actions.githubusercontent.com': {
                'jwks_url': 'https://token.actions.githubusercontent.com/.well-known/jwks',
                'require_scope': False}}, [])
        metadata = authorizer.resource_metadata()
        self.assertEqual(metadata['authorization_servers'], ['https://issuer.example'])
        self.assertIn('https://token.actions.githubusercontent.com', authorizer.issuers)

    def test_signed_grant_is_project_scoped_and_token_scopes_are_intersected(self):
        principal = self.authorizer.authenticate(self.token(self.claims))
        self.assertEqual(principal.project, 'owner/repo')
        self.assertEqual(principal.provider, 'dot')
        self.assertEqual(principal.scopes, frozenset({'tasks:read', 'tasks:work'}))

    def test_offline_access_does_not_grant_business_permissions(self):
        claims = dict(self.claims, scope='tasks:read tasks:work offline_access tasks:submit')
        principal = self.authorizer.authenticate(self.token(claims))
        self.assertEqual(principal.scopes, frozenset({'tasks:read', 'tasks:work'}))
        self.assertEqual(principal.project, 'owner/repo')

    def test_missing_exp_wrong_audience_issuer_scope_type_and_workflow_are_rejected(self):
        bad = [dict(self.claims, exp=0), dict(self.claims, aud='other'),
               dict(self.claims, iss='https://untrusted.example'), dict(self.claims, connection='another'),
               dict(self.claims, sub='another'), dict(self.claims, scope=['tasks:work']),
               dict(self.claims, iat=int(time.time()) + 3600)]
        missing = dict(self.claims)
        del missing['exp']
        bad.append(missing)
        for claims in bad:
            with self.subTest(claims=list(claims)), self.assertRaises(AuthenticationError) as error:
                self.authorizer.authenticate(self.token(claims))
            self.assertEqual(str(error.exception), 'bearer token rejected')

    def test_unsigned_token_and_wrong_signature_are_rejected(self):
        unsigned = jwt.encode(self.claims, '', algorithm='none')
        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode(self.claims, wrong_key, algorithm='RS256', headers={'kid': 'test-key'})
        for token in [unsigned, forged]:
            with self.assertRaises(AuthenticationError):
                self.authorizer.authenticate('Bearer ' + token)

    def test_event_revocation_rechecks_current_grants(self):
        self.assertTrue(self.authorizer.can_access_events('worker-1', 'owner/repo', 'dot'))
        self.assertFalse(self.authorizer.can_access_events('worker-1', 'other/repo', 'dot'))
        self.authorizer.grants.clear()
        self.assertFalse(self.authorizer.can_access_events('worker-1', 'owner/repo', 'dot'))


if __name__ == '__main__':
    unittest.main()
