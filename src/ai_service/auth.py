"""JWT resource-server verification against explicit project/workflow grants."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import jwt

from .transport import PublicHTTPS
from .mcp_logging import AuthDiagnostics, client_name, scope_names


class AuthenticationError(PermissionError):
    def __init__(self, message, *, reason_code='token_rejected', diagnostics=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.diagnostics = diagnostics or AuthDiagnostics()


class ProjectAdmissionError(PermissionError):
    def __init__(self, principal):
        super().__init__('project admission revoked')
        self.principal = principal


def valid_owner(owner):
    return isinstance(owner, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', owner) is not None


def project_belongs_to(project, owner):
    return (isinstance(project, str) and valid_owner(owner)
            and re.fullmatch(re.escape(owner) + r'/[A-Za-z0-9_.-]+', project) is not None
            and project.split('/')[1] not in {'.', '..'})


def grant_projects(grant):
    """Validate a legacy selector, explicit list, or one exact owner worker scope."""
    if sum(name in grant for name in ('project', 'projects', 'owner')) != 1:
        raise ValueError('use exactly one project, projects list or owner')
    if 'project' in grant:
        project = grant['project']
        if not isinstance(project, str) or not project:
            raise ValueError('a project is required')
        return frozenset({project})
    if 'owner' in grant:
        if not valid_owner(grant['owner']):
            raise ValueError('worker owner must be an exact owner name')
        projects = []
    else:
        projects = grant['projects']
        if (not isinstance(projects, list) or not projects
                or any(not isinstance(project, str)
                       or re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', project) is None
                       for project in projects)):
            raise ValueError('an explicit projects list is required')
        if len(set(projects)) != len(projects) or len({project.split('/')[0] for project in projects}) != 1:
            raise ValueError('worker projects must be unique and belong to one owner')
    scopes = set(grant['scopes'])
    if (grant['issuer'] == 'https://token.actions.githubusercontent.com'
            or not isinstance(grant.get('provider'), str) or not grant['provider']
            or not isinstance(grant.get('claims'), dict)
            or not isinstance(grant['claims'].get('azp'), str) or not grant['claims']['azp'].strip()
            or 'tasks:work' not in scopes or scopes - {'tasks:read', 'tasks:work', 'events:subscribe'}):
        raise ValueError('multi-project scope requires a connection-bound worker without submit permission')
    return frozenset(projects)


def grant_allows_project(grant, project):
    projects = grant_projects(grant)
    return project_belongs_to(project, grant['owner']) if 'owner' in grant else project in projects


@dataclass(frozen=True)
class Principal:
    identity: str
    project: str | None
    scopes: frozenset[str]
    provider: str | None = None
    diagnostics: AuthDiagnostics | None = None
    projects: frozenset[str] = frozenset()
    owner: str | None = None

    @property
    def allowed_projects(self):
        return self.projects or (frozenset({self.project}) if self.project is not None else frozenset())

    def allows_project(self, project):
        if self.owner is not None:
            return project_belongs_to(project, self.owner)
        return project in self.allowed_projects


class JWTAuthorizer:
    def __init__(self, resource, issuers, grants, *, transport=None):
        self.resource, self.issuers, self.grants = resource, issuers, grants
        self.transport = transport or PublicHTTPS()
        self._keys = {}
        for issuer, config in issuers.items():
            if config.get('require_scope', True) is not True and issuer != 'https://token.actions.githubusercontent.com':
                raise ValueError('scope claims are required outside GitHub workload identity')
        for grant in grants:
            grant_projects(grant)
            if grant['issuer'] == 'https://token.actions.githubusercontent.com':
                constraints = grant.get('claims', {})
                if 'pull_request_workflow' in grant:
                    workflow = grant['pull_request_workflow']
                    if (not isinstance(workflow, str)
                            or re.fullmatch(r'\.github/workflows/[A-Za-z0-9_-]+\.ya?ml', workflow) is None
                            or re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', grant['project']) is None
                            or grant['subject'] != f"repo:{grant['project']}:pull_request"
                            or constraints.get('event_name') != 'pull_request'
                            or constraints.get('base_ref') != 'main'
                            or constraints.get('repository_visibility') != 'public'
                            or not isinstance(constraints.get('repository_id'), str)
                            or re.fullmatch(r'[1-9][0-9]*', constraints['repository_id']) is None
                            or set(grant['scopes']) - {'tasks:read', 'tasks:submit'}):
                        raise ValueError('PR grants require a public repository ID, main base and task-only scopes')
                elif not {'repository', 'workflow_ref', 'ref'} <= set(constraints):
                    raise ValueError('GitHub grants must bind repository, workflow_ref and ref')
                if constraints.get('repository') != grant['project']:
                    raise ValueError('GitHub repository must match project admission')

    def _verify_pull_request(self, grant, claims):
        # GitHub's PR subject identifies the base repository, including fork PRs.
        # Check the current public PR metadata before granting any task access.
        ref, sha = claims.get('ref'), claims.get('sha')
        match = re.fullmatch(r'refs/pull/([1-9][0-9]*)/merge', ref) if isinstance(ref, str) else None
        if (match is None or not isinstance(sha, str) or re.fullmatch(r'[0-9a-f]{40}', sha) is None
                or claims.get('workflow_ref') != f"{grant['project']}/{grant['pull_request_workflow']}@{ref}"):
            raise ValueError('PR workflow or revision not admitted')
        number = int(match[1])
        reply = self.transport.request(f"https://api.github.com/repos/{grant['project']}/pulls/{number}",
            method='GET', timeout=5, headers={'Accept': 'application/vnd.github+json',
                'User-Agent': 'ai-service-pr-admission', 'X-GitHub-Api-Version': '2022-11-28'})
        if reply.status != 200:
            raise ValueError('PR metadata unavailable')
        pull = json.loads(reply.body)
        repository_id = grant['claims']['repository_id']
        if (pull['number'] != number or pull['state'] != 'open' or pull['merge_commit_sha'] != sha
                or pull['base']['ref'] != grant['claims']['base_ref']
                or any(repo['full_name'] != grant['project'] or str(repo['id']) != repository_id
                       for repo in (pull['head']['repo'], pull['base']['repo']))):
            raise ValueError('PR must be a current same-repository merge')

    def _jwks(self, issuer):
        cached = self._keys.get(issuer)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        reply = self.transport.request(self.issuers[issuer]['jwks_url'], method='GET')
        if reply.status != 200:
            raise AuthenticationError('identity key service unavailable')
        body = json.loads(reply.body)
        if not isinstance(body.get('keys'), list) or not body['keys']:
            raise AuthenticationError('identity key service unavailable')
        self._keys[issuer] = (time.monotonic() + 300, body['keys'])
        return body['keys']

    def authenticate(self, authorization):
        if not isinstance(authorization, str) or not authorization.startswith('Bearer '):
            raise AuthenticationError('bearer authentication required', reason_code='bearer_required')
        token = authorization[7:]
        if len(token) > 16384:
            raise AuthenticationError('invalid bearer token', reason_code='token_too_large')
        reason, diagnostics = 'invalid_token', AuthDiagnostics()
        try:
            # Unverified issuer is only a selector into configured trusted issuers.
            hint = jwt.decode(token, options={'verify_signature': False})
            issuer = hint['iss']
            reason = 'issuer_not_trusted'
            if issuer not in self.issuers:
                raise ValueError('issuer not trusted')
            config = self.issuers[issuer]
            header = jwt.get_unverified_header(token)
            algorithms = config.get('algorithms', ['RS256'])
            reason = 'algorithm_not_trusted'
            if not algorithms or set(algorithms) - {'RS256', 'ES256'} or header.get('alg') not in algorithms:
                raise ValueError('algorithm not trusted')
            reason = 'signing_key_unavailable'
            keys = [key for key in self._jwks(issuer) if key.get('kid') == header.get('kid')
                    and key.get('use', 'sig') == 'sig' and key.get('alg', header['alg']) == header['alg']]
            if len(keys) != 1:
                reason = 'signing_key_not_found'
                raise ValueError('unknown signing key')
            reason = 'jwt_validation_failed'
            key = jwt.PyJWK.from_dict(keys[0], algorithm=header['alg']).key
            claims = jwt.decode(token, key, algorithms=algorithms, issuer=issuer,
                audience=self.resource,
                options={'require': ['iss', 'sub', 'aud', 'exp', 'iat'], 'strict_aud': True})
            reason = 'invalid_claims'
            if type(claims['exp']) not in {int, float} or type(claims['iat']) not in {int, float}:
                raise ValueError('invalid token timestamps')
            if not isinstance(claims['sub'], str) or not claims['sub']:
                raise ValueError('invalid subject')
            # Retain only safe names after all JWT validation has succeeded.
            scope = claims.get('scope', '')
            azp = client_name(claims.get('azp'), self.grants)
            token_scopes = scope_names(scope.split()) if 'scope' in claims and isinstance(scope, str) else None
            diagnostics = AuthDiagnostics(azp=azp, token_scopes=token_scopes)
            reason = 'grant_not_admitted'
            matches = [grant for grant in self.grants if grant['issuer'] == issuer
                       and grant['subject'] == claims['sub']
                       and all(claims.get(name) == value for name, value in grant.get('claims', {}).items())]
            if len(matches) != 1:
                raise ValueError('subject or workflow not admitted')
            grant = matches[0]
            diagnostics = AuthDiagnostics(azp, scope_names(grant['scopes']), token_scopes)
            if 'pull_request_workflow' in grant:
                reason = 'workflow_not_admitted'
                self._verify_pull_request(grant, claims)
            scopes = set(grant['scopes'])
            if config.get('require_scope', True):
                reason = 'invalid_scopes'
                if not isinstance(scope, str):
                    raise ValueError('invalid scopes')
                scopes.intersection_update(scope.split())
            projects = grant_projects(grant)
            project = next(iter(projects)) if len(projects) == 1 else None
            diagnostics = AuthDiagnostics(azp, diagnostics.grant_scopes, token_scopes, scope_names(scopes))
            return Principal(grant['identity'], project, frozenset(scopes), grant.get('provider'),
                             diagnostics=diagnostics, projects=projects, owner=grant.get('owner'))
        except Exception:
            # Never expose JWT, issuer response, keys or raw validation errors.
            raise AuthenticationError('bearer token rejected', reason_code=reason, diagnostics=diagnostics) from None

    def can_access_events(self, identity, project, provider):
        return any(grant['identity'] == identity and grant_allows_project(grant, project)
                   and grant.get('provider') == provider and 'events:subscribe' in grant['scopes']
                   for grant in self.grants)

    def resource_metadata(self):
        return {'resource': self.resource, 'authorization_servers': [issuer for issuer in self.issuers
                    if issuer != 'https://token.actions.githubusercontent.com'],
                'scopes_supported': ['tasks:read', 'tasks:submit', 'tasks:work', 'events:subscribe', 'offline_access']}
