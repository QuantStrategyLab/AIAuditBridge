# Quant AI Audit Bridge

## Publishing an existing SOXL validation result

The `publish_validation` operation in `research_input_readback.yml` uses the
repository secret `AAB_VALIDATION_SYNC_TOKEN` and existing
`QSL_CONTROL_PLANE_SYNC_URL`. The same dedicated value must be bound to the
console's Worker through QuantRuntimeSettings' `runtime-strategy-switch`
environment and deployed before publication. Its authority is limited to the
`aiaudit.soxl_manual_validation` read-only, parked result source; it cannot approve
a candidate or change trading settings. Keep the existing control-plane and
research-task credentials unchanged; never copy a personal token as a substitute.

Use `operation=publish_validation` with the original successful `validation_run_id`.
The workflow validates and republishes that artifact without another model call
or backtest. Missing configuration is not successful delivery; verify the
publication result and the console's original source/run/time before closing it.


## QSL architecture role

- **Layer**: `ops-tooling`.
- **Responsibility**: AI audit and review automation bridge.
- **Owns**: audit prompts, service policy, workflow health terminology.
- **Consumes**: QuantStrategyLab repositories, PR/workflow metadata, Codex/API providers.
- **Must not**: submit broker orders or mutate live allocations.

[Chinese README](README.zh-CN.md)

> Investing involves risk. This project does not provide investment advice and is for education, research, and engineering review only.

## What this repository is

AIAuditBridge is the QuantStrategyLab AI audit automation bridge. It runs Codex VPS/service-backed monthly audit workflows. Direct OpenAI/Anthropic audit fallback is disabled because it bypassed authenticated service budgets; API analysis/review remains available through the budgeted service.

It produces research, audit, or orchestration artifacts. It should not submit broker orders or mutate live allocations by itself.

## Fixed Global ETF candidate review

`scripts/run_global_etf_research_codegen.py` is plan-only by default. Its explicit
execute path is limited to the manual `main` self-hosted workflow, the fixed
`global_etf_research_codegen` task, and the pinned UsEquityStrategies commit.
The technical task key and script filename are retained, but this replaces the
retired local-variable rename case. It reviews the fixed 126-day/15% research
candidate at UES `ceb3e6eb33c7913bcc10bacd6a04fda8aeb1c7ff`. It reads the
Volatility Managed Portfolios abstract from the author's public academic page,
requiring the author, paper title, original PDF link, and unique section bounds.
There is no fallback source, challenge bypass, parameter search, or model patch.

Docker must be available before a source or model call. The committed candidate
and its runner are tested in the existing restricted Docker helper before the
Codex/Luna/medium review. The model receives the source hash, fixed code, and
actual synthetic test status; its structured assessments remain advisory.
`review_completed` proves a returned review and successful synthetic checks,
not correct financial claims, profitability, or out-of-sample validation.

The old `global-etf-review-20260917` claim, result, and response are retained as the
authorization failure record; this explicitly authorized recovery uses
`~/.local/state/aiauditbridge/global-etf-review-20260917-auth-recovery-35124525442`.
The older `global-etf-codegen-20260916` directory is not changed or replayed.
The manual workflow also has an `auth_only` path for its existing OIDC and
audit-service health check. It cannot be combined with `execute`, and it does
not read the research source, create a claim, or call a model.
A claim without a terminal result remains unknown and is never retried
automatically. Only the advisory result is projected to a seven-day GitHub
artifact; source body and raw response stay private on VPS. No deployment,
trading, or financial promotion authority is granted.

## Bounded SOXL research entry

The source-bound new-research entry currently supports only the existing
`soxl_rsi2_mean_reversion` / `us_equity` template. Run it in an isolated
research environment after the approved UES research adapter merge, installing the
approved UES research-adapter merge SHA together with QPK
`de13e486da1bdba60f425e576e944591fc97b809`. The bridge client is installed
from this repository; the adapter and its offline input contract come from UES.

```bash
python3 -m venv .venv-rsi2-research
.venv-rsi2-research/bin/python -m pip install \
  'quant-platform-kit @ git+https://github.com/QuantStrategyLab/QuantPlatformKit.git@de13e486da1bdba60f425e576e944591fc97b809' \
  'us-equity-strategies @ git+https://github.com/QuantStrategyLab/UsEquityStrategies.git@d6b37b77c309e1fb7f25263271b6b0f653f7e7b8' \
  .
.venv-rsi2-research/bin/python -m scripts.run_new_research \
  --request request.json --output result.json
```

`request.json` supplies the validated source receipt, worker manifest, frozen
identity, and offline input paths. The command consumes those frozen inputs,
uses the budgeted Codex-only research route, and persists the research output;
it does not fetch market data, submit orders, or grant promotion authority.
The explicit SOXL codegen route reaches the service-backed Codex entry only
when no caller callback is supplied. The service pins Codex Luna with medium
reasoning, read-only review mode, and no deployment or trading authority; its
task-scoped subprocess uses a temporary working directory, ignores user
configuration, and disables plugins, apps, shell, and web search while
retaining the service's existing authentication owner. The two public source
receipts remain fixed and bounded to 512 KiB (the official Direxion SOXL/SOXS
fact sheet and the fixed AQR research page). Local tests do not call the model.
The CLI parks when formal backtest evidence is absent. A trusted Python caller
may provide UES `SoxlRsi2PromotionBinding`, promotion store, and shadow recorder
keywords to the same entry; arbitrary JSON pass/fail fields are not accepted as
formal evidence. `promotion_shadow_recorder` registers the initial observation;
`read_pending_shadow` only reads it later, while `sync_console` and
`pull_console` deliver it for review and recover the human decision. These are
caller-owned Python callbacks kept separate; the CLI has no trusted material
binding for them and parks without it. No HTTP or JSON schema layer is added.

For the fixed SOXL RSI2 case, `run_fixed_soxl_rsi2_case` is the production
caller. It reads the exact four-member b390 P1 root, verifies the pinned UES
checkout, materializes the optimization window `2022-01-03..2025-01-01` and
promotion window `2023-09-12..2026-09-12` at 753 sessions each, runs the UES
study once, freezes its real winner, and passes that proposal to the existing
QPK cycle with the fixed three-fold plan and 20/20 purge/embargo. Intermediate
input files are removed after the run, while the first fixed-run request is
retained under its `run_root` as `request.json`; later invocations revalidate
that fixed identity and reuse the existing QPK ticket/proposal without
rerunning a completed optimizer stage. Unknown, waiting, or terminal
checkpoints remain parked and do not trigger new optimization. `PerformanceStore`
is local-only and the shadow callback records an explicit no-order pending observation. A
`NO_IMPROVEMENT` or parked result is a valid research outcome and grants no
authority.

The explicit `soxl_rsi2_research_codegen` mode has Docker-only candidate test
and research runners. Focused tests verify the generated Docker commands,
candidate source loading, input/output mounts, and re-entry result reuse; Docker
has not been run in this workspace, so real backtest/research completion is
unverified. No result has been deployed or published, and this mode grants no
authority. Its fixed case wrapper accepts only the verified P1 root, the
approved UES revision `86aa4e03c30eb2fb561748d6e9c22e68d3267cfa`, and a
persistent run root; it materializes only the 2022-01-03 through 2025-01-01
optimization window (753 sessions). The manual watcher dispatch exposes this
case as `run_soxl_codegen`, mutually exclusive with the older controlled case,
and runs it on `ubuntu-latest`; the artifact keeps the sanitized result and
candidate source checkout for review without retaining the input root.

The `strategy_optimization_watcher.yml` workflow keeps its scheduled learning
job unchanged. A one-shot manual run is available only when dispatching with
`run_soxl_rsi2=true`; it uses the same WIF and exact P1 object prefix, uploads
only the sanitized result, and cleans its temporary workspace. It does not
accept candidate, date, root, or permission values from workflow inputs.

For the bounded financial explanation lane, dispatch the same workflow with
`run_soxl_financial_explanation=true` and leave `run_soxl_rsi2` and
`run_soxl_codegen` false and `dry_run=true`. The lane is manual-only, uses the fixed UES/QPK
revisions and P1 manifest, makes no model call, and uploads only the aggregate
`result.json`; raw P1 and materialized files are removed from its `/dev/shm`
workspace. This is a research-only explanation and does not authorize a new
candidate, promotion, or trading action.

Health terms are intentionally split into online service health, organization workflow health, background job health, and artifact/content health. See [`docs/health_taxonomy.md`](docs/health_taxonomy.md) before wiring new dashboard panels or automation gates.
The service also exposes a structured automation triage endpoint for failure diagnosis and release-readiness guidance. It remains advisory and does not bypass merge or deploy controls.

## Architecture boundary

AIAuditBridge is the organization-local AI audit boundary for QuantStrategyLab. Source repositories dispatch monthly audit requests to this repository; they should not embed raw `codex exec` commands, direct provider API calls, model routing, or fallback policy themselves.

Current execution model:

1. A source repository creates or identifies an audit issue.
2. The source repository dispatches this repository's monthly review workflow. The workflow filename is still `codex_audit.yml` for dispatch compatibility, but Codex execution is service-backed.
3. AIAuditBridge validates the source repository and task mapping, clones the source repository with a scoped GitHub token, and runs the selected provider/backend.
4. Only AIAuditBridge performs GitHub writes such as comments, branches, commits, pushes, and pull requests.

Keep this boundary inside the `QuantStrategyLab` organization. Do not move QuantStrategyLab audit execution or source-repository write tokens to another organization.

Codex execution is service-only: the workflow calls a QuantStrategyLab-owned HTTPS/443 Codex audit service from a standard GitHub-hosted runner. The service returns review text or structured patch suggestions only. AIAuditBridge still owns clone, path validation, patch application, commit, push, PR creation, and issue comments.

GitHub PR review has one AI owner: the GitHub Codex App. AIAuditBridge does not run a second PR reviewer or publish a parallel AI-review check. The deterministic `Codex Review Gate`, source CI, unresolved-conversation protection, and branch protection remain independent fail-closed merge controls.

The only scoped exception is the `SchwabTokenAutoRefresher` dependency lane. Its six-hour `dependency_audit.yml` workflow sends eligible Dependabot updates for `otpauth` and `playwright` to the VPS Codex service in read-only review mode. It never checks out or executes PR code; deterministic gates bind the exact base/head, manifests, lockfile, workflow path, and successful CI before the service is called. Ordinary PR review remains owned by the GitHub Codex App.

When `CODEX_AUDIT_AUTO_MERGE=true`, the bridge requests guarded auto-merge by adding the `auto-merge-ok` label to the generated PR only after the changed-file surface is low or medium risk and the file / total changed-line caps stay within policy. The bridge ensures the configured label exists before applying it; if the source token cannot create labels, create the label manually before enabling guarded auto-merge. If a source checkout contains `.github/codex_auto_merge_policy.json`, the bridge reads the baseline policy before Codex edits run, then uses that baseline policy before falling back to its built-in defaults. High-risk, unknown, policy-changing, file-removal/rename/copy, or invalid-policy surfaces are labeled with the configured human-review label (`human-review-required` by default) instead of `auto-merge-ok`, and the source issue comment includes the risk reasons and files for operator review. The bridge does not call GitHub native auto-merge directly; source repositories must keep their own CI and merge-guard workflow in control of the final merge decision.

When a source issue contains a `codex-pr-feedback` marker from a failed CI run or requested-changes review, the bridge treats the run as a bounded retry. If the referenced PR is still open, same-repository, based on the requested source ref, and tied to the same monthly issue branch prefix, the bridge updates that existing PR branch instead of opening another PR. Before clearing any stale guarded auto-merge label on that PR, the bridge reuses the baseline policy labels and skips label mutation when the policy is invalid or the auto-merge and human-review labels are not safe to distinguish.

This avoids hard-coding Codex CLI setup in every source repository and avoids depending on a repository outside the `QuantStrategyLab` organization.

## Compatibility governance role

`QuantStrategyLab/AIAuditBridge` is an ops/control-plane consumer only:

- It consumes compatibility governance metadata to align audit/review execution.
- It must **not** participate in trading runtime dependency graphs or strategy/runtime upgrade flows.
- All governance references here are for control-plane operation and should not be interpreted as runtime coupling.


## Supported source repositories

| Source repository | Allowed task |
| --- | --- |
| `QuantStrategyLab/CryptoLivePoolPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/HkEquitySnapshotPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/ResearchSignalContextPipelines` | `long_horizon_signal_shadow` |
| `QuantStrategyLab/UsEquitySnapshotPipelines` | `monthly_snapshot_audit` |

When adding a new dispatcher, update `SOURCE_REPO_TASKS` in `scripts/run_monthly_codex_audit.py` and add a regression test that proves the repository/task pair is accepted.

## Codex service configuration

AIAuditBridge uses the service backend only. The workflow runs on `ubuntu-latest` and requires a QuantStrategyLab-owned HTTPS/443 Codex audit service.

Configure these values in `QuantStrategyLab/AIAuditBridge`:

- Repository secret `CODEX_AUDIT_SERVICE_URL`, for example `https://codex-audit.example.com`.
  Use a secret because the URL may expose origin infrastructure details.
- Optional repository variable `CODEX_AUDIT_SERVICE_AUDIENCE`, default `quant-codex-audit`.
- Legacy `CODEX_AUDIT_API_FALLBACK_*`, direct API keys/model overrides do not
  enable direct provider calls. Such requests fail before network access;
  use authenticated service analysis/review for API-backed work.
- Monthly audits default to `provider=auto` for `monthly_snapshot_audit` and
  `provider=codex` for `long_horizon_signal_shadow`; override with
  `CODEX_AUDIT_PROVIDER` when you need a specific provider. Workflow dispatch
  uses `task_default` to defer provider selection to the task policy.
- Monthly audits with `CODEX_AUDIT_PROVIDER=auto` do not make direct paid API
  calls after Codex quota/capacity failure; the disabled fallback reports failure.
- Repository variable `CODEX_AUDIT_SERVICE_MODEL` for the VPS Codex service primary
  path; `VPS Codex Service Ops` deploy writes it into the systemd unit.
- Optional repository variable `CODEX_AUDIT_SERVICE_REASONING_EFFORT` for a
  VPS Codex reasoning-effort hard override. Leave it unset or set `auto` to let
  the service choose low/medium/high effort from task complexity.
- Optional service-side model routing variables:
  `AI_GATEWAY_LLM_LOW_COMPLEXITY_MODEL`,
  `AI_GATEWAY_LLM_MEDIUM_COMPLEXITY_MODEL`, and
  `AI_GATEWAY_LLM_HIGH_COMPLEXITY_MODEL`. Audit callers submit task-specific
  low/medium/high complexity hints; the VPS service keeps Codex auth local and
  chooses the final Codex model.
- Optional service-side reasoning routing variables:
  `CODEX_AUDIT_SERVICE_<TASK>_<LOW|MEDIUM|HIGH>_REASONING_EFFORT`,
  `CODEX_AUDIT_SERVICE_<LOW|MEDIUM|HIGH>_COMPLEXITY_REASONING_EFFORT`, and
  `AI_GATEWAY_CODEX_<LOW|MEDIUM|HIGH>_COMPLEXITY_REASONING_EFFORT`.
- Workflow permission `id-token: write` is already set so GitHub Actions can request an OIDC token for the service.

Run the service host with:

```bash
CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=QuantStrategyLab/AIAuditBridge \
CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES='QuantStrategyLab/AIAuditBridge,QuantStrategyLab/CryptoLivePoolPipelines,QuantStrategyLab/HkEquitySnapshotPipelines,QuantStrategyLab/UsEquitySnapshotPipelines,QuantStrategyLab/ResearchSignalContextPipelines' \
CODEX_AUDIT_SERVICE_AUDIENCE=quant-codex-audit \
CODEX_AUDIT_SERVICE_MODEL=gpt-5.4 \
CODEX_AUDIT_SERVICE_REASONING_EFFORT=auto \
CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE=1 \
CODEX_AUDIT_SERVICE_OPENAI_USAGE_WINDOW_DAYS=7 \
CODEX_AUDIT_SERVICE_ANTHROPIC_USAGE_WINDOW_DAYS=7 \
python3 -m service.ai_gateway_service
```

Terminate TLS on 443 with the platform load balancer or a reverse proxy and forward `/v1/codex-audit` to the service port. Do not pass GitHub write tokens to this service.

The service host should use an authenticated Codex CLI session. It strips
secret-like environment variables, including API keys, before spawning Codex.
It does not inject API keys into the Codex subprocess.
When `CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE=1`, the dashboard reads a
sanitized Codex rate-limit snapshot through the local authenticated Codex
app-server; API-key rows remain internal estimates unless a platform usage
provider is configured separately. The internal daily and weekly USD budgets
apply only to API-key and legacy-unclassified usage. The dashboard's Codex
amount is a nominal estimate for observability and never blocks Codex CLI;
the authenticated Codex execution handlers, rather than a caller-supplied
model name, select the Codex-account quota scope. Codex CLI is governed by the
authenticated account's own rate limits. Existing unreadable/corrupt API quota
stores block new API admissions and are preserved for recovery; they are not
reset to an empty ledger. Configured model prices apply to both estimates and
accounting, with daily and weekly limits checked together. These are
single-process estimated-cost controls, not a provider invoice or monthly cap.
Set `OPENAI_ADMIN_KEY` to an OpenAI Admin API key to add a sanitized
GPT/OpenAI completions Usage snapshot to `/v1/ai/quota`. Optional
`CODEX_AUDIT_SERVICE_OPENAI_ADMIN_API_KEY_IDS` can limit that snapshot to
specific API key IDs; never store raw API keys in that filter. OpenAI
organization-wide costs are kept in a separate `organization_costs` field and
are not mixed into the completions usage row.
Set `ANTHROPIC_ADMIN_KEY` to an Anthropic Admin API key to add a sanitized
Claude organization Usage/Cost snapshot. Do not reuse the normal
`ANTHROPIC_API_KEY`; Anthropic usage/cost reporting requires an Admin API key.
Optional `CODEX_AUDIT_SERVICE_ANTHROPIC_ADMIN_API_KEY_IDS` and
`CODEX_AUDIT_SERVICE_ANTHROPIC_ADMIN_WORKSPACE_IDS` filter usage by IDs; costs
are omitted when those filters are set to avoid mixing filtered usage with an
unfiltered cost total. The deploy script stores admin keys in a root-only
`0600` EnvironmentFile, not directly in the systemd unit.

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

AIAuditBridge rejects absolute paths, `.git` paths, secret-like paths, and blocked data paths before writing files locally.

## Output boundary

- Treat generated reports as evidence or review material, not automatic trading instructions.
- Keep source traceability and artifact timestamps visible.
- Research outputs may feed only a separately validated, inactive no-order candidate; P6 live use still requires an explicit owner decision.
- Keep credentials, private data, and external service tokens out of Git and logs.

### Research trigger matrix

| Trigger | Scope | Boundary |
| --- | --- | --- |
| Scheduled watcher | Discovers an already verified event and records an Issue | The same event creates at most one Issue comment; unreadable Issue state writes nothing. A durable attempt parks AI; only a trusted pre-execution quota deferral with a future `retry_at` may unlock one claim at expiry, while unknown, timeout, or comment failures remain parked. |
| SOXL RSI2 codegen | Requires an explicit, non-empty bounded research objective from a manual dispatch | The objective is validated before quota/model admission, authentication, P1 reads, materialization, or research-source reads. |
| General new-strategy design | Not implemented by this watcher | Do not describe the generic design path as implemented or automatically triggered. |

These paths remain research-only and do not grant deployment, trading, promotion, or live authority.

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

- [`docs/architecture.md`](docs/architecture.md): service components and endpoint map.
- [`docs/async_service_deployment.md`](docs/async_service_deployment.md): VPS service and Worker deployment.
- [`docs/health_taxonomy.md`](docs/health_taxonomy.md): dashboard, quota, workflow, job, and artifact health semantics.
- [`docs/bounded_research_diagnosis.md`](docs/bounded_research_diagnosis.md): verified P3 task → one read-only AI diagnosis, with low-frequency escalation boundaries.
- [`docs/ai_autonomy_architecture.md`](docs/ai_autonomy_architecture.md): AI autonomy design review and phased roadmap.

## Community and security

- See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request scope, local verification, and documentation expectations.
- Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for maintainer and contributor conduct.
- Report credential, automation, broker, exchange, or cloud-resource vulnerabilities through [SECURITY.md](SECURITY.md); do not open public issues for secrets or live-execution risk.

## License

See [LICENSE](LICENSE).
