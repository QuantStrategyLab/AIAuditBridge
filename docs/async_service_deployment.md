# Async Codex Audit Service Deployment

AIAuditBridge uses an async service contract to avoid keeping a GitHub Actions request open while Codex runs on the VPS.

## Architecture

1. A source repository creates or updates an audit issue, then dispatches `QuantStrategyLab/AIAuditBridge`.
2. AIAuditBridge clones the source repository with a scoped GitHub App token, builds the audit prompt, and requests a GitHub Actions OIDC token with audience `quant-codex-audit`.
3. AIAuditBridge submits `POST /v1/codex-audit/jobs` through the Cloudflare Worker.
4. The Worker forwards only Quant audit routes with bearer tokens to the VPS origin. The VPS service validates OIDC signature, audience, repository, workflow ref, git ref, source repository allowlists, and payload size.
5. The VPS service returns a random `job_id`, runs Codex in a background thread, and persists job state in a private local directory.
6. AIAuditBridge polls `GET /v1/codex-audit/jobs/{job_id}` until the job succeeds, fails, or times out.

The synchronous `POST /v1/codex-audit` endpoint remains available for local diagnostics, but production workflows should use the async job endpoints.

## Boundary with Pigbibi CodexGateway

`AIAuditBridge` intentionally stays separate from `Pigbibi/CodexGateway`.

- `CodexGateway` is a generic Codex invocation facade for prompt/context/image/schema calls.
- `AIAuditBridge` owns QuantStrategyLab monthly audit semantics: source issue context, bounded repository snapshots, service patch contracts, source repository allowlists, GitHub App writeback, and generated remediation PRs.
- Do not route Quant monthly audits through the Pigbibi gateway Worker or Pigbibi repository allowlist.
- Do not move audit-specific issue/PR behavior into `CodexGateway`; share only low-level primitives after the HTTP contracts are stable.

The historical self-hosted direct-Codex workflows in `SelfHostedCodexAuditBridge`, `CryptoCodexAuditBridge`, and the legacy `CodexAuditBridge` repository are retired. The production path is the GitHub-hosted `AIAuditBridge` workflow plus async VPS service.

## Permission and secret boundary

- Source repositories, including public QuantStrategyLab repositories, must not store provider keys or the Codex service URL.
- Source repositories should only dispatch the bridge workflow and provide issue/source context. Avoid running Codex directly in public workflows.
- `QuantStrategyLab/AIAuditBridge` is public, so it may contain only client/orchestration code. Its service URL, provider fallback keys, GitHub App private key, and Cloudflare origin stay in GitHub or Cloudflare secrets.
- The VPS service should allow only `QuantStrategyLab/AIAuditBridge` in `CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES`.
- The VPS service should require explicit `CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS` for the canonical `AIAuditBridge` workflows only.
- The VPS service should keep `CODEX_AUDIT_SERVICE_ALLOWED_REFS` as narrow as the enabled workflows allow, normally `refs/heads/main` plus `refs/pull/*/merge` for PR review smoke tests.
- Keep `CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES=public` unless the bridge repository is intentionally private.
- The VPS service should keep `CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES` limited to current audit source repositories and PR review targets.
- The Cloudflare Worker stores only `CODEX_AUDIT_ORIGIN_URL` as a Worker secret. Do not commit the origin URL if it exposes infrastructure details.
- Job IDs are random and status reads still require service authentication. Job responses never include the original prompt.
- Static service bearer tokens are no longer supported; production calls must use GitHub Actions OIDC.

## Open source repository checklist

For public source repositories:

- Do not add `CODEX_AUDIT_SERVICE_URL`, provider API keys, Cloudflare tokens, VPS hostnames, or private keys.
- Avoid exposing bridge dispatch tokens or `id-token: write` service calls to forked pull request workflows.
- Treat issue bodies and generated artifacts as public unless the source repository is private.
- Keep write permissions narrow: issue creation and workflow dispatch should stay in the source workflow; Codex remediation writes should be performed by the bridge using the scoped GitHub App token.
- If a public issue contains private market data, account identifiers, or credentials, remove it before dispatching Codex.

## Quick deploy

### 1. Deploy the VPS service

After merging the async service code, run the manual `VPS Codex Service Ops` workflow with deploy mode, or run on the VPS:

```bash
CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=QuantStrategyLab/AIAuditBridge \
CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS='QuantStrategyLab/AIAuditBridge/.github/workflows/codex_audit.yml@refs/heads/main,QuantStrategyLab/AIAuditBridge/.github/workflows/codex_pr_review.yml@refs/heads/main,QuantStrategyLab/AIAuditBridge/.github/workflows/codex_pr_review.yml@refs/pull/*/merge' \
CODEX_AUDIT_SERVICE_ALLOWED_REFS='refs/heads/main,refs/pull/*/merge' \
CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES='public' \
CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES='QuantStrategyLab/AIAuditBridge,QuantStrategyLab/CryptoLivePoolPipelines,QuantStrategyLab/HkEquitySnapshotPipelines,QuantStrategyLab/UsEquitySnapshotPipelines,QuantStrategyLab/ResearchSignalContextPipelines' \
CODEX_AUDIT_SERVICE_AUDIENCE=quant-codex-audit \
CODEX_AUDIT_SERVICE_MODEL=gpt-5.4 \
CODEX_AUDIT_SERVICE_REASONING_EFFORT=auto \
CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE=1 \
CODEX_AUDIT_SERVICE_OPENAI_USAGE_WINDOW_DAYS=7 \
CODEX_AUDIT_SERVICE_ANTHROPIC_USAGE_WINDOW_DAYS=7 \
CODEX_AUDIT_SERVICE_JOB_DIR=/var/lib/codex-audit-bridge/jobs \
bash scripts/deploy_codex_audit_service.sh deploy
```

The job directory should be owned by the service user and mode `0700`.

The service should rely on an authenticated Codex CLI session and must not
inject OpenAI/Codex API keys into the Codex subprocess.
With `CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE=1`, `/v1/ai/quota` includes a
sanitized Codex account rate-limit snapshot read from the local Codex
app-server. This does not expose Codex auth tokens and should stay behind the
existing dashboard authentication.
With `OPENAI_ADMIN_KEY` set, `/v1/ai/quota` also includes a sanitized OpenAI
completions Usage snapshot. Store the Admin API key only as a secret;
the deploy script writes it to a root-only `0600` EnvironmentFile instead of
embedding it in the public systemd unit. Use `CODEX_AUDIT_SERVICE_OPENAI_ADMIN_API_KEY_IDS`
only for API key IDs, not raw API keys. OpenAI organization-wide costs are kept
in a separate `organization_costs` field and are not mixed into the completions
usage row.
With `ANTHROPIC_ADMIN_KEY` set, `/v1/ai/quota` also includes a sanitized Claude
organization Usage/Cost snapshot. Store it separately from the normal
`ANTHROPIC_API_KEY`; the usage/cost endpoints require an Admin API key. Use
`CODEX_AUDIT_SERVICE_ANTHROPIC_ADMIN_API_KEY_IDS` and
`CODEX_AUDIT_SERVICE_ANTHROPIC_ADMIN_WORKSPACE_IDS` only for IDs, never raw API
keys. When those filters are set, costs are omitted to avoid mixing filtered
usage with an unfiltered cost total.
Set `CODEX_AUDIT_SERVICE_REASONING_EFFORT` only when a hard override is needed;
unset or `auto` keeps task-complexity routing.

Dashboard health panels intentionally separate online service health, organization workflow health, background job status, and artifact/content evidence. See [`health_taxonomy.md`](health_taxonomy.md) before treating a dashboard status as an automation gate.

### 2. Deploy the Cloudflare Worker

```bash
cd cloudflare/codex-audit-proxy
npx -y wrangler@latest secret put CODEX_AUDIT_ORIGIN_URL
npx -y wrangler@latest deploy
```

Use the direct VPS HTTPS origin as `CODEX_AUDIT_ORIGIN_URL`; do not point the Quant Worker at the Pigbibi Worker or another Quant Worker.

Smoke test:

```bash
curl -fsS https://quantstrategylab-codex-audit-proxy.<account-subdomain>.workers.dev/healthz
curl -sS -o /tmp/codex-audit-probe.json -w '%{http_code}\n' \
  -X POST -H 'Content-Type: application/json' --data '{}' \
  https://quantstrategylab-codex-audit-proxy.<account-subdomain>.workers.dev/v1/codex-audit/jobs
```

The unauthenticated submit probe should return `401`. If the request is sent to
the Worker URL, the Worker may reject it before it reaches the origin service.

### 3. Point AIAuditBridge at the Worker

```bash
gh secret set CODEX_AUDIT_SERVICE_URL -R QuantStrategyLab/AIAuditBridge
```

Set the secret value to the Worker base URL, for example:

```text
https://quantstrategylab-codex-audit-proxy.<account-subdomain>.workers.dev
```

Keep `CODEX_AUDIT_SERVICE_AUDIENCE=quant-codex-audit` unless the VPS service audience changes.

### 4. Validate with a manual bridge run

Run a manual `codex_audit.yml` dispatch against a low-risk source issue with provider `codex`. Confirm:

- the submit response creates a job;
- polling reaches `succeeded` or a clear failure;
- the source repository receives only the intended comment or PR;
- no provider keys, origin URL, service token, or job prompt are printed in logs.

## Self-deployment for forks

Forks and third-party open-source users should deploy their own Worker, origin
service, GitHub App credentials, and provider secrets. This repository contains
the orchestration code, but the production service only trusts OIDC claims for
the configured bridge repository/workflow/ref. A fork cannot use the
QuantStrategyLab service unless its repository and workflow ref are deliberately
added to the Quant service allowlists.

## Rollback

If async polling fails, roll back both sides together:

1. Restore the previous bridge client code or redeploy a sync-compatible release.
2. Point `CODEX_AUDIT_SERVICE_URL` back to the previous known-good Worker or origin.
3. Keep the source repository workflows unchanged; they dispatch the bridge and do not know the service transport details.
