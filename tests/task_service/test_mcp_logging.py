"""Exercise signed JWTs and HTTP responses without real credentials or services."""
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_service.auth import JWTAuthorizer
from ai_service.mcp_logging import MCPLog, TOOLS
from ai_service.runtime import Policy, TaskRuntime
from ai_service.server import Application, make_server
from ai_service.transport import HttpReply


class MCPLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid='test-key', use='sig', alg='RS256')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.private = ['synthetic-secret-token', 'private-subject-marker', 'private-identity-marker',
                        'private-project-marker', 'private-header-marker', 'private-claim-marker',
                        'private-body-marker', 'private@example.test', '192.0.2.77', '2001:db8::77']
        self.grant = {'issuer': 'https://issuer.example', 'subject': self.private[1],
                      'identity': self.private[2], 'project': self.private[3], 'provider': 'dot',
                      'scopes': ['tasks:read', 'tasks:work', 'events:subscribe'],
                      'claims': {'azp': 'example-client', 'connection': self.private[5]}}
        self.claims = {'iss': self.grant['issuer'], 'sub': self.grant['subject'],
                       'aud': 'https://service.example/mcp', 'iat': int(time.time()),
                       'exp': int(time.time()) + 60, 'scope': 'tasks:read tasks:work',
                       'azp': 'example-client', 'connection': self.private[5],
                       'email': self.private[7], 'ip': self.private[8], 'other': list(self.private)}
        transport = SimpleNamespace(request=lambda *args, **kwargs:
            HttpReply(200, json.dumps({'keys': [self.jwk]}).encode()))
        auth = JWTAuthorizer(self.claims['aud'], {self.grant['issuer']: {'jwks_url': 'https://issuer.example/jwks'}},
                             [self.grant], transport=transport)
        runtime = TaskRuntime(Path(temporary.name) / 'state.sqlite', {},
                              {self.grant['project']: Policy({'agent': 'dot'}, {'dot': {'configured'}})})
        self.app = Application(runtime, auth, SimpleNamespace(provider_name='dot'))

    def token(self, **changes):
        token = jwt.encode(dict(self.claims, **changes), self.key, algorithm='RS256', headers={'kid': 'test-key'})
        self.private.append(token)
        return 'Bearer ' + token

    def request(self, method='tools/list', *, authorization=None, params=None, message=None,
                path='/mcp', level='INFO', failure=None):
        body = message if message is not None else {'jsonrpc': '2.0', 'id': self.private[6],
                'method': method, 'params': params if params is not None else {}}
        headers = {'Authorization': authorization if authorization is not None else self.token(),
                   'Cookie': self.private[0], 'X-Private': self.private[4],
                   'X-Forwarded-For': self.private[8]}
        output, errors = io.StringIO(), io.StringIO()
        server = make_server(self.app, 0, mcp_log_level=level)
        server.daemon_threads = False
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        worker.start()
        request = urllib.request.Request('http://127.0.0.1:' + str(server.server_port) + path,
                                         data=json.dumps(body).encode(), headers=headers)
        failing = patch.object(server.RequestHandlerClass, 'end_headers', side_effect=BrokenPipeError(self.private[0])) if failure else contextlib.nullcontext()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), failing:
            try:
                try:
                    reply = urllib.request.urlopen(request, timeout=2)
                except urllib.error.HTTPError as error:
                    reply = error
                with reply:
                    status, payload = reply.code, json.load(reply) if reply.code != 202 else None
            except OSError:
                if not failure:
                    raise
                status, payload = None, None
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)
        raw = output.getvalue() + errors.getvalue()
        for private in self.private + ['Authorization', 'Cookie', 'Traceback', '127.0.0.1']:
            self.assertNotIn(private, raw)
        self.assertEqual(errors.getvalue(), '')
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        if path == '/mcp' and level == 'INFO':
            self.assertEqual(len(records), 1)
        return status, payload, records

    def test_three_and_five_tools_are_actual_response_names_and_scope_intersection(self):
        for extra in ['', ' events:subscribe offline_access tasks:submit']:
            with self.subTest(extra=extra):
                status, payload, records = self.request(authorization=self.token(scope=self.claims['scope'] + extra))
                record = records[0]
                self.assertEqual(status, 200)
                self.assertEqual(record['returned_tool_names'], [tool['name'] for tool in payload['result']['tools']])
                self.assertEqual(len(record['returned_tool_names']), 5 if extra else 3)
                self.assertEqual(record['grant_scopes'], sorted(self.grant['scopes']))
                self.assertEqual(record['token_scopes'], sorted((self.claims['scope'] + extra).split()))
                self.assertEqual(record['effective_scopes'], sorted(set(self.grant['scopes']) & set((self.claims['scope'] + extra).split())))
                self.assertEqual(record['azp'], 'example-client')
                self.assertTrue(record['events_provider_matched'])
                self.assertTrue(record['response_written'])
                self.assertEqual((record['result'], record['auth_decision'], record['reason_code']), ('ok', 'allowed', 'granted'))

    def test_events_provider_mismatch_and_absence_explain_three_tools(self):
        for events in [None, SimpleNamespace(provider_name=self.private[2])]:
            self.app.events = events
            _, _, records = self.request(authorization=self.token(scope='tasks:work events:subscribe'))
            self.assertFalse(records[0]['events_provider_matched'])
            self.assertIn('events:subscribe', records[0]['effective_scopes'])
            self.assertEqual(len(records[0]['returned_tool_names']), 3)

    def test_initialize_and_notifications_record_their_method(self):
        _, _, records = self.request('initialize', params={'protocolVersion': '2025-11-25', 'capabilities': {},
                                              'clientInfo': {'name': self.private[7], 'version': self.private[0]}})
        self.assertEqual(records[0]['method'], 'initialize')
        self.assertEqual(records[0]['result'], 'ok')
        self.assertIsNone(records[0]['returned_tool_names'])
        status, _, records = self.request(message={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertEqual(status, 202)
        self.assertEqual(records[0]['method'], 'notifications/initialized')

    def test_preverification_denials_are_anonymous_and_body_is_not_used(self):
        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode(self.claims, wrong_key, algorithm='RS256', headers={'kid': 'test-key'})
        self.private.append(forged)
        cases = [('', 'bearer_required'), ('Bearer ' + self.private[0], 'invalid_token'),
                 ('Bearer ' + 'x' * 16385, 'token_too_large'),
                 ('Bearer ' + forged, 'jwt_validation_failed'),
                 (self.token(exp=0), 'jwt_validation_failed'),
                 (self.token(aud=self.private[0]), 'jwt_validation_failed'),
                 (self.token(iss='https://untrusted.example'), 'issuer_not_trusted')]
        for token, reason in cases:
            with self.subTest(reason=reason):
                status, _, records = self.request(authorization=token)
                record = records[0]
                self.assertEqual(status, 401)
                self.assertEqual((record['method'], record['result'], record['auth_decision'], record['reason_code']),
                                 ('auth', 'denied', 'denied', reason))
                for name in ['azp', 'grant_scopes', 'token_scopes', 'effective_scopes', 'events_provider_matched', 'returned_tool_names']:
                    self.assertIsNone(record[name])

    def test_verified_grant_denial_retains_only_trusted_safe_names(self):
        for changes in [{'sub': self.private[7]}, {'connection': self.private[0]}]:
            status, _, records = self.request(authorization=self.token(**changes))
            record = records[0]
            self.assertEqual(status, 401)
            self.assertEqual(record['reason_code'], 'grant_not_admitted')
            self.assertEqual(record['azp'], 'example-client')
            self.assertEqual(record['token_scopes'], ['tasks:read', 'tasks:work'])
            self.assertIsNone(record['grant_scopes'])
            self.assertIsNone(record['effective_scopes'])

    def test_invalid_and_empty_token_scope_names(self):
        status, _, records = self.request(authorization=self.token(scope=self.private))
        self.assertEqual(status, 401)
        self.assertEqual(records[0]['reason_code'], 'invalid_scopes')
        self.assertEqual(records[0]['grant_scopes'], sorted(self.grant['scopes']))
        self.assertIsNone(records[0]['token_scopes'])
        self.assertIsNone(records[0]['effective_scopes'])
        _, _, records = self.request(authorization=self.token(scope=''))
        self.assertEqual(records[0]['token_scopes'], [])
        self.assertEqual(records[0]['effective_scopes'], [])
        self.assertEqual(records[0]['result'], 'denied')

    def test_key_service_error_details_are_suppressed(self):
        with patch.object(self.app.auth.transport, 'request', side_effect=RuntimeError(self.private[7])):
            status, _, records = self.request()
        self.assertEqual(status, 401)
        self.assertEqual(records[0]['reason_code'], 'signing_key_unavailable')
        self.assertIsNone(records[0]['azp'])

    def test_existing_workload_scope_exemption_is_preserved(self):
        issuer = 'https://token.actions.githubusercontent.com'
        self.grant.update(issuer=issuer)
        self.grant['claims'].update(repository=self.grant['project'], workflow_ref='configured-workflow', ref='configured-ref')
        self.claims.update(iss=issuer, **self.grant['claims'])
        self.claims.pop('scope')
        self.app.auth.issuers = {issuer: {'jwks_url': 'https://issuer.example/jwks', 'require_scope': False}}
        _, _, records = self.request()
        self.assertIsNone(records[0]['token_scopes'])
        self.assertEqual(records[0]['effective_scopes'], sorted(self.grant['scopes']))
        self.assertEqual(len(records[0]['returned_tool_names']), 5)

    def test_project_admission_denial_preserves_http_403(self):
        self.app.runtime.policies.clear()
        status, _, records = self.request()
        self.assertEqual(status, 403)
        self.assertEqual((records[0]['reason_code'], records[0]['auth_decision']), ('project_not_admitted', 'denied'))
        self.assertEqual(records[0]['effective_scopes'], ['tasks:read', 'tasks:work'])

    def test_http_200_rpc_denial_is_never_a_successful_tool_list(self):
        for params, token, reason, code in [({}, self.token(scope='tasks:read'), 'scope_or_provider_denied', -32003),
                ({'_meta': self.private[0]}, self.token(), 'invalid_arguments', -32602)]:
            status, payload, records = self.request(params=params, authorization=token)
            record = records[0]
            self.assertEqual(status, 200)
            self.assertEqual(payload['error']['code'], code)
            self.assertEqual((record['result'], record['auth_decision'], record['reason_code'], record['error_code']),
                             ('denied', 'allowed', reason, code))
            self.assertIsNone(record['returned_tool_names'])

    def test_unknown_scopes_methods_tools_and_azp_cannot_exfiltrate(self):
        self.grant['scopes'].append(self.private[0])
        _, _, records = self.request(authorization=self.token(scope='tasks:work ' + self.private[0]))
        for name in ['grant_scopes', 'token_scopes', 'effective_scopes']:
            self.assertIn('[REDACTED_UNKNOWN_SCOPE]', records[0][name])
        _, _, records = self.request(self.private[7])
        self.assertEqual((records[0]['method'], records[0]['reason_code']), ('unknown', 'method_not_found'))
        with patch.object(self.app, 'tools', return_value=[{'name': self.private[0], 'description': self.private[7]}]):
            _, _, records = self.request()
        self.assertEqual(records[0]['returned_tool_names'], ['[REDACTED_UNKNOWN_TOOL]'])
        for azp in self.private[:10]:
            _, _, records = self.request(authorization=self.token(azp=azp))
            self.assertIsNone(records[0]['azp'])
        # Even a configured azp cannot carry an email, IP, JWT or newline.
        for azp in [self.private[7], self.private[8], self.private[9], self.private[-1], 'bad\nclient']:
            self.grant['claims']['azp'] = azp
            _, _, records = self.request(authorization=self.token(azp=azp))
            self.assertIsNone(records[0]['azp'])

    def test_internal_and_response_write_failures_have_no_traceback_or_peer_address(self):
        with patch.object(self.app, 'dispatch', side_effect=RuntimeError(self.private[0])):
            status, _, records = self.request()
        self.assertEqual(status, 500)
        self.assertEqual(records[0]['reason_code'], 'internal_error')
        _, _, records = self.request(failure=True)
        self.assertEqual((records[0]['result'], records[0]['reason_code'], records[0]['response_written']),
                         ('denied', 'response_write_failed', False))
        self.assertIsNone(records[0]['returned_tool_names'])

    def test_log_levels_and_non_mcp_routes(self):
        for level in ['WARNING', 'ERROR', 'CRITICAL', 'OFF']:
            self.assertEqual(self.request(level=level)[2], [])
        self.assertEqual(self.request(level='ERROR', authorization='')[2], [])
        self.assertEqual(self.request(level='WARNING', authorization='')[2][0]['result'], 'denied')
        self.assertEqual(self.request(path='/unknown')[2], [])
        for value in [None, True, 20, 'info', 'private-invalid-level']:
            with self.assertRaises(ValueError):
                make_server(self.app, 0, mcp_log_level=value)

    def test_logging_failure_does_not_change_the_response(self):
        with patch('ai_service.mcp_logging.print', side_effect=OSError(self.private[0])):
            principal = self.app.auth.authenticate(self.token())
            MCPLog().emit(method='tools/list', status=200, decision='allowed', reason='granted',
                          diagnostics=principal.diagnostics, provider_matched=True,
                          payload={'result': {'tools': [{'name': name} for name in TOOLS]}})
            self.assertEqual(self.app.dispatch('POST', '/mcp', {},
                             {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, principal=principal)[0], 200)
