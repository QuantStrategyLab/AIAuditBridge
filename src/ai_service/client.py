"""V2 task client. Credentials are supplied by the caller's approved token client."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .transport import PublicHTTPS, json_body


class TaskClient:
    def __init__(self, resource, token, *, transport=None, max_response_bytes=2_000_000):
        parsed = urlsplit(resource)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('service URL must be HTTPS without credentials, query or fragment')
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 16_000_000:
            raise ValueError('invalid response size limit')
        self.resource = resource.rstrip('/')
        self.token = token
        self.transport = transport or PublicHTTPS(max_response_bytes=max_response_bytes)

    def request(self, method, path, body=None, idempotency_key=None):
        token = self.token() if callable(self.token) else self.token
        if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
            raise ValueError('an authenticated token is required')
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key or '\r' in idempotency_key or '\n' in idempotency_key:
                raise ValueError('invalid idempotency key')
            headers['Idempotency-Key'] = idempotency_key
        reply = self.transport.request(self.resource + path, method=method, headers=headers,
                                       body=b'' if body is None else json_body(body))
        if not 200 <= reply.status < 300:
            raise RuntimeError('AI service request rejected (HTTP ' + str(reply.status) + ')')
        return json.loads(reply.body)

    def submit(self, request, idempotency_key):
        return self.request('POST', '/v1/tasks', request, idempotency_key)

    def get(self, task_id):
        return self.request('GET', '/v1/tasks/' + quote(task_id, safe=''))

    def message(self, task_id, message, idempotency_key):
        return self.request('POST', '/v1/tasks/' + quote(task_id, safe='') + '/messages', message, idempotency_key)

    def cancel(self, task_id):
        return self.request('POST', '/v1/tasks/' + quote(task_id, safe='') + '/cancel', {})

    def wait(self, task_id, *, timeout_seconds=600, poll_interval=2):
        if timeout_seconds <= 0 or not 0 < poll_interval <= 30:
            raise ValueError('invalid polling limits')
        deadline = time.monotonic() + timeout_seconds
        while True:
            record = self.get(task_id)
            if record['status'] in {'completed', 'failed', 'cancelled', 'invalid_output', 'outcome_unknown'}:
                return record
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # A client timeout cannot imply that external work has stopped.
                raise TimeoutError('task wait timed out; reuse the existing task id')
            time.sleep(min(poll_interval, remaining))


def github_token(audience, *, transport=None):
    url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL', '')
    secret = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or not host.endswith('.actions.githubusercontent.com')
            or parsed.username or parsed.password or parsed.fragment or not secret
            or '\r' in secret or '\n' in secret):
        raise ValueError('approved GitHub workload identity is unavailable')
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != 'audience']
    query.append(('audience', audience))
    target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ''))
    reply = (transport or PublicHTTPS()).request(target, method='GET', headers={'Authorization': 'Bearer ' + secret})
    if reply.status != 200:
        raise RuntimeError('GitHub workload identity request rejected')
    result = json.loads(reply.body).get('value')
    if not isinstance(result, str) or not result:
        raise RuntimeError('GitHub workload identity response is invalid')
    return result


def client_from_env(*, max_response_bytes=2_000_000):
    resource = os.environ.get('AI_SERVICE_URL', '')
    audience = os.environ.get('AI_SERVICE_AUDIENCE', '')
    if not resource or not audience:
        raise ValueError('AI_SERVICE_URL and AI_SERVICE_AUDIENCE are required')
    if os.environ.get('AI_SERVICE_TOKEN'):
        return TaskClient(resource, lambda: os.environ.get('AI_SERVICE_TOKEN'), max_response_bytes=max_response_bytes)
    return TaskClient(resource, lambda: github_token(audience), max_response_bytes=max_response_bytes)


def main():
    parser = argparse.ArgumentParser(description='Submit and read project-scoped AI tasks')
    parser.add_argument('--url', default=os.environ.get('AI_SERVICE_URL', ''))
    parser.add_argument('--audience', default=os.environ.get('AI_SERVICE_AUDIENCE', ''))
    parser.add_argument('--token-env', default='AI_SERVICE_TOKEN')
    commands = parser.add_subparsers(dest='command', required=True)
    submit = commands.add_parser('submit')
    submit.add_argument('--request-file', required=True)
    submit.add_argument('--idempotency-key', required=True)
    read = commands.add_parser('get')
    read.add_argument('task_id')
    wait = commands.add_parser('wait')
    wait.add_argument('task_id')
    wait.add_argument('--timeout-seconds', type=int, default=600)
    cancel = commands.add_parser('cancel')
    cancel.add_argument('task_id')
    message = commands.add_parser('message')
    message.add_argument('task_id')
    message.add_argument('--request-file', required=True)
    message.add_argument('--idempotency-key', required=True)
    args = parser.parse_args()
    if not args.url:
        parser.error('--url or AI_SERVICE_URL is required')
    if not os.environ.get(args.token_env) and not args.audience:
        parser.error('an approved token client or --audience is required')
    client = TaskClient(args.url, lambda: os.environ.get(args.token_env) or github_token(args.audience))
    if args.command == 'submit':
        record = client.submit(json.loads(Path(args.request_file).read_text()), args.idempotency_key)
    elif args.command == 'message':
        record = client.message(args.task_id, json.loads(Path(args.request_file).read_text()), args.idempotency_key)
    elif args.command == 'cancel':
        record = client.cancel(args.task_id)
    elif args.command == 'wait':
        record = client.wait(args.task_id, timeout_seconds=args.timeout_seconds)
    else:
        record = client.get(args.task_id)
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
