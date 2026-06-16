# CodexAuditBridge

[Chinese README](README.zh-CN.md)

> Investing involves risk. This project does not provide investment advice and is for education, research, and engineering review only.

## What this repository is

CodexAuditBridge is a QuantStrategyLab audit automation bridge. It runs service-backed Codex audit workflows for snapshot reviews and low-risk fix pull requests.

It produces research, audit, or orchestration artifacts. It should not submit broker orders or mutate live allocations by itself.

## Architecture boundary

CodexAuditBridge is the organization-local Codex boundary for QuantStrategyLab. Source repositories dispatch review requests to this repository; they should not embed raw `codex exec` commands or depend on a specific Codex runner themselves.

Current execution model:

1. A source repository creates or identifies an audit issue.
2. The source repository dispatches this repository's monthly review workflow. The workflow filename is still `codex_audit.yml` for dispatch compatibility, but Codex execution is service-backed.
3. CodexAuditBridge validates the source repository and task mapping, clones the source repository with a scoped GitHub token, and runs the selected provider/backend.
4. Only CodexAuditBridge performs GitHub writes such as comments, branches, commits, pushes, and pull requests.

Keep this boundary inside the `QuantStrategyLab` organization. Do not move QuantStrategyLab audit execution or source-repository write tokens to another organization.

Codex execution is service-only: the workflow calls a QuantStrategyLab-owned HTTPS/443 Codex audit service from a standard GitHub-hosted runner. The service returns review text or structured patch suggestions only. CodexAuditBridge still owns clone, path validation, patch application, commit, push, PR creation, and issue comments.

This avoids hard-coding Codex CLI setup in every source repository and avoids depending on a repository outside the `QuantStrategyLab` organization.

## Supported source repositories

| Source repository | Allowed task |
| --- | --- |
| `QuantStrategyLab/CryptoLivePoolPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/HkEquitySnapshotPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/ResearchSignalContextPipelines` | `long_horizon_signal_shadow` |
| `QuantStrategyLab/UsEquitySnapshotPipelines` | `monthly_snapshot_audit` |

When adding a new dispatcher, update `SOURCE_REPO_TASKS` in `scripts/run_monthly_codex_audit.py` and add a regression test that proves the repository/task pair is accepted.

## Codex service configuration

CodexAuditBridge uses the service backend only. The workflow runs on `ubuntu-latest` and requires a QuantStrategyLab-owned HTTPS/443 Codex audit service.

Configure these values in `QuantStrategyLab/CodexAuditBridge`:

- Repository secret `CODEX_AUDIT_SERVICE_URL`, for example `https://codex-audit.example.com`.
  Use a secret because the URL may expose origin infrastructure details.
- Optional repository variable `CODEX_AUDIT_SERVICE_AUDIENCE`, default `quant-codex-audit`.
- Workflow permission `id-token: write` is already set so GitHub Actions can request an OIDC token for the service.

Run the service host with:

```bash
CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=QuantStrategyLab/CodexAuditBridge \
CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES='QuantStrategyLab/CryptoLivePoolPipelines,QuantStrategyLab/HkEquitySnapshotPipelines,QuantStrategyLab/UsEquitySnapshotPipelines,QuantStrategyLab/ResearchSignalContextPipelines' \
CODEX_AUDIT_SERVICE_AUDIENCE=quant-codex-audit \
python3 scripts/codex_audit_service.py
```

Terminate TLS on 443 with the platform load balancer or a reverse proxy and forward `/v1/codex-audit` to the service port. Do not pass GitHub write tokens to this service.

The service host should use an authenticated Codex CLI session. It strips
secret-like environment variables, including API keys, before spawning Codex.
Set `CODEX_AUDIT_SERVICE_ALLOW_OPENAI_API_KEY_FALLBACK=true` only for an
explicit, temporary fallback that passes `CODEX_AUDIT_SERVICE_OPENAI_API_KEY` or
`OPENAI_API_KEY` to the Codex subprocess.

If no custom domain is available, `cloudflare/codex-audit-proxy/` contains a minimal Cloudflare Worker that can publish a free `workers.dev` HTTPS entry point while keeping the VPS origin URL in a Cloudflare secret. The production service path is async: submit `POST /v1/codex-audit/jobs`, then poll `GET /v1/codex-audit/jobs/{job_id}`. See `docs/async_service_deployment.md` for the deployment and open-source repository checklist.

The manual `VPS Codex Service Ops` workflow can be used by maintainers to inspect or deploy the VPS-side service through the existing `self-hosted,codex-vps` runner. The deployment keeps the Pigbibi `/v1/codex` gateway unchanged and adds an nginx route for `/v1/codex-audit` to this repository's audit service.

### Service patch contract

In `review_and_fix` mode, the service must return exactly one JSON object:

```json
{
  "final_message": "Markdown summary for the issue comment or PR body.",
  "changes": [
    {
      "path": "relative/file/path.py",
      "content": "complete UTF-8 file contents"
    }
  ]
}
```

CodexAuditBridge rejects absolute paths, `.git` paths, secret-like paths, and blocked data paths before writing files locally.

## Output boundary

- Treat generated reports as evidence or review material, not automatic trading instructions.
- Keep source traceability and artifact timestamps visible.
- Require human review before using outputs in downstream strategy or platform changes.
- Keep credentials, private data, and external service tokens out of Git and logs.

## Repository layout

- `tests/`: unit, contract, and regression tests.
- `.github/workflows/`: CI, scheduled jobs, release, or deployment workflows.
- `scripts/`: operator scripts and local helpers.

## Quick start

Review `.github/workflows/`, `scripts/run_monthly_codex_audit.py`, and the README files before running automation.

```bash
git status --short
python3 -m unittest discover -s tests -v
```

## Useful docs

- No separate `docs/` directory yet; start with this README, `README.zh-CN.md`, and the workflow files.

## Community and security

- See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request scope, local verification, and documentation expectations.
- Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for maintainer and contributor conduct.
- Report credential, automation, broker, exchange, or cloud-resource vulnerabilities through [SECURITY.md](SECURITY.md); do not open public issues for secrets or live-execution risk.

## License

See [LICENSE](LICENSE).
