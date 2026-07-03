# Quant AI Audit Bridge

[Chinese README](README.zh-CN.md)

> Investing involves risk. This project does not provide investment advice and is for education, research, and engineering review only.

## What this repository is

CodexAuditBridge is the QuantStrategyLab AI audit automation bridge. It runs Codex VPS/service-backed audit workflows first, with OpenAI/Anthropic API fallback for approved reviews and low-risk fix pull requests.

It produces research, audit, or orchestration artifacts. It should not submit broker orders or mutate live allocations by itself.

## Architecture boundary

CodexAuditBridge is the organization-local AI audit boundary for QuantStrategyLab. Source repositories dispatch review requests to this repository; they should not embed raw `codex exec` commands, direct provider API calls, model routing, or fallback policy themselves.

Current execution model:

1. A source repository creates or identifies an audit issue.
2. The source repository dispatches this repository's monthly review workflow. The workflow filename is still `codex_audit.yml` for dispatch compatibility, but Codex execution is service-backed.
3. CodexAuditBridge validates the source repository and task mapping, clones the source repository with a scoped GitHub token, and runs the selected provider/backend.
4. Only CodexAuditBridge performs GitHub writes such as comments, branches, commits, pushes, and pull requests.

Keep this boundary inside the `QuantStrategyLab` organization. Do not move QuantStrategyLab audit execution or source-repository write tokens to another organization.

Codex execution is service-only: the workflow calls a QuantStrategyLab-owned HTTPS/443 Codex audit service from a standard GitHub-hosted runner. The service returns review text or structured patch suggestions only. CodexAuditBridge still owns clone, path validation, patch application, commit, push, PR creation, and issue comments.

When `CODEX_AUDIT_AUTO_MERGE=true`, the bridge requests guarded auto-merge by adding the `auto-merge-ok` label to the generated PR only after the changed-file surface is low or medium risk and the file / total changed-line caps stay within policy. The bridge ensures the configured label exists before applying it; if the source token cannot create labels, create the label manually before enabling guarded auto-merge. If a source checkout contains `.github/codex_auto_merge_policy.json`, the bridge reads the baseline policy before Codex edits run, then uses that baseline policy before falling back to its built-in defaults. High-risk, unknown, policy-changing, file-removal/rename/copy, or invalid-policy surfaces are labeled with the configured human-review label (`human-review-required` by default) instead of `auto-merge-ok`, and the source issue comment includes the risk reasons and files for operator review. The bridge does not call GitHub native auto-merge directly; source repositories must keep their own CI and merge-guard workflow in control of the final merge decision.

When a source issue contains a `codex-pr-feedback` marker from a failed CI run or requested-changes review, the bridge treats the run as a bounded retry. If the referenced PR is still open, same-repository, based on the requested source ref, and tied to the same monthly issue branch prefix, the bridge updates that existing PR branch instead of opening another PR. Before clearing any stale guarded auto-merge label on that PR, the bridge reuses the baseline policy labels and skips label mutation when the policy is invalid or the auto-merge and human-review labels are not safe to distinguish.

This avoids hard-coding Codex CLI setup in every source repository and avoids depending on a repository outside the `QuantStrategyLab` organization.

## Compatibility governance role

QuantStrategyLab `AIAuditBridge` is an ops/control-plane consumer only:

- It consumes compatibility governance metadata to align audit/review execution contracts.
- It must **not** participate in trading runtime dependency graphs or strategy/runtime upgrade flows.
- All governance references from this repo should be interpreted as control-plane/tooling compatibility, not runtime coupling.


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

Configure these values in `QuantStrategyLab/AIAuditBridge`:

- Repository secret `CODEX_AUDIT_SERVICE_URL`, for example `https://codex-audit.example.com`.
  Use a secret because the URL may expose origin infrastructure details.
- Optional repository variable `CODEX_AUDIT_SERVICE_AUDIENCE`, default `quant-codex-audit`.
- Required repository variable `CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES`,
  comma or newline separated. Only the listed source repositories may use
  OpenAI/Anthropic fallback.
- Optional repository variable `CODEX_AUDIT_API_FALLBACK_ALLOW_FIX`, default `true`.
  When enabled and `CODEX_AUDIT_MODE=review_and_fix`, OpenAI/Anthropic fallback
  uses the same service patch contract as the Codex backend and can open
  remediation PRs instead of posting review-only comments.
- Optional repository variable `CODEX_AUDIT_API_FALLBACK_PROVIDER_ORDER`, default
  `openai,anthropic`.
- Repository variable `OPENAI_MODEL` for OpenAI API fallback.
- Repository variable `ANTHROPIC_MODEL` for Anthropic API fallback.
- Monthly audits with `CODEX_AUDIT_PROVIDER=auto` fall back to the configured
  API reviewers when the Codex service hits quota/capacity failures.
- PR review workflows fall back to direct API review on recoverable Codex
  service failures.
- Repository variable `CODEX_AUDIT_SERVICE_MODEL` for the VPS Codex service primary
  path; `VPS Codex Service Ops` deploy writes it into the systemd unit.
- Optional repository variable `CODEX_AUDIT_SERVICE_REASONING_EFFORT` for a
  VPS Codex reasoning-effort hard override. Leave it unset or set `auto` to let
  the service choose low/medium/high effort from task complexity.
- Optional service-side model routing variables:
  `AI_GATEWAY_LLM_LOW_COMPLEXITY_MODEL`,
  `AI_GATEWAY_LLM_MEDIUM_COMPLEXITY_MODEL`, and
  `AI_GATEWAY_LLM_HIGH_COMPLEXITY_MODEL`. PR review callers submit
  `task=pr_review` with low/medium/high complexity hints; the VPS service keeps
  Codex auth local and chooses the final Codex model.
- Optional service-side reasoning routing variables:
  `CODEX_AUDIT_SERVICE_<TASK>_<LOW|MEDIUM|HIGH>_REASONING_EFFORT`,
  `CODEX_AUDIT_SERVICE_<LOW|MEDIUM|HIGH>_COMPLEXITY_REASONING_EFFORT`, and
  `AI_GATEWAY_CODEX_<LOW|MEDIUM|HIGH>_COMPLEXITY_REASONING_EFFORT`.
- Optional direct API fallback overrides:
  `CODEX_AUDIT_OPENAI_LOW_COMPLEXITY_MODEL`,
  `CODEX_AUDIT_ANTHROPIC_MEDIUM_COMPLEXITY_MODEL`, and matching
  high/medium/low names.
- Workflow permission `id-token: write` is already set so GitHub Actions can request an OIDC token for the service.

Run the service host with:

```bash
CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=QuantStrategyLab/AIAuditBridge,QuantStrategyLab/CodexAuditBridge \
CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES='QuantStrategyLab/CryptoLivePoolPipelines,QuantStrategyLab/HkEquitySnapshotPipelines,QuantStrategyLab/UsEquitySnapshotPipelines,QuantStrategyLab/ResearchSignalContextPipelines' \
CODEX_AUDIT_SERVICE_AUDIENCE=quant-codex-audit \
CODEX_AUDIT_SERVICE_MODEL=gpt-5.4 \
CODEX_AUDIT_SERVICE_REASONING_EFFORT=auto \
python3 scripts/codex_audit_service.py
```

Terminate TLS on 443 with the platform load balancer or a reverse proxy and forward `/v1/codex-audit` to the service port. Do not pass GitHub write tokens to this service.

The service host should use an authenticated Codex CLI session. It strips
secret-like environment variables, including API keys, before spawning Codex.
It does not inject API keys into the Codex subprocess.

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
