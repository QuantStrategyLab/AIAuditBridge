# Async Codex Audit Service Deployment

CodexAuditBridge uses an async service contract to avoid keeping a GitHub Actions request open while Codex runs on the VPS.

## Architecture

1. A source repository creates or updates an audit issue, then dispatches `QuantStrategyLab/CodexAuditBridge`.
2. CodexAuditBridge clones the source repository with a scoped GitHub App token, builds the audit prompt, and requests a GitHub Actions OIDC token with audience `quant-codex-audit`.
3. CodexAuditBridge submits `POST /v1/codex-audit/jobs` through the Cloudflare Worker.
4. The Worker forwards only Quant audit routes to the VPS origin. The VPS service validates OIDC, repository allowlists, source repository allowlists, and payload size.
5. The VPS service returns a random `job_id`, runs Codex in a background thread, and persists job state in a private local directory.
6. CodexAuditBridge polls `GET /v1/codex-audit/jobs/{job_id}` until the job succeeds, fails, or times out.

The synchronous `POST /v1/codex-audit` endpoint remains available for local diagnostics, but production workflows should use the async job endpoints.

## Boundary with Pigbibi CodexGateway

`CodexAuditBridge` intentionally stays separate from `Pigbibi/CodexGateway`.

- `CodexGateway` is a generic Codex invocation facade for prompt/context/image/schema calls.
- `CodexAuditBridge` owns QuantStrategyLab monthly audit semantics: source issue context, bounded repository snapshots, service patch contracts, source repository allowlists, GitHub App writeback, and generated remediation PRs.
- Do not route Quant monthly audits through the Pigbibi gateway Worker or Pigbibi repository allowlist.
- Do not move audit-specific issue/PR behavior into `CodexGateway`; share only low-level primitives after the HTTP contracts are stable.

The historical self-hosted direct-Codex workflows in `SelfHostedCodexAuditBridge` and `CryptoCodexAuditBridge` should be treated as compatibility fallback. The preferred production path is the GitHub-hosted `CodexAuditBridge` workflow plus async VPS service. After parity is verified for current monthly sources, the self-hosted direct-Codex workflows can be disabled or deleted.

## Permission and secret boundary

- Source repositories, including public QuantStrategyLab repositories, must not store provider keys or the Codex service URL.
- Source repositories should only dispatch the bridge workflow and provide issue/source context. Avoid running Codex directly in public workflows.
- `QuantStrategyLab/CodexAuditBridge` is public, so it may contain only client/orchestration code. Its service URL, provider fallback keys, GitHub App private key, and Cloudflare origin stay in GitHub or Cloudflare secrets.
- The VPS service should allow only `QuantStrategyLab/CodexAuditBridge` in `CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES`; this OIDC allowlist is required because the bridge repository is public.
- The VPS service should keep `CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES` limited to the current source repositories.
- The Cloudflare Worker stores only `CODEX_AUDIT_ORIGIN_URL` as a Worker secret. Do not commit the origin URL if it exposes infrastructure details.
- Job IDs are random and status reads still require service authentication. Job responses never include the original prompt.

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
CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=QuantStrategyLab/CodexAuditBridge \
CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES='QuantStrategyLab/CryptoLivePoolPipelines,QuantStrategyLab/HkEquitySnapshotPipelines,QuantStrategyLab/UsEquitySnapshotPipelines,QuantStrategyLab/ResearchSignalContextPipelines' \
CODEX_AUDIT_SERVICE_AUDIENCE=quant-codex-audit \
CODEX_AUDIT_SERVICE_JOB_DIR=/var/lib/codex-audit-bridge/jobs \
bash scripts/deploy_codex_audit_service.sh deploy
```

The job directory should be owned by the service user and mode `0700`.

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

The unauthenticated submit probe should return `401` from the origin service.

### 3. Point CodexAuditBridge at the Worker

```bash
gh secret set CODEX_AUDIT_SERVICE_URL -R QuantStrategyLab/CodexAuditBridge
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
- no provider keys, origin URL, or job prompt are printed in logs.

## Rollback

If async polling fails, roll back both sides together:

1. Restore the previous bridge client code or redeploy a sync-compatible release.
2. Point `CODEX_AUDIT_SERVICE_URL` back to the previous known-good Worker or origin.
3. Keep the source repository workflows unchanged; they dispatch the bridge and do not know the service transport details.
