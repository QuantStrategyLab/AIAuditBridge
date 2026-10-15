"""Allowlisted MCP diagnostics; never serialize requests, claims or exceptions."""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass


SCOPES = frozenset({'tasks:read', 'tasks:submit', 'tasks:work', 'events:subscribe', 'offline_access'})
TOOLS = frozenset({'get_task', 'claim_task', 'report_task_result',
                   'get_event_subscription', 'delete_event_subscription'})
METHODS = frozenset({'initialize', 'ping', 'server/discover', 'tools/list', 'tools/call',
                     'events/list', 'events/subscribe', 'events/unsubscribe', 'notifications/initialized'})
REASONS = frozenset({'granted', 'bearer_required', 'token_too_large', 'invalid_token',
                     'issuer_not_trusted', 'algorithm_not_trusted', 'signing_key_unavailable',
                     'signing_key_not_found', 'jwt_validation_failed', 'invalid_claims',
                     'grant_not_admitted', 'workflow_not_admitted', 'invalid_scopes',
                     'token_rejected', 'project_not_admitted', 'invalid_request', 'access_denied',
                     'not_found', 'task_conflict', 'internal_error', 'method_not_allowed',
                     'method_not_found', 'invalid_arguments', 'scope_or_provider_denied',
                     'callback_endpoint_rejected', 'response_write_failed'})
LEVELS = {'DEBUG': logging.DEBUG, 'INFO': logging.INFO, 'WARNING': logging.WARNING,
          'ERROR': logging.ERROR, 'CRITICAL': logging.CRITICAL, 'OFF': 100}


def log_level(value):
    if not isinstance(value, str) or value not in LEVELS:
        raise ValueError('invalid MCP log level')
    return LEVELS[value]


def scope_names(values):
    if values is None:
        return None
    return tuple(sorted({value if isinstance(value, str) and value in SCOPES
                         else '[REDACTED_UNKNOWN_SCOPE]' for value in values}))


def client_name(value, grants):
    # Only verified azp values that name a configured client may be attributed.
    if (not isinstance(value, str) or re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', value) is None
            or not any(grant.get('claims', {}).get('azp') == value for grant in grants)):
        return None
    return value


@dataclass(frozen=True)
class AuthDiagnostics:
    azp: str | None = None
    grant_scopes: tuple[str, ...] | None = None
    token_scopes: tuple[str, ...] | None = None
    effective_scopes: tuple[str, ...] | None = None


class MCPLog:
    def __init__(self, level='INFO'):
        self.level = log_level(level)
        self.lock = threading.Lock()

    def emit(self, *, method, status, decision, reason, diagnostics, provider_matched,
             payload, write_failed=False):
        try:
            error = payload.get('error') if isinstance(payload, dict) else None
            code = error.get('code') if isinstance(error, dict) else None
            if write_failed:
                reason = 'response_write_failed'
            elif status == 200 and error is not None:
                reason = {-32601: 'method_not_found', -32602: 'invalid_arguments',
                          -32003: 'scope_or_provider_denied', -32009: 'task_conflict',
                          -32015: 'callback_endpoint_rejected'}.get(code, 'internal_error')
            ok = not write_failed and 200 <= status < 300 and error is None
            severity = logging.ERROR if write_failed or status >= 500 else logging.INFO if ok else logging.WARNING
            if severity < self.level:
                return
            names = None
            if method == 'tools/list' and ok:
                names = [tool['name'] if isinstance(tool.get('name'), str) and tool['name'] in TOOLS
                         else '[REDACTED_UNKNOWN_TOOL]' for tool in payload['result']['tools']]
            record = {'event': 'mcp_request_summary', 'method': method if method in METHODS or method in {'auth', 'http/get'} else 'unknown',
                      'result': 'ok' if ok else 'denied',
                      'reason_code': reason if reason in REASONS else 'token_rejected',
                      'auth_decision': decision if decision in {'allowed', 'denied', 'not_attempted'} else 'denied',
                      'azp': diagnostics.azp, 'grant_scopes': scope_names(diagnostics.grant_scopes),
                      'token_scopes': scope_names(diagnostics.token_scopes),
                      'effective_scopes': scope_names(diagnostics.effective_scopes),
                      'events_provider_matched': provider_matched if type(provider_matched) is bool else None,
                      'http_status': status, 'error_code': code if type(code) is int and code in {-32601, -32602, -32003, -32009, -32015} else None,
                      'response_written': not write_failed, 'returned_tool_names': names}
            # Keep concurrent request summaries on separate journal lines.
            with self.lock:
                print(json.dumps(record, separators=(',', ':'), ensure_ascii=True), flush=True)
        except Exception:
            # Diagnostics must not change authentication or response behavior.
            pass
