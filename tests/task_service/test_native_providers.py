import json
import socket
import tempfile
import unittest
from pathlib import Path

from ai_service.providers import GrokWebhookProvider
from ai_service.runtime import Policy, TaskRuntime
from ai_service.transport import HttpReply, public_destination


class FakeTransport:
    def __init__(self, status=200, fail=False):
        self.status, self.fail, self.calls = status, fail, []

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise TimeoutError('secret response and credential')
        return HttpReply(self.status, b'private upstream reply')


class NativeProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.request = {'mode': 'agent', 'model': 'configured-bot', 'objective': 'private objective',
                        'materials': ['private attachment'], 'output_schema': {'type': 'object'}}

    def runtime(self, transport):
        provider = GrokWebhookProvider('https://cursor.com/example', 'test-secret',
                                       'https://service.example/mcp', transport=transport)
        return TaskRuntime(Path(self.tmp.name) / 'state.sqlite', {'grok': provider},
                           {'owner/repo': Policy({'agent': 'grok'}, {'grok': {'configured-bot'}})})

    def test_acceptance_is_running_without_fabricated_completion_or_model(self):
        transport = FakeTransport()
        runtime = self.runtime(transport)
        task = runtime.submit('owner/repo', 'one', self.request)
        runtime.tick()
        actual = runtime.get('owner/repo', task['id'])
        self.assertEqual(actual['status'], 'running')
        self.assertIsNone(actual['output'])
        self.assertEqual(actual['handle']['model_verification'], 'unavailable')
        notification = json.loads(transport.calls[0][1]['body'])
        self.assertEqual(notification['task_id'], task['id'])
        self.assertNotIn('private', str(notification))
        self.assertNotIn('test-secret', str(actual))

    def test_explicit_rejection_and_lost_reply_have_different_states(self):
        for status, fail, expected in [(401, False, 'failed'), (200, True, 'outcome_unknown')]:
            with self.subTest(expected=expected):
                transport = FakeTransport(status, fail)
                runtime = self.runtime(transport)
                task = runtime.submit('owner/repo', expected, self.request)
                runtime.tick()
                runtime.tick()
                record = runtime.get('owner/repo', task['id'])
                self.assertEqual(record['status'], expected)
                self.assertNotIn('secret', str(record))
                self.assertEqual(len(transport.calls), 1)

    def test_native_cancellation_requires_worker_confirmation(self):
        runtime = self.runtime(FakeTransport())
        task = runtime.submit('owner/repo', 'one', self.request)
        runtime.tick()
        runtime.claim('owner/repo', task['id'], 'grok', 'bot', 'attempt')
        runtime.cancel('owner/repo', task['id'])
        runtime.tick()
        self.assertEqual(runtime.get('owner/repo', task['id'])['status'], 'cancel_requested')
        self.assertEqual(runtime.report('owner/repo', task['id'], 'grok', 'bot', 'attempt',
                                        1, 'cancelled')['status'], 'cancelled')

    def test_callback_destination_rejects_private_mixed_and_credential_urls(self):
        def resolver(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        for url in ['https://example.com/path', 'http://example.com',
                    'https://user:pass@example.com', 'https://example.com/#fragment']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_destination(url, resolver)

    def test_public_destination_retains_original_tls_hostname(self):
        def resolver(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        parsed, addresses = public_destination('https://example.com/callback', resolver)
        self.assertEqual(parsed.hostname, 'example.com')
        self.assertEqual(addresses, [(socket.AF_INET, '93.184.216.34')])


if __name__ == '__main__':
    unittest.main()
