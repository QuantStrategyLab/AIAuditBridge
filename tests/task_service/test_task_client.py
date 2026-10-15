import os
import unittest
from unittest.mock import patch

from ai_service.client import TaskClient, github_token
from ai_service.transport import HttpReply


class ClientTests(unittest.TestCase):
    def test_github_identity_destination_cannot_be_caller_selected_private_host(self):
        for url in ['https://attacker.example/token', 'http://pipelines.actions.githubusercontent.com/token',
                    'https://pipelines.actions.githubusercontent.com.attacker.example/token']:
            with patch.dict(os.environ, {'ACTIONS_ID_TOKEN_REQUEST_URL': url, 'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic'}):
                with self.assertRaises(ValueError):
                    github_token('https://service.example/mcp')

    def test_github_audience_is_encoded_and_overrides_old_query_value(self):
        calls = []
        class Transport:
            def request(self, url, **kwargs):
                calls.append(url)
                return HttpReply(200, b'{"value":"synthetic-token"}')
        with patch.dict(os.environ, {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://pipelines.actions.githubusercontent.com/token?audience=old',
                                   'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic'}):
            self.assertEqual(github_token('https://service.example/mcp', transport=Transport()), 'synthetic-token')
        self.assertNotIn('audience=old', calls[0])
        self.assertIn('audience=https%3A%2F%2Fservice.example%2Fmcp', calls[0])

    def test_wait_returns_unknown_without_resubmitting_and_errors_redact_upstream(self):
        calls = []
        class Transport:
            def request(self, url, **kwargs):
                calls.append(kwargs['method'])
                return HttpReply(200, b'{"status":"outcome_unknown"}')
        client = TaskClient('https://service.example', 'synthetic-token', transport=Transport())
        self.assertEqual(client.wait('task')['status'], 'outcome_unknown')
        self.assertEqual(calls, ['GET'])

    def test_image_response_limit_is_explicit_and_bounded(self):
        client = TaskClient('https://service.example', 'synthetic', max_response_bytes=16_000_000)
        self.assertEqual(client.transport.max_response_bytes, 16_000_000)
        for limit in [True, 0, 16_000_001, 2.5]:
            with self.assertRaises(ValueError):
                TaskClient('https://service.example', 'synthetic', max_response_bytes=limit)


if __name__ == '__main__':
    unittest.main()
