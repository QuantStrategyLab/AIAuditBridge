# AI Gateway Architecture

## Overview

The AI Gateway (`AiGateway`) is a lightweight HTTP service that sits between
GitHub Actions workflows and the underlying AI providers (Anthropic Claude,
OpenAI GPT, and Codex CLI). It provides a unified interface for three core
operations: **Analyze**, **Execute**, and **Review**.

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  GitHub Actions │────▶│   AiGateway      │────▶│  Anthropic API  │
│  (OIDC auth)    │     │  (HTTP server)   │     │  OpenAI API     │
│                 │     │                  │     │  Codex CLI      │
└─────────────────┘     │  ┌────────────┐  │     └─────────────────┘
                        │  │ Auth       │  │
                        │  │ (GitHub    │  │
                        │  │  OIDC JWT) │  │
                        │  ├────────────┤  │
                        │  │ Quota      │  │
                        │  │ (per-repo  │  │
                        │  │  budget)   │  │
                        │  ├────────────┤  │
                        │  │ Health     │  │
                        │  │ (latency,  │  │
                        │  │  errors)   │  │
                        │  ├────────────┤  │
                        │  │ Audit Log  │  │
                        │  │ (JSON to   │  │
                        │  │  stderr)   │  │
                        │  └────────────┘  │
                        └──────────────────┘
```

## Components

### Service Layer (`service/`)

| Module | Responsibility |
|---|---|
| `ai_gateway_service.py` | HTTP request routing, rate limiting, auth, CORS |
| `contracts.py` | Request/response schemas (Analyze, Execute, Review) |
| `health.py` | Endpoint metrics, error rates, latency tracking |
| `quota.py` | Per-repo budget enforcement, model cost estimation |
| `autonomy.py` | Confidence scoring, change risk classification |
| `feedback.py` | Closed-loop change tracking and evaluation |

### Adapters (`service/adapters/`)

| Adapter | Provider |
|---|---|
| `llm_adapter.py` | Anthropic Claude, OpenAI GPT — text completion |
| `codex_adapter.py` | Codex CLI — sandboxed code execution |

### Auth (`service/auth/`)

| Module | Mechanism |
|---|---|
| `github_oidc.py` | GitHub Actions OIDC JWT verification |

## Endpoints

| Path | Method | Description | Rate Limited |
|---|---|---|---|
| `/v1/ai/analyze` | POST | Single LLM completion via LlmAdapter | Yes (30/min) |
| `/v1/ai/execute/jobs` | POST | Async Codex execution | No |
| `/v1/ai/execute/jobs/{id}` | GET | Poll async job status | No |
| `/v1/ai/execute` | POST | Sync Codex execution (legacy) | No |
| `/v1/ai/review` | POST | Multi-model parallel review | Yes (30/min) |
| `/v1/ai/health` | GET | Detailed health snapshot | No |
| `/healthz` | GET | Liveness check | No |
| `/v1/ai/quota` | GET | Per-repo quota status | No |
| `/v1/ai/feedback/*` | GET/POST | Change tracking and evaluation | No |

Backward-compatible aliases:
- `/v1/codex-audit` → `/v1/ai/execute`
- `/v1/codex-audit/jobs` → `/v1/ai/execute/jobs`
- `/v1/codex-audit/jobs/{id}` → `/v1/ai/execute/jobs/{id}`

## Data Flow

### Analyze (sync)
```
POST /v1/ai/analyze
  → authenticate (OIDC JWT)
  → rate limit check (sliding window)
  → quota check (per-repo daily budget)
  → LlmAdapter.complete()
  → record quota usage
  → record health metrics
  → return {output, model, latency}
```

### Execute (async)
```
POST /v1/ai/execute/jobs
  → authenticate
  → validate source_repository (allowlist + org match)
  → quota check
  → resolve Codex reasoning effort from override or task complexity
  → submit job (file-based store)
  → background thread: CodexAdapter.execute()
  → poll via GET /v1/ai/execute/jobs/{id}
```

### Review (sync)
```
POST /v1/ai/review
  → authenticate
  → rate limit check
  → parallel LLM reviews (Claude + GPT)
  → optional Codex verification
  → extract confidence scores
  → compute consensus + recommended action
  → return {results, consensus, recommended_action}
```

## Security

- **Authentication**: GitHub Actions OIDC JWT with allowlisted repos, workflows,
  refs, and repository visibilities. Static token fallback with minimum 32-char
  requirement.
- **Authorization**: Source repository org must match OIDC claims repository org
  (prevents cross-org escalation).
- **Sandbox**: Codex sandbox restricted to service-side allowlist (default: `read-only`).
- **Codex reasoning effort**: `CODEX_AUDIT_SERVICE_REASONING_EFFORT` can hard
  override CLI effort; unset/`auto` routes low/medium/high by task complexity.
- **Rate Limiting**: Sliding window of 30 requests per 60 seconds for sync endpoints.
- **Input Validation**: Payload size capped at 2 MB. Prompt must be non-empty.
  Task/mode must be valid constants. Reviewers must be supported.
- **Audit Logging**: Structured JSON lines to stderr for every request and critical event.

## Deployment

The service runs as a standalone HTTP server (stdlib `ThreadingHTTPServer`)
on port 8797 by default. It is deployed behind nginx on a VPS with:

- OIDC authentication for GitHub Actions callers
- Static token fallback for local testing
- File-based async job store (TTL-based cleanup)
- Health check endpoint for monitoring
