# AiGateway Dashboard

Cloudflare Worker that serves an operations dashboard for the AiGateway service.

## Endpoints

- `/` — HTML dashboard with auto-refresh
- `/api/*` — proxies to AiGateway VPS origin (with `DASHBOARD_API_TOKEN` auth)

## Secrets

| Secret | Purpose |
|--------|---------|
| `GITHUB_OAUTH_CLIENT_ID` | GitHub OAuth App client ID |
| `GITHUB_OAUTH_CLIENT_SECRET` | GitHub OAuth App client secret |
| `DASHBOARD_SESSION_SECRET` | Required dedicated HMAC secret for signed dashboard sessions |
| `DASHBOARD_SESSION_KV` | Optional Workers KV binding used to revoke signed sessions on logout |
| `AI_GATEWAY_ORIGIN_URL` | VPS origin URL (e.g. `https://43.156.238.238.sslip.io`) |
| `DASHBOARD_API_TOKEN` | Static token for read-only API access |

`DASHBOARD_API_TOKEN` must match the VPS service `CODEX_AUDIT_SERVICE_TOKEN`
so the dashboard can read `/v1/ai/*` endpoints.
Dashboard sessions are signed cookies, not Worker in-memory state, so they
survive Worker cold starts and edge isolate changes. When `DASHBOARD_SESSION_KV` is bound, logout revokes the session ID server-side. Revocable sessions fail closed if the KV binding is removed. Enabling the KV binding invalidates sessions issued before the binding existed; users should sign in again after rollout. `DASHBOARD_SESSION_SECRET` must be independent from GitHub OAuth and provider API secrets.

## Display semantics

- `health` shows online service health, not monthly audit quality or strategy health.
- `quota` shows remaining Codex window percentage when available; GPT/Claude rows come from Admin Usage/Cost APIs and may omit cost when the provider does not return it.
- Dashboard visibility does not change approval boundaries: high-risk changes, policy changes, secrets, and live-trading decisions still require human review.

## Operational data diagnosis

`/api/diagnoses` and `/api/diagnoses/<job_id>` are session-protected, read-only
projections of AIAuditBridge operational diagnosis ledger records. Only the explicit
job status and up to 500 characters of successful advisory output are shown; no
job polling, prompts, error details, or action endpoints are exposed. The service
must include `QuantStrategyLab/AIAuditBridge` in
`CODEX_AUDIT_SERVICE_DASHBOARD_REPOSITORIES` for this read access.

Submission uses the existing `Codex Audit` workflow's GitHub OIDC identity, never
the dashboard's static token. `AI_OPERATIONAL_DIAGNOSIS_ENABLED=true` enables the
operational job on its existing daily schedule or a manual
`operational_diagnosis=true` dispatch. It reads only the latest VPS cycle, rejects
missing/invalid or over-two-hour-old input, and skips healthy cycles. Incident
fingerprints are stored separately from monitor alert state. An uncertain outcome
is not resubmitted automatically; capacity deferrals may be tried on a later run.
The model remains Codex only, read-only and quota gated. Empty results and healthy
skips do not establish successful diagnosis of a real incident.

## Deploy

```bash
cd cloudflare/ai-gateway-dash
npx wrangler secret put GITHUB_OAUTH_CLIENT_ID
npx wrangler secret put GITHUB_OAUTH_CLIENT_SECRET
npx wrangler secret put AI_GATEWAY_ORIGIN_URL
npx wrangler secret put DASHBOARD_API_TOKEN
npx wrangler secret put DASHBOARD_SESSION_SECRET
npx wrangler deploy
```
