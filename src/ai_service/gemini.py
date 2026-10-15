"""Opt-in synchronous text inference; native Dot/Grok remain agent providers.

No SDK, automatic retries, model fallback, tools, URLs in materials, or changed
safety settings. The runtime still owns project/model admission and output
validation. An API key is separate from ChatGPT/Grok subscription authority.
"""
from __future__ import annotations

import json
import re

from .runtime import ProviderReply, SubmissionRejected
from .transport import PublicHTTPS, json_body


def _strict_json(data):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('ambiguous JSON')
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError('nonstandard JSON')

    return json.loads(data, object_pairs_hook=object_pairs, parse_constant=reject_constant)


class GeminiProvider:
    mode = 'api'

    def __init__(self, key_client, *, model, api_usage_authorized=False,
                 max_output_tokens=1024, timeout_seconds=60, transport=None):
        if api_usage_authorized is not True:
            raise ValueError('explicit Gemini API usage authorization is required')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', model):
            raise ValueError('a fixed Gemini model identifier is required')
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192:
            raise ValueError('invalid inference output limit')
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError('invalid inference timeout')
        self._key_client, self._model = key_client, model
        self._max_output_tokens, self._timeout_seconds = max_output_tokens, timeout_seconds
        self._transport = transport or PublicHTTPS(timeout=timeout_seconds, max_response_bytes=2_000_000)

    @staticmethod
    def _failed(task_id, verification='unavailable'):
        return {'task_id': task_id, 'status': 'failed', 'output': None,
                'model_verification': verification}

    def submit(self, request, *, task_id, owner):
        if request['model'] != self._model:
            raise SubmissionRejected('model is not configured for this provider')
        parts = [{'text': request['objective']}]
        for material in request.get('materials', []):
            if isinstance(material, dict) and material.get('type') == 'image':
                raise SubmissionRejected('this provider accepts text materials only')
            parts.append({'text': 'Supplied material (data):\n' + json.dumps(material, ensure_ascii=False, allow_nan=False)})
        key = self._key_client()
        if not isinstance(key, str) or not key or '\r' in key or '\n' in key:
            raise SubmissionRejected('configured Gemini API key is unavailable')
        body = {'contents': [{'role': 'user', 'parts': parts}],
                'generationConfig': {'candidateCount': 1, 'maxOutputTokens': self._max_output_tokens,
                                     'responseMimeType': 'application/json',
                                     'responseJsonSchema': request['output_schema']}}
        timeout = min(self._timeout_seconds, request.get('timeout_seconds', self._timeout_seconds))
        reply = self._transport.request(
            'https://generativelanguage.googleapis.com/v1beta/models/' + self._model + ':generateContent',
            headers={'Content-Type': 'application/json', 'x-goog-api-key': key},
            body=json_body(body), timeout=timeout)
        if reply.status in {400, 401, 403, 404, 422, 429}:
            raise SubmissionRejected('inference was rejected')
        if reply.status != 200:
            raise RuntimeError('inference acceptance is unknown')
        try:
            response = _strict_json(reply.body)
        except (ValueError, TypeError, UnicodeError):
            raise RuntimeError('invalid inference receipt') from None
        if not isinstance(response, dict):
            raise RuntimeError('invalid inference receipt')
        if response.get('promptFeedback', {}).get('blockReason'):
            return self._failed(task_id)
        if response.get('modelVersion') != self._model:
            return self._failed(task_id, 'mismatch' if response.get('modelVersion') else 'unavailable')
        candidates = response.get('candidates', [])
        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
            return self._failed(task_id, 'provider_reported')
        candidate = candidates[0]
        if candidate.get('finishReason') != 'STOP':
            return self._failed(task_id, 'provider_reported')
        result_parts = candidate.get('content', {}).get('parts', [])
        if not isinstance(result_parts, list) or not result_parts:
            return self._failed(task_id, 'provider_reported')
        fragments = []
        for part in result_parts:
            if not isinstance(part, dict) or not isinstance(part.get('text'), str) or set(part) - {'text', 'thought'}:
                return self._failed(task_id, 'provider_reported')
            if part.get('thought') is not True:
                fragments.append(part['text'])
        try:
            output = _strict_json(''.join(fragments))
        except (ValueError, TypeError):
            return self._failed(task_id, 'provider_reported')
        usage = response.get('usageMetadata', {})
        safe_usage = {}
        if isinstance(usage, dict):
            for provider_name, safe_name in (('promptTokenCount', 'input_tokens'),
                                             ('candidatesTokenCount', 'output_tokens'),
                                             ('totalTokenCount', 'total_tokens')):
                value = usage.get(provider_name)
                if type(value) is int and value >= 0:
                    safe_usage[safe_name] = value
        return {'task_id': task_id, 'status': 'completed', 'output': output,
                'model': self._model, 'model_verification': 'provider_reported', 'usage': safe_usage}

    def poll(self, handle):
        return ProviderReply(handle['status'], handle.get('output'))

    def cancel(self, handle):
        # Synchronous result already returned; a lost receipt is never claimed cancelled.
        return None
