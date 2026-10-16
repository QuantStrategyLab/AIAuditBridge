"""Authenticated v2 task API and MCP worker endpoint; bind behind an HTTPS proxy."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jsonschema import Draft202012Validator, ValidationError

from .auth import (AuthenticationError, JWTAuthorizer, Principal, ProjectAdmissionError, grant_projects,
                   project_belongs_to, valid_owner)
from .mcp_logging import AuthDiagnostics, MCPLog, METHODS, log_level
from .events import CallbackEndpointError, DotEventProvider, EventSubscriptions
from .inference import ChatGPTPlanProvider
from .gemini import GeminiProvider
from .notifications import NativeNotifications
from .providers import GrokWebhookProvider
from .runtime import Conflict, NotFound, Policy, TaskRuntime
from .transport import json_body
from .worker_tools import worker_tools, tools_schema_sha256


class Application:
    def __init__(self, runtime, authorizer, events=None):
        self.runtime, self.auth, self.events = runtime, authorizer, events
        self.notifications = NativeNotifications(runtime)

    @staticmethod
    def public_record(record):
        result = dict(record)
        if isinstance(result.get('handle'), dict):
            # Inference adapter keeps its result in the private durable handle until
            # schema validation. Only the validated top-level output is public.
            result['handle'] = {key: value for key, value in result['handle'].items() if key != 'output'}
        return result

    @staticmethod
    def require(principal, scope):
        if scope not in principal.scopes:
            raise PermissionError('required scope not granted')

    def worker(self, principal, task_id):
        self.require(principal, 'tasks:work')
        record = self.runtime.get(principal.project, task_id)
        if not principal.provider or record['provider'] != principal.provider:
            raise PermissionError('worker is not assigned to this task')
        return record

    def event_worker(self, principal):
        self.require(principal, 'events:subscribe')
        if not self.events or principal.provider != self.events.provider_name:
            raise PermissionError('events are unavailable for this worker')

    def authenticate(self, headers):
        principal = self.auth.authenticate(headers.get("Authorization"))
        if not any(principal.allows_project(project) for project in self.runtime.policies):
            raise ProjectAdmissionError(principal)
        return principal

    def project_context(self, principal, project=None):
        selected = principal.project if project is None else project
        if not isinstance(selected, str) or not principal.allows_project(selected):
            raise PermissionError('an authorized project must be selected')
        if selected not in self.runtime.policies:
            raise PermissionError('project admission revoked')
        return replace(principal, project=selected)

    def dispatch(self, method, target, headers, body=None, *, principal=None):
        path = urlsplit(target).path
        if method == 'GET' and path == '/.well-known/oauth-protected-resource':
            return 200, self.auth.resource_metadata()
        principal = principal or self.authenticate(headers)
        if path == '/mcp' and method == 'POST':
            reply = self.mcp(principal, body)
            return (202, None) if reply is None else (200, reply)
        if path == '/mcp' and method == 'GET':
            # This stateless endpoint does not offer an SSE listening stream.
            return 405, {'error': 'method_not_allowed'}
        selectors = parse_qs(urlsplit(target).query, keep_blank_values=True).get('project', [None])
        if len(selectors) != 1:
            raise ValueError('select exactly one project')
        principal = self.project_context(principal, selectors[0])
        if path == '/v1/tasks' and method == 'POST':
            self.require(principal, 'tasks:submit')
            return 202, self.public_record(self.runtime.submit(principal.project, headers.get('Idempotency-Key'), body))
        parts = path.strip('/').split('/')
        if len(parts) in {3, 4} and parts[:2] == ['v1', 'tasks']:
            task_id = parts[2]
            operation = parts[3] if len(parts) == 4 else ''
            if method == 'GET':
                self.require(principal, 'tasks:read')
                # Worker identity cannot read another configured provider's task.
                if principal.provider:
                    self.worker(principal, task_id)
                if operation == '':
                    record = self.runtime.get(principal.project, task_id)
                    return 200, {**self.public_record(record), 'messages': self.runtime.messages(principal.project, task_id)}
                if operation == 'events':
                    after = int(parse_qs(urlsplit(target).query).get('after', ['0'])[0])
                    return 200, {'events': self.runtime.events(principal.project, task_id, after)}
            if method == 'POST':
                self.require(principal, 'tasks:submit')
                if operation == 'messages':
                    return 200, self.public_record(self.runtime.message(principal.project, task_id, headers.get('Idempotency-Key'), body))
                if operation == 'cancel':
                    if body not in (None, {}):
                        raise ValueError('cancel takes no arguments')
                    return 200, self.public_record(self.runtime.cancel(principal.project, task_id))
        raise NotFound('endpoint not found')

    def mcp(self, principal, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise ValueError('a JSON-RPC request is required')
        if 'id' not in message:
            params = message.get('params', {})
            if (message.get('method') != 'notifications/initialized'
                    or not isinstance(params, dict)
                    or ('_meta' in params and not isinstance(params['_meta'], dict))):
                raise ValueError('invalid MCP notification')
            # Authentication and project admission already ran in dispatch.
            # Client initialization does not change task state or permissions.
            return None
        identifier = message['id']
        try:
            method, params = message.get('method'), message.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('params must be an object')
            if '_meta' in params:
                if not isinstance(params['_meta'], dict):
                    raise ValueError('protocol metadata must be an object')
                # MCP envelope metadata is untrusted; it never supplies task
                # arguments, identity, scopes or project admission.
                params = {key: value for key, value in params.items() if key != '_meta'}
            if method == 'initialize':
                self.require(principal, 'tasks:work')
                requested = params.get('protocolVersion')
                if (not isinstance(requested, str)
                        or not isinstance(params.get('capabilities'), dict)
                        or not isinstance(params.get('clientInfo'), dict)):
                    raise ValueError('invalid initialization')
                supported = {'2025-03-26', '2025-06-18', '2025-11-25'}
                try:
                    server_version = version('quant-ai-service')
                except PackageNotFoundError:
                    server_version = 'development'
                result = {'protocolVersion': requested if requested in supported else '2025-11-25',
                          'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'quant-ai-service', 'version': server_version}}
            elif method == 'ping':
                result = {}
            elif method == 'server/discover':
                caps = {'tools': {}}
                if self.events and 'events:subscribe' in principal.scopes and principal.provider == self.events.provider_name:
                    caps['events'] = {}
                result = {'resultType': 'complete', 'supportedVersions': ['2026-07-28'], 'capabilities': caps}
            elif method == 'tools/list':
                if 'tasks:work' not in principal.scopes:
                    self.event_worker(principal)
                result = {'tools': self.tools(principal)}
            elif method == 'tools/call':
                result = self.call_tool(principal, params)
            elif method in {'events/list', 'events/subscribe', 'events/unsubscribe'}:
                self.event_worker(principal)
                if method == 'events/list':
                    result = self.events.catalog()
                elif method == 'events/subscribe':
                    if not isinstance(params.get('arguments'), dict):
                        raise ValueError('project filter is required')
                    self.project_context(principal, params['arguments'].get('project'))
                    result = self.events.subscribe(principal.identity, params)
                else:
                    if not isinstance(params.get('arguments'), dict):
                        raise ValueError('project filter is required')
                    self.project_context(principal, params['arguments'].get('project'))
                    result = self.events.unsubscribe_request(principal.identity, params)
            else:
                return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32601, 'message': 'method not found'}}
            return {'jsonrpc': '2.0', 'id': identifier, 'result': result}
        except CallbackEndpointError as error:
            return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32015,
                    'message': 'CallbackEndpointError', 'data': {'reason': error.reason}}}
        except (ValueError, PermissionError, NotFound, ValidationError) as error:
            code = -32009 if isinstance(error, Conflict) else -32003 if isinstance(error, PermissionError) else -32602
            # Schema errors contain supplied values; keep private materials out of errors.
            description = 'task conflict' if isinstance(error, Conflict) else 'access denied' if isinstance(error, PermissionError) else 'invalid arguments'
            return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': code, 'message': description}}

    def tools(self, principal):
        return worker_tools(principal, self.runtime.policies,
                            self.events.provider_name if self.events else None)

    def call_tool(self, principal, params):
        if set(params) != {'name', 'arguments'} or not isinstance(params['arguments'], dict):
            raise ValueError('tool name and arguments are required')
        name, args = params['name'], params['arguments']
        if name in {'get_event_subscription', 'delete_event_subscription'}:
            self.event_worker(principal)
        elif name in {'get_task', 'claim_task', 'report_task_result'}:
            self.require(principal, 'tasks:work')
        definitions = {tool['name']: tool for tool in self.tools(principal)}
        if name not in definitions:
            raise ValueError('unknown tool')
        from jsonschema import Draft202012Validator
        Draft202012Validator(definitions[name]['inputSchema']).validate(args)
        principal = self.project_context(principal, args.get('project'))
        if name in {'get_event_subscription', 'delete_event_subscription'}:
            operation = self.events.status if name == 'get_event_subscription' else self.events.unsubscribe
            record = operation(principal.identity, args['subscription_id'], owner=principal.project)
        elif name == 'get_task':
            record = self.worker(principal, args['task_id'])
            record = {**record, 'messages': self.runtime.messages(principal.project, args['task_id'])}
        elif name == 'claim_task':
            self.worker(principal, args['task_id'])
            record = self.runtime.claim(principal.project, args['task_id'], principal.provider,
                                        principal.identity, args['attempt_id'])
        else:
            self.worker(principal, args['task_id'])
            record = self.runtime.report(principal.project, args['task_id'], principal.provider,
                                          principal.identity, args['attempt_id'], args['revision'],
                                          args['status'], args.get('output'), args.get('artifacts'))
        record = self.public_record(record)
        return {'content': [{'type': 'text', 'text': json.dumps(record, ensure_ascii=False)}],
                'structuredContent': record, 'isError': False}


def validate_configuration(config):
    """Validate startup configuration without state, network or credential reads."""
    if config.get('enabled') is not True:
        raise ValueError('service is disabled; configure identity and native assistant before enabling')
    log_level(config.get('mcp_log_level', 'INFO'))
    auth = JWTAuthorizer(config['resource'], config['issuers'], config['grants'])
    policies = {owner: Policy(providers=item['providers'],
                             models={key: set(models) for key, models in item['models'].items()},
                             max_active=item.get('max_active', 4),
                             max_timeout_seconds=item.get('max_timeout_seconds', 3600))
                for owner, item in config['projects'].items()}
    owner = config.get('owner')
    if owner is not None and (not valid_owner(owner)
            or any(not project_belongs_to(project, owner) for project in policies)):
        raise ValueError('service projects must belong to its configured owner')
    for grant in config['grants']:
        if owner is not None and (grant.get('owner', owner) != owner
                or any(not project_belongs_to(project, owner) for project in grant_projects(grant))):
            raise ValueError('grant is outside the configured service owner')
        if 'owner' in grant and any(not project_belongs_to(project, grant['owner']) for project in policies):
            raise ValueError('owner worker cannot span service owners')
        if 'projects' in grant and not grant_projects(grant) <= policies.keys():
            raise ValueError('worker project list must be explicitly admitted')
    Path(config['state_path'])
    limit = config.get('max_request_bytes', 2_000_000)
    if type(limit) is not int or not 1 <= limit <= 16_000_000:
        raise ValueError('invalid request size limit')
    dot_count = 0
    for item in config['providers'].values():
        kind = item['type']
        if kind == 'dot_events':
            dot_count += 1
        elif kind == 'grok_webhook':
            item['key_env']
            # A synthetic key validates static constructor arguments only.
            GrokWebhookProvider(item['url'], 'configuration-check', config['resource'])
        elif kind == 'chatgpt_plan_oauth':
            item['token_env']
            ChatGPTPlanProvider(lambda: None, plan_usage_authorized=item.get('plan_usage_authorized', False))
        elif kind == 'gemini_api':
            if item.get('enabled') is not True:
                continue
            item['key_env']
            GeminiProvider(lambda: None, model=item['model'],
                api_usage_authorized=item.get('api_usage_authorized', False),
                max_output_tokens=item.get('max_output_tokens', 1024),
                timeout_seconds=item.get('timeout_seconds', 60))
        else:
            raise ValueError('unsupported native provider')
    if dot_count > 1:
        raise ValueError('one Dot event provider per service deployment')
    return auth, policies


def check_worker_tools(config, identity, expected_projects):
    """Check owner-worker schemas without creating runtime, subscriptions or a JWT."""
    _, policies = validate_configuration(config)
    grants = [grant for grant in config['grants'] if grant.get('identity') == identity]
    if len(grants) != 1:
        raise ValueError('worker selector must match exactly once')
    grant = grants[0]
    scopes = frozenset(grant['scopes'])
    if scopes != {'tasks:read', 'tasks:work', 'events:subscribe'}:
        raise ValueError('expected a read/work/events worker without submit scope')
    if not grant.get('owner') or grant['owner'] != config.get('owner'):
        raise ValueError('worker must match the configured service owner')
    provider = grant.get('provider')
    if config['providers'].get(provider, {}).get('type') != 'dot_events':
        raise ValueError('worker must use the configured event provider')
    expected = sorted(set(expected_projects))
    if not expected or len(expected) != len(expected_projects):
        raise ValueError('expected projects must be nonempty and unique')
    principal = Principal(identity, None, scopes, provider, owner=grant['owner'])
    tools = worker_tools(principal, policies, provider)
    if {tool['name'] for tool in tools} != {'get_task', 'claim_task', 'report_task_result',
                                          'get_event_subscription', 'delete_event_subscription'} or len(tools) != 5:
        raise ValueError('expected five worker tools')
    for tool in tools:
        schema = tool['inputSchema']
        Draft202012Validator.check_schema(schema)
        if (schema['required'].count('project') != 1
                or schema['properties']['project']['enum'] != expected):
            raise ValueError('required project enum must match expected projects exactly')
    return {'valid': True, 'worker_match_count': 1, 'owner_matches': True,
            'provider_matches': True, 'worker_has_submit': False,
            'tool_count': len(tools), 'project_required_in_all': True,
            'project_enum_exact_match': True, 'tools_schema_sha256': tools_schema_sha256(tools),
            'preflight_kind': 'config_derived_worker_tools', 'real_oauth_verified': False,
            'running_process_verified': False}


def build_application(config):
    auth, policies = validate_configuration(config)
    providers = {}
    runtime = TaskRuntime(Path(config['state_path']), providers, policies)
    events = None
    for name, item in config['providers'].items():
        if item['type'] == 'grok_webhook':
            key = os.environ.get(item['key_env'])
            providers[name] = GrokWebhookProvider(item['url'], key, config['resource'])
        elif item['type'] == 'dot_events':
            if events is not None:
                raise ValueError('one Dot event provider per service deployment')
            events = EventSubscriptions(runtime, provider_name=name, can_access=auth.can_access_events)
            providers[name] = DotEventProvider(events)
        elif item['type'] == 'chatgpt_plan_oauth':
            environment_name = item['token_env']
            providers[name] = ChatGPTPlanProvider(lambda name=environment_name: os.environ.get(name),
                                                  plan_usage_authorized=item.get('plan_usage_authorized', False))
        elif item['type'] == 'gemini_api':
            if item.get('enabled') is not True:
                continue
            environment_name = item['key_env']
            providers[name] = GeminiProvider(lambda name=environment_name: os.environ.get(name),
                model=item['model'], api_usage_authorized=item.get('api_usage_authorized', False),
                max_output_tokens=item.get('max_output_tokens', 1024),
                timeout_seconds=item.get('timeout_seconds', 60))
        else:
            raise ValueError('unsupported native provider')
    return Application(runtime, auth, events)


def make_server(application, port, *, max_request_bytes=2_000_000, mcp_log_level='INFO'):
    if type(max_request_bytes) is not int or not 1 <= max_request_bytes <= 16_000_000:
        raise ValueError('invalid request size limit')
    audit = MCPLog(mcp_log_level)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass  # No tokens, request bodies or private task identifiers in access logs.

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            self.connection.settimeout(15)
            extra = {}
            is_mcp = urlsplit(self.path).path == '/mcp'
            principal, diagnostics = None, AuthDiagnostics()
            decision, reason, method = 'not_attempted', 'invalid_request', 'auth'
            try:
                if self.headers.get('Transfer-Encoding'):
                    raise ValueError('chunked requests are not supported')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= max_request_bytes:
                    raise ValueError('request size exceeds service limit')
                headers = {name: self.headers.get(name) for name in ['Authorization', 'Idempotency-Key']}
                discovery = self.command == 'GET' and urlsplit(self.path).path == '/.well-known/oauth-protected-resource'
                if not discovery:
                    decision = 'denied'
                    principal = application.authenticate(headers)
                    diagnostics = principal.diagnostics or AuthDiagnostics(effective_scopes=tuple(principal.scopes))
                    decision, reason = 'allowed', 'granted'
                if discovery and length:
                    raise ValueError('discovery takes no body')
                body = None
                if length:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError('incomplete request')
                    body = json.loads(raw)
                if is_mcp:
                    requested = body.get('method') if isinstance(body, dict) else None
                    method = requested if isinstance(requested, str) and requested in METHODS else 'unknown'
                    if self.command == 'GET':
                        method = 'http/get'
                status, payload = application.dispatch(self.command, self.path, headers, body, principal=principal)
                if status == 405:
                    reason = 'method_not_allowed'
            except AuthenticationError as error:
                diagnostics, reason = error.diagnostics, error.reason_code
                status, payload = 401, {'error': 'authentication_required'}
                resource = urlsplit(application.auth.resource)
                metadata = resource.scheme + '://' + resource.netloc + '/.well-known/oauth-protected-resource'
                extra['WWW-Authenticate'] = 'Bearer resource_metadata="' + metadata + '"'
            except ProjectAdmissionError as error:
                principal, reason = error.principal, 'project_not_admitted'
                diagnostics = principal.diagnostics or AuthDiagnostics(effective_scopes=tuple(principal.scopes))
                status, payload = 403, {'error': 'access_denied'}
            except PermissionError:
                reason = 'access_denied'
                status, payload = 403, {'error': 'access_denied'}
            except NotFound:
                reason = 'not_found'
                status, payload = 404, {'error': 'not_found'}
            except Conflict:
                reason = 'task_conflict'
                status, payload = 409, {'error': 'task_conflict'}
            except (ValueError, TypeError, ValidationError):
                reason = 'invalid_request'
                status, payload = 400, {'error': 'invalid_request'}
            except Exception:
                reason = 'internal_error'
                status, payload = 500, {'error': 'internal_error'}
            provider_matched = None if principal is None else bool(application.events and principal.provider == application.events.provider_name)
            try:
                encoded = b'' if status == 202 and payload is None else json_body(payload)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(encoded)
                self.wfile.flush()
                self.close_connection = True
            except Exception:
                if is_mcp:
                    audit.emit(method=method, status=status, decision=decision, reason=reason,
                               diagnostics=diagnostics, provider_matched=provider_matched, payload=payload,
                               write_failed=True)
                raise
            if is_mcp:
                audit.emit(method=method, status=status, decision=decision, reason=reason,
                           diagnostics=diagnostics, provider_matched=provider_matched, payload=payload)

    class BoundedServer(ThreadingHTTPServer):
        def handle_error(self, *_):
            # The default traceback includes the peer address and exception data.
            pass

        def __init__(self, *args):
            self.slots = threading.BoundedSemaphore(16)
            super().__init__(*args)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

    server = BoundedServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def main(configuration_guard=None):
    parser = argparse.ArgumentParser(description='Generic native-agent task service')
    parser.add_argument('--config', required=True)
    parser.add_argument('--port', type=int, default=8090)
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument('--check-config', action='store_true', help='validate configuration without starting the service')
    checks.add_argument('--check-worker-tools', action='store_true', help='check owner-worker schemas without starting the service or authenticating')
    parser.add_argument('--worker-identity', help='exact configured worker identity for --check-worker-tools')
    parser.add_argument('--expect-project', action='append', help='exact expected project; repeat for every admitted project')
    args = parser.parse_args()
    if args.check_worker_tools and (not args.worker_identity or not args.expect_project):
        parser.error('--check-worker-tools requires --worker-identity and --expect-project')
    if not args.check_worker_tools and (args.worker_identity is not None or args.expect_project is not None):
        parser.error('worker selectors require --check-worker-tools')
    config_path = Path(args.config)
    try:
        if config_path.stat().st_mode & 0o077:
            raise ValueError('config must be readable only by its owner')
        raw = config_path.read_bytes()
        config = json.loads(raw)
        if configuration_guard:
            configuration_guard(config)
        if args.check_worker_tools:
            summary = check_worker_tools(config, args.worker_identity, args.expect_project)
            summary['config_sha256'] = hashlib.sha256(raw).hexdigest()
            print(json.dumps(summary))
            return
        if args.check_config:
            _, policies = validate_configuration(config)
            print(json.dumps({'valid': True, 'projects': len(policies),
                              'providers': len(config['providers'])}))
            return
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        if args.check_config or args.check_worker_tools:
            print(json.dumps({'valid': False, 'error': 'configuration rejected'}))
            raise SystemExit(1) from None
        parser.error('configuration rejected; check owner-only permissions and configuration')
    application = build_application(config)
    lock_path = application.runtime.path.with_suffix('.service.lock')
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        parser.error('a dispatcher already owns this state database')
    stop = threading.Event()
    server = make_server(application, args.port, max_request_bytes=config.get("max_request_bytes", 2_000_000),
                         mcp_log_level=config.get('mcp_log_level', 'INFO'))
    def drive():
        while not stop.is_set():
            try:
                application.runtime.tick()
                application.notifications.flush()
                if application.events:
                    application.events.flush()
            except Exception:
                # Stop admitting requests if the durable dispatcher cannot advance them.
                stop.set()
                server.shutdown()
                return
            stop.wait(1)
    dispatcher = threading.Thread(target=drive, daemon=True)
    dispatcher.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        dispatcher.join(timeout=30)
        os.close(lock_fd)


if __name__ == '__main__':
    main()
