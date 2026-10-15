"""Short inference through the documented ChatGPT-plan OAuth Responses route."""
from __future__ import annotations

import base64
import json

from .runtime import ProviderReply, SubmissionRejected
from .transport import PublicHTTPS, json_body


class ChatGPTPlanProvider:
    mode = 'api'

    def __init__(self, token_client, *, plan_usage_authorized=False, transport=None):
        if plan_usage_authorized is not True:
            raise ValueError('registered ChatGPT plan-usage authorization is required')
        self._token_client = token_client
        self._transport = transport or PublicHTTPS(timeout=60, max_response_bytes=4_000_000)

    def submit(self, request, *, task_id, owner):
        token = self._token_client()
        if not isinstance(token, str) or not token or '\r' in token or '\n' in token:
            raise SubmissionRejected('approved OAuth client has no access token')
        # No tools, shell, repository mutation, previous_response_id or paid API fallback.
        materials = request.get('materials', [])
        content = [{'type': 'input_text', 'text': request['objective']}]
        for material in materials:
            if isinstance(material, dict) and material.get('type') == 'image':
                if set(material) != {'type', 'mime_type', 'base64'} or material['mime_type'] not in {'image/png', 'image/jpeg', 'image/webp'}:
                    raise SubmissionRejected('unsupported image material')
                try:
                    decoded = base64.b64decode(material['base64'], validate=True)
                except (ValueError, TypeError):
                    raise SubmissionRejected('invalid image material') from None
                if not decoded:
                    raise SubmissionRejected('empty image material')
                content.append({'type': 'input_image', 'image_url': 'data:' + material['mime_type'] + ';base64,' + material['base64']})
            else:
                content.append({'type': 'input_text', 'text': 'Supplied material (data):\n' + json.dumps(material, ensure_ascii=False)})
        body = {'model': request['model'], 'input': [{'role': 'user', 'content': content}],
                'store': False, 'stream': True,
                'text': {'format': {'type': 'json_schema', 'name': 'task_result',
                                     'schema': request['output_schema'], 'strict': True}}}
        reply = self._transport.request('https://api.openai.com/v1/responses',
            body=json_body(body), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                                          'Accept': 'text/event-stream'})
        if reply.status in {400, 401, 403, 404, 422, 429}:
            raise SubmissionRejected('inference was rejected')
        if reply.status != 200:
            raise RuntimeError('inference acceptance is unknown')
        terminal = None
        # Parse SSE records, including multi-line data. Never accept deltas as completion.
        for record in reply.body.decode('utf-8').replace('\r\n', '\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip(' ') for line in record.split('\n') if line.startswith('data:'))
            if not data or data == '[DONE]':
                continue
            event = json.loads(data)
            if event.get('type') in {'response.completed', 'response.failed', 'response.incomplete'}:
                if terminal is not None:
                    raise RuntimeError('ambiguous terminal inference events')
                terminal = event
        if terminal is None:
            raise RuntimeError('inference ended without a terminal event')
        response = terminal.get('response', {})
        if terminal['type'] != 'response.completed' or response.get('status') != 'completed':
            return {'task_id': task_id, 'status': 'failed', 'output': None, 'model_verification': 'unavailable'}
        if response.get('model') != request['model']:
            return {'task_id': task_id, 'status': 'failed', 'output': None, 'model_verification': 'mismatch'}
        fragments = []
        for item in response.get('output', []):
            if item.get('type') == 'message':
                for part in item.get('content', []):
                    if part.get('type') == 'output_text':
                        fragments.append(part.get('text', ''))
                    elif part.get('type') == 'refusal':
                        return {'task_id': task_id, 'status': 'failed', 'output': None, 'model_verification': 'provider_reported'}
        output = json.loads(''.join(fragments))
        usage = response.get('usage', {})
        safe_usage = {name: value for name, value in usage.items()
                      if name in {'input_tokens', 'output_tokens', 'total_tokens'} and type(value) is int and value >= 0}
        return {'task_id': task_id, 'response_id': response.get('id'), 'status': 'completed', 'output': output,
                'model': response['model'], 'model_verification': 'provider_reported', 'usage': safe_usage}

    def poll(self, handle):
        return ProviderReply(handle['status'], handle.get('output'))

    def cancel(self, handle):
        # Synchronous inference has already returned; no claim that a lost request was cancelled.
        return None
