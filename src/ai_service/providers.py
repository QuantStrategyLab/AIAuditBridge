"""Native persistent-agent triggers. Results arrive through authenticated tools."""
from __future__ import annotations

from urllib.parse import urlsplit

from .runtime import SubmissionRejected
from .transport import PublicHTTPS, json_body


class GrokWebhookProvider:
    mode = 'agent'
    callback_driven = True

    def __init__(self, webhook_url, key, task_resource, *, transport=None):
        parsed = urlsplit(webhook_url)
        resource = urlsplit(task_resource)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('Grok webhook must be a configured HTTPS URL')
        if resource.scheme != 'https' or not resource.hostname or resource.username or resource.password:
            raise ValueError('task resource must be HTTPS')
        if not isinstance(key, str) or not key or '\r' in key or '\n' in key:
            raise ValueError('Grok webhook key is required')
        self._url, self._key = webhook_url, key
        self._resource = task_resource
        self._transport = transport or PublicHTTPS()

    def submit(self, request, *, task_id, owner):
        # The fixed routine reads the task through project-scoped OAuth tools.
        # No credentials, business materials or executable instructions in the trigger.
        self.notify(task_id=task_id, owner=owner, change='requested', revision=1)
        return {'task_id': task_id, 'acceptance': 'routine_started',
                'model_verification': 'unavailable'}

    def notify(self, *, task_id, owner, change, revision):
        reply = self._transport.request(self._url,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._key},
            body=json_body({'type': 'task.requested', 'task_id': task_id,
                            'project': owner, 'resource': self._resource,
                            'change': change, 'revision': revision}))
        if reply.status != 200:
            # Cursor explicitly documents non-200 as "a run did not start".
            raise SubmissionRejected('native routine rejected the trigger')

    def cancel(self, handle):
        # No documented native cancellation API. The task tool exposes cancel_requested;
        # only the authenticated execution attempt can confirm it stopped.
        return None
