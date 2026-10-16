"""Pure worker tool definitions shared by HTTP responses and offline preflight."""
from __future__ import annotations

import hashlib
import json


def tools_schema_sha256(tools):
    return hashlib.sha256(json.dumps(tools, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def worker_tools(principal, policies, event_provider=None):
    fields = {'task_id': {'type': 'string'}, 'attempt_id': {'type': 'string'},
              'revision': {'type': 'integer', 'minimum': 1},
              'status': {'type': 'string', 'enum': ['completed', 'failed', 'cancelled']},
              'output': {}, 'artifacts': {'type': 'array', 'items': {'type': 'object'}}}
    definitions = [('get_task', 'Read assigned task, current materials, revision and cancellation state.', ['task_id'], ['task_id']),
                   ('claim_task', 'Claim one execution attempt. Reuse the same attempt_id for retries.',
                    ['task_id', 'attempt_id'], ['task_id', 'attempt_id']),
                   ('report_task_result', 'Report a final advisory result for the claimed attempt and current revision.',
                    list(fields), ['task_id', 'attempt_id', 'revision', 'status'])]
    tools = [{'name': name, 'description': description,
             'inputSchema': {'type': 'object', 'properties': {key: fields[key] for key in allowed},
                             'required': required, 'additionalProperties': False}}
            for name, description, allowed, required in definitions if 'tasks:work' in principal.scopes]
    if event_provider is not None and 'events:subscribe' in principal.scopes and principal.provider == event_provider:
        tools.extend({'name': name, 'description': description,
                      'inputSchema': {'type': 'object', 'properties': {
                          'subscription_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
                          'required': ['subscription_id'], 'additionalProperties': False}}
                     for name, description in [
                         ('get_event_subscription', 'Read an owned subscription: active, inactive or gone, with lifecycle timestamps.'),
                         ('delete_event_subscription', 'Delete an owned subscription and stop delivery. Returns gone; deleted=false if already gone.')])
    if principal.project is None:
        for tool in tools:
            tool['inputSchema']['properties']['project'] = {
                'type': 'string', 'enum': sorted(project for project in policies if principal.allows_project(project))}
            tool['inputSchema']['required'].append('project')
    return tools
