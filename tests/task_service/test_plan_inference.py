import json
import tempfile
import unittest
from pathlib import Path

from ai_service.inference import ChatGPTPlanProvider
from ai_service.runtime import Policy, TaskRuntime
from ai_service.transport import HttpReply


class PlanInferenceTests(unittest.TestCase):
    def setUp(self):
        self.request = {'mode': 'api', 'model': 'synthetic-model', 'objective': 'Summarize',
                        'output_schema': {'type': 'object', 'properties': {'summary': {'type': 'string'}},
                                          'required': ['summary'], 'additionalProperties': False}}
        self.response = {'id': 'synthetic-response', 'status': 'completed', 'model': 'synthetic-model',
                         'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"summary":"done"}'}]}],
                         'usage': {'input_tokens': 5, 'output_tokens': 4, 'total_tokens': 9}}
        self.calls = []

    def runtime(self, events, status=200):
        class Transport:
            def request(inner, url, **kwargs):
                self.calls.append((url, kwargs))
                body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                return HttpReply(status, body)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        provider = ChatGPTPlanProvider(lambda: 'synthetic-oauth-token', plan_usage_authorized=True, transport=Transport())
        return TaskRuntime(Path(tmp.name) / 'state.sqlite', {'plan': provider},
                           {'owner/repo': Policy({'api': 'plan'}, {'plan': {'synthetic-model'}})})

    def test_requires_registered_authorization(self):
        with self.assertRaises(ValueError):
            ChatGPTPlanProvider(lambda: 'token')

    def test_stream_terminal_model_schema_usage_and_no_store(self):
        runtime = self.runtime([{'type': 'response.completed', 'response': self.response}])
        task = runtime.submit('owner/repo', 'one', self.request)
        runtime.tick()
        runtime.tick()
        record = runtime.get('owner/repo', task['id'])
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['output'], {'summary': 'done'})
        payload = json.loads(self.calls[0][1]['body'])
        self.assertFalse(payload['store'])
        self.assertTrue(payload['stream'])
        self.assertNotIn('tools', payload)
        self.assertNotIn('synthetic-oauth-token', str(record))
        self.assertEqual(record['handle']['usage']['total_tokens'], 9)

    def test_deltas_and_done_marker_do_not_prove_completion(self):
        runtime = self.runtime([{'type': 'response.output_text.delta', 'delta': '{"summary":"done"}'}])
        task = runtime.submit('owner/repo', 'one', self.request)
        runtime.tick()
        runtime.tick()
        self.assertEqual(runtime.get('owner/repo', task['id'])['status'], 'outcome_unknown')
        self.assertEqual(len(self.calls), 1)

    def test_usage_failure_and_model_mismatch_fail_without_fallback(self):
        for terminal in [{'type': 'response.failed', 'response': {'error': {'code': 'subscription_sharing_usage_limit_exceeded'}}},
                         {'type': 'response.completed', 'response': {**self.response, 'model': 'other'}}]:
            runtime = self.runtime([terminal])
            task = runtime.submit('owner/repo', 'one', self.request)
            runtime.tick()
            runtime.tick()
            self.assertEqual(runtime.get('owner/repo', task['id'])['status'], 'failed')
        self.assertEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main()
