# Cloudflare Worker Proxy for CodexAuditBridge

This Worker provides a free `workers.dev` HTTPS entry point for CodexAuditBridge when no custom domain is available.

This Worker should stay separate from the Pigbibi CodexGateway Worker so origin URLs, repository allowlists, and logs remain isolated.

The Worker is intentionally thin:

- serves `GET /healthz` locally, proxies legacy `POST /v1/codex-audit`, and proxies async `POST /v1/codex-audit/jobs` plus `GET /v1/codex-audit/jobs/{job_id}`;
- requires a bearer token before proxying and forwards the GitHub Actions OIDC `Authorization` header to the existing Codex audit service;
- keeps the VPS origin URL in a Cloudflare Worker secret, not in git;
- does not store provider keys, GitHub tokens, or Codex credentials.

## Deploy

From this directory:

```bash
npx -y wrangler@latest secret put CODEX_AUDIT_ORIGIN_URL
npx -y wrangler@latest deploy
```

`CODEX_AUDIT_ORIGIN_URL` should be the current HTTPS origin for the Codex audit service. It may be either the service base URL or the full `/v1/codex-audit` URL.

The production shape is:

```text
quantstrategylab-codex-audit-proxy -> VPS HTTPS origin -> codex-audit-service
```

After deploy, set the `CodexAuditBridge` GitHub secret `CODEX_AUDIT_SERVICE_URL` to the Worker URL, for example:

```text
https://quantstrategylab-codex-audit-proxy.<cloudflare-account-subdomain>.workers.dev
```

The account subdomain is controlled by the Cloudflare account. If it is still `pigbibi`, the organization name should be represented in the Worker name, not by changing the whole account subdomain.

Keep `CODEX_AUDIT_SERVICE_AUDIENCE` unchanged unless the origin service audience changes.

The origin service is OIDC-only and should be configured with
`CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES`,
`CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS`, and
`CODEX_AUDIT_SERVICE_ALLOWED_REFS`. Forks should deploy their own Worker and
origin service; this Worker URL is not a shared public Codex endpoint.

## Smoke test

```bash
curl -fsS https://quantstrategylab-codex-audit-proxy.<cloudflare-account-subdomain>.workers.dev/healthz
```

A full async audit request still requires a valid GitHub Actions OIDC bearer
token and should be tested from the bridge workflow. An unauthenticated
`POST /v1/codex-audit/jobs` should return `401`; the Worker may reject it before
it reaches the authenticated origin.
