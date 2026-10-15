import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ai_service.auth import AuthenticationError, Principal
from ai_service.runtime import Policy, TaskRuntime
from ai_service.server import Application, make_server
from test_task_runtime import CallbackProvider


class Authorizer:
    resource = 'https://service.example/mcp'
    principals = {'caller': Principal('caller', 'owner/repo', frozenset({'tasks:read', 'tasks:submit'})),
                  'bot': Principal('bot', 'owner/repo', frozenset({'tasks:read', 'tasks:work'}), 'grok'),
                  'reviewer': Principal('reviewer', 'owner/repo', frozenset({'tasks:read', 'tasks:work'}), 'review'),
                  'other': Principal('other', 'other/repo', frozenset({'tasks:read', 'tasks:submit'}))}
    def authenticate(self, header):
        if header not in self.principals:
            raise AuthenticationError('missing token')
        return self.principals[header]
    def resource_metadata(self):
        return {'resource': self.resource}


class TaskServerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = TaskRuntime(Path(tmp.name) / 'state.sqlite',
            {'grok': CallbackProvider(), 'review': CallbackProvider()},
            {'owner/repo': Policy({'agent': 'grok'}, {'grok': {'configured'}}),
             'other/repo': Policy({'agent': 'grok'}, {'grok': {'configured'}})})
        self.app = Application(self.runtime, Authorizer())
        self.request = {'mode': 'agent', 'model': 'configured', 'objective': 'private objective',
                        'output_schema': {'type': 'object'}}

    def rpc(self, identity, name, arguments):
        return self.app.dispatch('POST', '/mcp', {'Authorization': identity},
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})[1]

    def test_end_to_end_submit_claim_report_and_read(self):
        status, task = self.app.dispatch('POST', '/v1/tasks', {'Authorization': 'caller', 'Idempotency-Key': 'one'}, self.request)
        self.assertEqual(status, 202)
        self.runtime.tick()
        claimed = self.rpc('bot', 'claim_task', {'task_id': task['id'], 'attempt_id': 'one'})
        self.assertEqual(claimed['result']['structuredContent']['revision'], 1)
        reported = self.rpc('bot', 'report_task_result', {'task_id': task['id'], 'attempt_id': 'one',
                            'revision': 1, 'status': 'completed', 'output': {'answer': 'done'}})
        self.assertEqual(reported['result']['structuredContent']['status'], 'completed')
        self.assertEqual(self.app.dispatch('GET', '/v1/tasks/' + task['id'], {'Authorization': 'caller'})[1]['output'], {'answer': 'done'})

    def test_caller_cannot_claim_and_unassigned_worker_cannot_read(self):
        task = self.runtime.submit('owner/repo', 'one', self.request)
        self.runtime.tick()
        for identity in ['caller', 'reviewer']:
            result = self.rpc(identity, 'get_task', {'task_id': task['id']})
            self.assertEqual(result['error']['code'], -32003)
        result = self.rpc('bot', 'get_task', {'task_id': task['id'], 'private_extra': 'secret'})
        self.assertNotIn('secret', str(result))
        self.assertEqual(result['error']['code'], -32602)

    def test_worker_cannot_submit_or_override_identity(self):
        with self.assertRaises(PermissionError):
            self.app.dispatch('POST', '/v1/tasks', {'Authorization': 'bot', 'Idempotency-Key': 'one'}, self.request)
        task = self.runtime.submit('owner/repo', 'one', self.request)
        self.runtime.tick()
        result = self.rpc('bot', 'claim_task', {'task_id': task['id'], 'attempt_id': 'one', 'worker': 'reviewer'})
        self.assertEqual(result['error']['code'], -32602)

    def test_standard_mcp_initialize_negotiates_before_tool_discovery(self):
        for requested, expected in [('2025-03-26', '2025-03-26'),
                                    ('2025-06-18', '2025-06-18'),
                                    ('2025-11-25', '2025-11-25'),
                                    ('unknown', '2025-11-25')]:
            with self.subTest(version=requested):
                status, reply = self.app.dispatch('POST', '/mcp', {'Authorization': 'bot'},
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                        'protocolVersion': requested, 'capabilities': {},
                        'clientInfo': {'name': 'test-client', 'version': '1'}}})
                self.assertEqual(status, 200)
                self.assertEqual(reply['result']['protocolVersion'], expected)
                self.assertEqual(reply['result']['capabilities'], {'tools': {}})
                self.assertTrue(reply['result']['serverInfo']['name'])
                self.assertTrue(reply['result']['serverInfo']['version'])
        status, reply = self.app.dispatch('POST', '/mcp', {'Authorization': 'bot'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        self.assertEqual(status, 200)
        self.assertEqual(len(reply['result']['tools']), 3)

    def test_initialize_cannot_grant_worker_permissions(self):
        reply = self.app.dispatch('POST', '/mcp', {'Authorization': 'caller'},
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'test-client', 'version': '1'},
                '_meta': {'scopes': ['tasks:work']}}})[1]
        self.assertEqual(reply['error']['code'], -32003)

    def test_http_initialized_notification_is_authenticated_and_has_no_body(self):
        server = make_server(self.app, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_port) + '/mcp'
        initialize = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'http-test', 'version': '1'}}}
        request = urllib.request.Request(url, data=json.dumps(initialize).encode(),
            headers={'Authorization': 'bot', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request) as reply:
            self.assertEqual(json.load(reply)['result']['protocolVersion'], '2025-11-25')
        body = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode()
        for auth, expected in [('bot', 202), ('private-invalid-token', 401)]:
            request = urllib.request.Request(url, data=body, headers={'Authorization': auth,
                'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
            if expected == 202:
                with urllib.request.urlopen(request) as reply:
                    self.assertEqual(reply.status, 202)
                    self.assertEqual(reply.read(), b'')
            else:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 401)

    def test_idless_tool_calls_and_malformed_notifications_are_rejected(self):
        for message in [
            {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': []},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {'_meta': []}},
        ]:
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.app.dispatch('POST', '/mcp', {'Authorization': 'bot'}, message)

    def test_mcp_ping_and_no_stream_get(self):
        reply = self.app.dispatch('POST', '/mcp', {'Authorization': 'bot'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})[1]
        self.assertEqual(reply, {'jsonrpc': '2.0', 'id': 3, 'result': {}})
        self.assertEqual(self.app.dispatch('GET', '/mcp', {'Authorization': 'bot'})[0], 405)

    def test_native_protocol_metadata_is_not_business_arguments_or_authority(self):
        task = self.runtime.submit('owner/repo', 'one', self.request)
        self.runtime.tick()
        params = {'name': 'claim_task', 'arguments': {'task_id': task['id'], 'attempt_id': 'one'},
                  '_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                            'identity': 'caller', 'project': 'other/repo', 'scopes': ['tasks:submit']}}
        reply = self.app.dispatch('POST', '/mcp', {'Authorization': 'bot'},
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params})[1]
        self.assertEqual(reply['result']['structuredContent']['worker'], 'bot')
        self.assertEqual(reply['result']['structuredContent']['owner'], 'owner/repo')
        self.assertIn('_meta', params)
        # Metadata cannot grant caller identity the worker role.
        denied = self.app.dispatch('POST', '/mcp', {'Authorization': 'caller'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': params})[1]
        self.assertEqual(denied['error']['code'], -32003)

    def test_malformed_protocol_metadata_is_rejected(self):
        for metadata in [None, [], 'not-an-object', 1]:
            with self.subTest(metadata_type=type(metadata).__name__):
                reply = self.app.dispatch('POST', '/mcp', {'Authorization': 'bot'},
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {'_meta': metadata}})[1]
                self.assertEqual(reply['error']['code'], -32602)

    def test_unvalidated_private_adapter_output_is_not_exposed_in_handle(self):
        record = {'status': 'running', 'output': None,
                  'handle': {'task_id': 'task', 'output': {'private_unvalidated': True}, 'model': 'configured'}}
        public = self.app.public_record(record)
        self.assertNotIn('output', public['handle'])
        self.assertIsNone(public['output'])
        self.assertIn('output', record['handle'])

    def test_legacy_endpoint_is_not_available(self):
        from ai_service.runtime import NotFound
        with self.assertRaises(NotFound):
            self.app.dispatch('POST', '/v1/codex', {'Authorization': 'caller'}, {})

    def test_http_metadata_challenge_and_errors_do_not_include_token(self):
        server = make_server(self.app, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_port)
        with urllib.request.urlopen(base + '/.well-known/oauth-protected-resource') as reply:
            self.assertEqual(json.load(reply)['resource'], self.app.auth.resource)
        request = urllib.request.Request(base + '/v1/tasks/unknown', headers={'Authorization': 'private-invalid-token'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 401)
        self.assertIn('https://service.example/.well-known/oauth-protected-resource', error.exception.headers['WWW-Authenticate'])
        self.assertNotIn(b'private-invalid-token', error.exception.read())

    def test_unauthenticated_body_is_rejected_without_waiting_for_upload(self):
        server = make_server(self.app, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(b"POST /mcp HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1000000\r\n\r\n")
                # No body is sent. An implementation reading it first would time out.
                response = client.recv(4096)
                self.assertIn(b"401 Unauthorized", response)
        finally:
            server.shutdown()
            server.server_close()

    def test_request_body_limit_is_explicit_and_rejects_before_dispatch(self):
        for limit in [True, 0, 16_000_001, 2.5]:
            with self.assertRaises(ValueError):
                make_server(self.app, 0, max_request_bytes=limit)
        server = make_server(self.app, 0, max_request_bytes=64)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = 'http://127.0.0.1:' + str(server.server_port) + '/v1/tasks'
            request = urllib.request.Request(url, data=b'X' * 65, method='POST')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 400)
            self.assertNotIn('XXX', error.exception.read().decode())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
