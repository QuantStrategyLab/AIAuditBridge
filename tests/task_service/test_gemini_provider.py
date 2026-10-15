"""Synthetic transport only; no credentials or real Gemini calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_service.gemini import GeminiProvider
from ai_service.runtime import Conflict, Policy, SubmissionRejected, TaskRuntime
from ai_service.server import build_application
from ai_service.transport import HttpReply, TransportError


class GeminiProviderTests(unittest.TestCase):
    def setUp(self):
        self.request = {'mode': 'api', 'model': 'synthetic-model', 'objective': 'Summarize',
                        'materials': [{'text': 'synthetic material'}],
                        'output_schema': {'type': 'object', 'properties': {'summary': {'type': 'string'}},
                                          'required': ['summary'], 'additionalProperties': False}}
        self.response = {'modelVersion': 'synthetic-model', 'candidates': [{
            'finishReason': 'STOP', 'content': {'role': 'model', 'parts': [{'text': '{"summary":"done"}'}]}}],
            'usageMetadata': {'promptTokenCount': 5, 'candidatesTokenCount': 4, 'totalTokenCount': 9,
                              'untrusted': 'sensitive'}}
        self.calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def runtime(self, response=None, *, status=200, error=None):
        response = self.response if response is None else response
        class Transport:
            def request(inner, url, **kwargs):
                self.calls.append((url, kwargs))
                if error:
                    raise error
                return HttpReply(status, json.dumps(response).encode())
        provider = GeminiProvider(lambda: 'synthetic-key', model='synthetic-model',
                                  api_usage_authorized=True, transport=Transport())
        return TaskRuntime(Path(self.tmp.name) / ('state-' + str(len(list(Path(self.tmp.name).glob('*.sqlite')))) + '.sqlite'),
                           {'gemini': provider}, {'owner/repo': Policy({'api': 'gemini'},
                            {'gemini': {'synthetic-model'}}, max_active=1, max_timeout_seconds=60)})

    def finish(self, runtime, request=None):
        task = runtime.submit('owner/repo', 'one', request or self.request)
        for _ in range(3):
            runtime.tick()
        return runtime.get('owner/repo', task['id'])

    def test_authorization_model_and_key_are_required(self):
        with self.assertRaises(ValueError):
            GeminiProvider(lambda: 'synthetic', model='synthetic-model')
        for model in ('../../escape', '', 'model?key=bad', None):
            with self.assertRaises(ValueError):
                GeminiProvider(lambda: 'synthetic', model=model, api_usage_authorized=True)
        provider = GeminiProvider(lambda: 'bad\nkey', model='synthetic-model', api_usage_authorized=True)
        with self.assertRaises(SubmissionRejected):
            provider.submit(self.request, task_id='x', owner='owner/repo')

    def test_fixed_request_json_schema_and_redacted_usage(self):
        record = self.finish(self.runtime())
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['output'], {'summary': 'done'})
        url, options = self.calls[0]
        self.assertEqual(url, 'https://generativelanguage.googleapis.com/v1beta/models/synthetic-model:generateContent')
        self.assertNotIn('synthetic-key', url)
        self.assertEqual(options['headers']['x-goog-api-key'], 'synthetic-key')
        payload = json.loads(options['body'])
        self.assertEqual(payload['generationConfig']['responseJsonSchema'], self.request['output_schema'])
        self.assertEqual(payload['generationConfig']['responseMimeType'], 'application/json')
        self.assertEqual(payload['generationConfig']['maxOutputTokens'], 1024)
        self.assertEqual(options['timeout'], 60)
        self.assertNotIn('tools', payload)
        self.assertNotIn('safetySettings', payload)
        self.assertNotIn('synthetic-key', str(record))
        self.assertNotIn('sensitive', str(record))
        self.assertEqual(record['handle']['usage'], {'input_tokens': 5, 'output_tokens': 4, 'total_tokens': 9})

    def test_rejection_codes_are_terminal_and_never_retried(self):
        for status in (400, 401, 403, 404, 422, 429):
            with self.subTest(status=status):
                before = len(self.calls)
                record = self.finish(self.runtime({'error': 'private provider details'}, status=status))
                self.assertEqual(record['status'], 'failed')
                self.assertEqual(len(self.calls), before + 1)
                self.assertNotIn('private provider details', str(record))

    def test_lost_receipt_and_5xx_stay_unknown_without_fallback(self):
        for error, status in ((TransportError('private details'), 200), (None, 500)):
            runtime = self.runtime(status=status, error=error)
            before = len(self.calls)
            record = self.finish(runtime)
            self.assertEqual(record['status'], 'outcome_unknown')
            self.assertEqual(len(self.calls), before + 1)
            self.assertNotIn('private details', str(record))
            with self.assertRaises(Conflict):
                runtime.submit('owner/repo', 'two', self.request)

    def test_refusal_truncation_and_model_mismatch_fail(self):
        candidates = self.response['candidates']
        cases = [{'promptFeedback': {'blockReason': 'SAFETY'}},
                 {**self.response, 'modelVersion': 'another-model'},
                 {**self.response, 'candidates': [{**candidates[0], 'finishReason': 'MAX_TOKENS'}]},
                 {**self.response, 'candidates': candidates * 2},
                 {**self.response, 'candidates': [{**candidates[0], 'content': {'parts': [{'functionCall': {}}]}}]}]
        for reply in cases:
            self.assertEqual(self.finish(self.runtime(reply))['status'], 'failed')

    def test_output_schema_is_still_validated_by_runtime(self):
        reply = {**self.response, 'candidates': [{'finishReason': 'STOP',
                 'content': {'parts': [{'text': '{"summary":3}'}]}}]}
        record = self.finish(self.runtime(reply))
        self.assertEqual(record['status'], 'invalid_output')
        self.assertIsNone(record['output'])

    def test_ambiguous_or_nonstandard_json_is_not_accepted(self):
        for text in ('{"summary":"one","summary":"two"}', '{"summary":NaN}', 'not json'):
            reply = {**self.response, 'candidates': [{'finishReason': 'STOP',
                     'content': {'parts': [{'text': text}]}}]}
            self.assertEqual(self.finish(self.runtime(reply))['status'], 'failed')

    def test_configured_limits_and_direct_model_override_are_rejected(self):
        for kwargs in ({'max_output_tokens': 8193}, {'max_output_tokens': True},
                       {'timeout_seconds': 61}, {'timeout_seconds': 0}):
            with self.assertRaises(ValueError):
                GeminiProvider(lambda: 'synthetic', model='synthetic-model',
                               api_usage_authorized=True, **kwargs)
        provider = GeminiProvider(lambda: self.fail('must not read key'), model='synthetic-model',
                                  api_usage_authorized=True)
        with self.assertRaises(SubmissionRejected):
            provider.submit({**self.request, 'model': 'other'}, task_id='x', owner='owner/repo')

    def test_project_model_quota_and_client_fields_remain_enforced(self):
        runtime = self.runtime()
        for project, request in [('other/repo', self.request),
                                 ('owner/repo', {**self.request, 'model': 'other'})]:
            with self.assertRaises(PermissionError):
                runtime.submit(project, 'one', request)
        with self.assertRaises(ValueError):
            runtime.submit('owner/repo', 'one', {**self.request, 'api_key': 'synthetic'})
        with self.assertRaises(ValueError):
            runtime.submit('owner/repo', 'one', {**self.request, 'timeout_seconds': 61})
        self.assertEqual(self.calls, [])

    def test_image_material_is_not_implicitly_enabled(self):
        record = self.finish(self.runtime(), {**self.request, 'materials': [{'type': 'image', 'base64': 'AA=='}]})
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(self.calls, [])

    def test_disabled_provider_does_not_read_key_or_add_route(self):
        config = {'enabled': True, 'resource': 'https://synthetic.example/mcp', 'issuers': {}, 'grants': [],
                  'state_path': str(Path(self.tmp.name) / 'disabled.sqlite'), 'projects': {},
                  'providers': {'gemini': {'type': 'gemini_api', 'enabled': False}}}
        with patch('ai_service.server.os.environ.get', side_effect=AssertionError('must not read key')):
            application = build_application(config)
        self.assertNotIn('gemini', application.runtime.providers)
        self.assertEqual(application.runtime.policies, {})

    def test_configured_provider_is_fixed_and_requires_authorization(self):
        config = {'enabled': True, 'resource': 'https://synthetic.example/mcp', 'issuers': {}, 'grants': [],
                  'state_path': str(Path(self.tmp.name) / 'config.sqlite'), 'projects': {},
                  'providers': {'gemini': {'type': 'gemini_api', 'enabled': True,
                     'model': 'synthetic-model', 'key_env': 'SYNTHETIC_ONLY', 'api_usage_authorized': False}}}
        with self.assertRaises(ValueError):
            build_application(config)
        config['providers']['gemini']['api_usage_authorized'] = True
        application = build_application(config)
        self.assertEqual(application.runtime.providers['gemini'].mode, 'api')


if __name__ == '__main__':
    unittest.main()
