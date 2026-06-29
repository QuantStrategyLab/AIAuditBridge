# AiGateway Dashboard

Cloudflare Worker that serves an operations dashboard for the AiGateway service.

## Endpoints

- `/` — HTML dashboard with auto-refresh
- `/api/*` — proxies to AiGateway VPS origin (with `DASHBOARD_API_TOKEN` auth)

## Secrets

| Secret | Purpose |
|--------|---------|
| `AI_GATEWAY_ORIGIN_URL` | VPS origin URL (e.g. `https://43.156.238.238.sslip.io`) |
| `DASHBOARD_API_TOKEN` | Static token for read-only API access |

## Deploy

```bash
cd cloudflare/ai-gateway-dash
npx wrangler secret put AI_GATEWAY_ORIGIN_URL
npx wrangler secret put DASHBOARD_API_TOKEN
npx wrangler deploy
```
