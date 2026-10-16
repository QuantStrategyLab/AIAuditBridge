# VPS Quant Monitor

Scheduled strategy health monitoring and a daily close briefing for a Quant
deployment. The monitor reads strategy artifacts, builds a health dashboard,
detects drift, files issue-only optimization proposals, and sends Telegram
alerts for operational problems.

## Status

Experimental. Use at your own risk. This project is not financial advice and
does not guarantee any trading result. Live trading can lose money; you are
responsible for your own configuration and orders.

## What this is

`ops/quant-monitor` holds the scripts and systemd units that run on a VPS:

- a health cycle every 30 minutes (`codex-quant.timer`) that syncs read-only
  strategy mirrors, validates lifecycle artifacts, builds a strategy health
  dashboard, and runs drift detection;
- a daily close briefing (`codex-daily-briefing.timer`, 22:30 UTC) that builds
  per-domain reports and dispatches them through the AIAuditBridge consumer.

Monitor report consumption lives in the `quant_monitor_domain` package, with
shared watcher logic in QuantPlatformKit. The deployed runtime is not switched
automatically; the legacy setup refuses activation by default until the V2
dependency lock, approved source versions, and cloud environment are qualified.

The monitor is read-only with respect to strategies. It produces evidence,
reports, and issue-only optimization proposals only.

## What this is not

It does not place orders, and it does not change strategy code, live parameters,
position sizes, or risk budgets. Monitoring evidence triggers research review
only. The `canary_eligible` and `pause_request_pending_confirmation` fields in a
health card are evidence states, not broker actions. The current payload policy
mode is `evidence_only`: automation is limited to monitoring, artifact
validation, and issue-only research tasks, and never changes a strategy stage,
canary state, or broker order state.

## Quick start

```bash
export QUANT_MONITOR_ROOT="$PWD"
bash scripts/sync_strategy_repos.sh
bash scripts/health_check.sh
bash scripts/daily_briefing.sh
```

`health_check.sh` first updates the strategy repositories, then selects
`lifecycle-preflight-*` artifacts from the last 7 days produced by scheduled or
manual `preflight_backtests` runs on the `main` branch of the four strategy
repositories. Artifacts are validated for path, file type, domain/profile,
JSON/CSV contract, and size limits before an atomic switch. Code and lifecycle
data are kept in separate roots:

- `PROJECTS_ROOT`: strategy code and `QuantPlatformKit`;
- `QUANT_PROJECTS_ROOT`: read-only return-matrix mirror;
- `LIFECYCLE_LOCAL_ROOT`: backtest, monitor snapshot, and drift state.

When a domain has no trusted artifact, only that domain is blocked and the
reason is written to `data/lifecycle-artifacts/status.json`; the monitor never
falls back to demo or synthetic data. GitHub API rate limits or upstream
failures are recorded in `status.json` as a redacted `reason_code` /
`http_status` / `rate_limit_reset_at`, with `shared_upstream` added when several
domains share one root cause. Health-cycle Telegram alerts collapse upstream
failures and do not present them as strategy or trading anomalies.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `QUANT_MONITOR_ROOT` | Yes | none | This directory. |
| `PROJECTS_ROOT` | Yes | none | Strategy code and `QuantPlatformKit` checkouts. |
| `QUANT_PROJECTS_ROOT` | Yes | none | Read-only return-matrix mirror. |
| `LIFECYCLE_LOCAL_ROOT` | Yes | none | Backtest, monitor snapshot, and drift state. |
| `QUANT_PLATFORM_KIT_ROOT` | No | `$QUANT_MONITOR_ROOT/data/lifecycle-projects/QuantPlatformKit` | Dedicated QuantPlatformKit mirror. |
| `GLOBAL_TELEGRAM_CHAT_ID` | Yes | none | Injected by the systemd unit or environment. Never commit it. |
| `GH_TOKEN` | No | none | Used by `gh` for repository fetch and issue creation. |
| `QSL_GITHUB_REPO` | No | none | Default repository for non-strategy briefing issues; strategy evidence goes to the matching strategy repository. |

### Telegram

The bot token is loaded from a GCP Secret. Set the secret name with
`QUANT_SENTINEL_TELEGRAM_SECRET_NAME` and the readable project with
`QUANT_SENTINEL_GCP_PROJECT`. The systemd unit writes a temporary env file under
`RuntimeDirectory=quant-monitor` through `load_telegram_env.sh`; `health_check.sh`
and `daily_briefing.sh` import it through `source_telegram_env.sh`. Never commit
the token or chat ID.

Scheduled AI consumers attach fixed `failure_stage` and `failure_category`
enumerations to failed results, preserving the original `status`, `reason`, and
exit code. These fields identify the current processing boundary only; they do
not prove a root cause or whether a model request was received. Unknown
exception types are recorded as `unknown_error`. Original exceptions, paths,
accounts, prompts, responses, and credentials are never emitted.

Alert routing:

| Event | Action |
|---|---|
| Lifecycle score or drift degradation | Deduplicated, issue-only AI optimization proposal in the matching strategy repository |
| Data or trusted artifact unavailable | Telegram |
| Circuit breaker or runtime risk | Telegram |
| Optimization-proposal record failure | Telegram |

### Binance live-run sync

Disabled unless configured. With `BINANCE_LIVE_RUNS_SYNC_ENABLED=1` and a
producer-published `BINANCE_LIVE_RUNS_START_AT` (a UTC ISO timestamp), the
synchronizer reads ordinary `workflow_dispatch` strategy runs from the
`main.yml` workflow on the `main` branch and accepts the exact
`binance-live-run-<run_id>-<run_attempt>` artifact containing `lifecycle-run.json`.
A missing or failed run records an unknown barrier for the corresponding UTC day;
GitHub list/API failures mark only the `crypto` domain unavailable and leave the
other domains unchanged. Without these variables the synchronizer does not
access the network.

Health and daily additionally need `BINANCE_LIVE_MONITOR_STREAM_ID`,
`BINANCE_LIVE_MONITOR_REQUIRED_START_AT`, and
`BINANCE_LIVE_MONITOR_REQUIRED_END_AT` to consume a Binance live profile range.
The stream must exactly match the `lifecycle_stream_id` written by the sync, and
both times must be UTC ISO timestamps matching a trusted checkpoint. When a value
is missing or malformed, the stream does not exist, or coverage is incomplete (or
fewer than the existing 10 valid daily returns), only `crypto_live_pool_rotation`
is marked unavailable; other research profiles and domains continue on their
original paths. Health and daily use the same downstream window configuration and
require the QPK `complete_requested_window` API. Without configuration the window
is never guessed and no live-ready result is produced.

### GCS runtime digest input

Disabled by default, and it does not change the 22:30 UTC briefing timer. When
`QUANT_MONITOR_RUNTIME_DIGEST_ENABLED=true` and a valid
`QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX` (ending in `runtime_daily`), an IANA
`QUANT_MONITOR_RUNTIME_TIMEZONE`, and a paper-scoped
`QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY` are set, the pipeline reads the fixed
`{prefix}/longbridge/paper/{day}/handoff.json` pointer for the business date in
that timezone. The pointer must contain exactly `schema_version`,
`business_date`, and `object_uri`, with schema `runtime_daily_handoff.v1`. Its
object URI must stay under the same approved prefix and business-day directory,
with the producer's UTC-observation filename format. The consumer then reads only
that immutable object and reuses the existing projection validation, `--dispatch`,
and `health_cycle.json` ledger. It never lists objects, selects `latest`, or falls
back to an older file; a missing or invalid pointer fails without sending. The
producer's create-only handoff preserves the first pointer for a day, and the
consumer ledger prevents duplicate delivery on same-day reruns. The explicit-object
mode is used only when both `QUANT_MONITOR_RUNTIME_PROJECTION_OBJECT` and
`QUANT_MONITOR_RUNTIME_BUSINESS_DAY` are supplied; they must be paired and
identify an object under the configured prefix and day. Reads use `gcloud storage
cat` with a 20-second timeout, a 1 MiB body limit, and no retry. The local
`--runtime-projection` mode remains available and is mutually exclusive with GCS
input.

### One-shot immutable-object runtime briefing

The `preview-longbridge-paper-once` and `send-longbridge-paper-once` modes are
manual workflow operations. The default remains `inspect`; no new timer or
automatic send is enabled. The business day is supplied as an ordinary dispatch
input. The object URI and complete approved paper target-key set (one key per
line) must come only from the protected environment secrets
`RUNTIME_DIGEST_ONCE_PROJECTION_OBJECT` and `RUNTIME_DIGEST_ONCE_TARGET_KEYS`; do
not put the URI or target keys in ordinary dispatch inputs or public logs.

Preview validates the object, day, and targets and checks the original
notification route. It therefore reads the existing bot secret under the original
execution identity and calls Telegram `getMe`, but it does not send the daily
message. Both modes invoke the fixed published consumer and reuse the original
identity and `health_cycle.json`; that ledger must already exist, be readable, and
resolve to the original path. Never create an empty ledger to replace history.
`unknown`, timeout, a lost return, or a mismatched input, ledger, or route stops
the operation without automatic retry. The result exposes only bounded status and
does not echo the object body or credentials.

## Strategy health snapshot

`health_cycle.py` normalizes the lifecycle dashboard into
`data/health/strategy_health_dashboard.v1.json`. Refresh it directly with:

```bash
bash scripts/refresh_strategy_health.sh
```

The refresh script supports the `quant-lifecycle dashboard` CLI with or without
`--output-dir`; older CLI temporary output is handled only inside the monitor
data directory. With no usable input it outputs `unavailable` and never generates
demo metrics.

Publishing is off by default. Set `STRATEGY_HEALTH_PUBLISH=1`, a dedicated
`STRATEGY_HEALTH_SYNC_URL`, and either `STRATEGY_HEALTH_SYNC_TOKEN` or a
root-owned `STRATEGY_HEALTH_SYNC_TOKEN_FILE`, then run:

```bash
bash scripts/publish_strategy_health.sh
```

The publish script accepts only `strategy_health_dashboard.v1`, does not fall
back to another token, and does not write the token or raw errors to output.

## VPS deployment

```bash
# From a clone of this repository; REVIEWED_MAIN_SHA is the reviewed main commit
AIAUDIT_BRIDGE_SOURCE_SHA="$REVIEWED_MAIN_SHA" bash ops/quant-monitor/scripts/deploy_to_vps.sh
```

On the VPS:

```bash
sudo cp ops/quant-monitor/systemd/codex-quant.service.example /etc/systemd/system/codex-quant.service
# edit the unit: set GLOBAL_TELEGRAM_CHAT_ID, the GCP project, and other values
sudo systemctl daemon-reload && sudo systemctl enable --now codex-quant.service
```

The close briefing and AIAuditBridge dispatch run through
`bash scripts/daily_briefing_pipeline.sh`. In the repository root:

```bash
python3 scripts/consume_daily_briefing.py --runtime-projection projection.json
python3 scripts/consume_daily_briefing.py --runtime-projection projection.json --dispatch
```

The default is a preview; only `--dispatch` sends, reusing existing per-target
delivery and suppressing duplicates for the same target set. `--dry-run` and
`--send-dry-run` do not touch the network and do not write delivery state. Trade
counts in the projection are not yet connected and must not be read as zero
trades. This path is not a cloud-generated report and does not disable the
platforms' own notifications.

### Immutable release install

Production oneshot services read code from `/opt/quant-monitor/releases/<40-hex-SHA>`.
Install a release from an exact local commit without fetching, pulling, changing
systemd units/drop-ins, reloading the daemon, or starting/stopping services:

```bash
# the release root must be writable by the current user
# (override with QUANT_MONITOR_RELEASE_ROOT for testing)
bash ops/quant-monitor/scripts/install_immutable_release.sh \
  --sha <40-hex-commit> \
  --repo /path/to/repo \
  --runtime-data <path-to-runtime-data> \
  --runtime-venv <path-to-runtime-venv>
```

The script validates that the SHA is 40 lowercase hex and exists as a commit in
`--repo`, exports the commit tree with `git archive`, checks required paths, and
atomically moves it under `/opt/quant-monitor/releases/<SHA>` (overridable with
`--release-root` or `QUANT_MONITOR_RELEASE_ROOT`). It symlinks only the release's
`ops/quant-monitor/data` and `.venv` to the provided runtime directories,
copying nothing and deleting no runtime data. An existing target is reused only
when it matches the commit's full archive tree; otherwise it is refused. On
failure the script removes only its own temporary directory.

### Daily-only source adoption

`adopt_daily_briefing_release.sh` links an immutable source release to the
existing approved venv and data, then performs offline import and signature
checks for the candidate consumer/runtime modules and the domain builder's
required QPK modules, and checks candidate source ownership and `gcloud`
availability. It installs no packages, changes no QPK/health/AI configuration,
reads no real ledger or report, calls no cloud API, and sends no notification.
After the checks, it writes only its own
`zzzzzzzzzz-aab-daily-release.conf`, overriding the daily service's
`WorkingDirectory`, `ExecStart`, and two code roots. The original unit's
credentials, routing, timezone, QPK, data, venv, `ExecStartPre`, and other
drop-ins are preserved. It never starts or restarts a service or timer.

```bash
sudo bash \
  ops/quant-monitor/scripts/adopt_daily_briefing_release.sh \
  --sha <40-hex-origin-main-sha> \
  --expected-current-sha <40-hex-current-daily-release-sha> \
  --repo /path/to/repo \
  --runtime-data /path/to/existing/runtime/data \
  --runtime-venv /path/to/existing/runtime/.venv
```

A helper-owned drop-in with the same name is replaced only when its content
matches the current release exactly; an unknown file with that name stops the
helper. Other existing drop-ins are neither rewritten nor deleted. `status=prepared`
means only that the candidate passed the limited offline qualification and that
the release matches the daily unit readback; it does not mean the full pipeline
is healthy, the service runs, or the briefing was delivered. On inconsistent
readback the helper prints `status=unknown`, keeps the previous drop-in snapshot,
and requires manual inspection without automatic rollback or retry.

## QPK pin and dependency lock

`ops/quant-monitor/qpk-runtime.sha` holds the reviewed 40-hex QuantPlatformKit
commit. `setup_vps_runtime.sh` and `sync_strategy_repos.sh` read the pin from that
file and fail rather than falling back to `origin/main` when it is missing, not
40-hex, or unavailable after fetch or checkout. The setup AAB dirty check includes
the pin file. The sync pins only QPK; the other four strategy repositories still
track `origin/main`.

`ops/quant-monitor/requirements-linux-py312.lock` pins the direct and transitive
dependencies the monitor needs on CPython 3.12 / Linux x86_64 / glibc >= 2.34,
with PyPI wheel hashes, and is not a reproducibility proof for the whole VPS.
Before changing the mirror or venv, `setup_vps_runtime.sh` requires the lock to
exist under `QUANT_MONITOR_ROOT` with bytes matching the reviewed source, checks
the interpreter (3.12), OS (Linux), machine (x86_64), and glibc (>= 2.34), then
installs with `python -m pip install --require-hashes --only-binary=:all:`. QPK is
installed from a temporary git archive with `--no-deps --no-build-isolation`.
Set `QUANT_MONITOR_VENV` to prepare a venv at a new absolute path whose parent
already exists and that does not overlap the source, data, existing `.venv`, the
Projects roots, or the QPK mirror; otherwise the venv is created under
`$QUANT_MONITOR_ROOT/.venv`.

## Read-only inspection modes

The manual-only `VPS Codex Service Ops` workflow provides read-only inspection
modes for a running deployment. They do not restart services, run pipelines, call
models, change permissions, or touch accounts, transactions, notifications, or
deployment.

- `inspect-quant-paths` runs `inspect_runtime_paths.py` from an exact-main, clean
  checkout with a clean process environment. It projects the documented root
  variables through fixed path classes, reports fixed provenance categories,
  override flags, selected public dependency SHA256s, and the current QPK pin. It
  never sources environment files or returns their contents or hashes, and it
  reports filesystem identity separately without resolving symlinks.
- `inspect-monitor-failures` runs `inspect_monitor_failures.py --inspect` with
  `env -i` and `/usr/bin/python3 -I -B` against `codex-quant.service`,
  `codex-daily-briefing.service`, and their two timers. Each unit has bounded
  `/usr/bin/systemctl --no-pager show` reads before and after its evidence read.
  Only a service's known nonzero current `InvocationID` permits one matching
  `journalctl` query. Output keeps only fixed enumerations, counters, booleans,
  and normalized times; raw log text, paths, URLs, accounts, tokens,
  InvocationIDs, and exceptions are never emitted.
- `inspect-daily-failure-sample` runs only
  `inspect_monitor_failures.py --inspect-daily-only`, reading only the daily
  service's bounded snapshot and one journal query. It reports classified sample
  counts only when exactly 64 complete JSON records are read and validated.
- `inspect-recorded-daily-errors` runs
  `inspect_monitor_failures.py --inspect-recorded-daily-errors`, binding a
  recorded daily invocation ID and reporting allowlisted error categories and
  exit-code pairs without retaining raw text.

All modes keep the same limits: a five-second deadline per command, a streamed
combined stdout/stderr byte cap (16 KiB for systemd, 64 KiB per service journal),
and at most 64 journal records. Reaching either cap is truncated and unknown.
Missing units or IDs, denied visibility, stderr, malformed or mismatched records,
and changed snapshots cannot produce a diagnosis.

Offline verification uses in-memory metadata runners with guards against
processes, sockets, and external callbacks installed before helper import. No
arguments perform host reads; only `--inspect` does. The built-in
`--fixture-test` exercises the collector without metadata commands:

```bash
python3 -B -m unittest discover -s ops/quant-monitor/tests -p test_inspect_monitor_failures.py -v
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C /usr/bin/python3 -I -B ops/quant-monitor/scripts/inspect_monitor_failures.py --fixture-test
```

## Result receipts

The daily consumer emits one bounded stderr line at its explicit runtime refusal
or domain/runtime return boundary, prefixed `[briefing-result:v1]`. It projects
only fixed `branch`, `stage`, `reason`, `action`, `dispatch_failed`, and
exit-code values. Unknown reasons become `unknown`, and missing or partial
dispatch evidence keeps `dispatch_failed=unknown`. A `false` flag requires the
existing dispatcher's complete, typed result fields; it means the result reports
no failure, not that a message was delivered. No exception text, message body,
path, object URI, date, target key, account identity, or credentials enter this
line.

The pipeline's `[briefing-pipeline-result:v1]` line separately records
`builder_exit`, `consumer_exit` (`not_run` if skipped), and the existing
`domain_exit`. The first nonzero domain result still wins, and with the runtime
switch enabled either branch failing makes the wrapper exit 1. A Telegram-routed
domain result still exits 2 even after a successful send; that is a routing
outcome, not proof of a crash or a delivery failure. These receipts add no calls,
retries, notifications, ledger changes, or diagnostic workflow.

## Delivery state

Telegram delivery reuses `data/alert-state/health_cycle.json`, recording
`pending`, `sent`, `failed`, or `unknown` per event and target hash. Writing must
succeed before a send; the monitor stops rather than sending without state. Only
an explicit success is `sent`; an explicit service rejection is `failed` with at
most one retry; timeouts, lost responses, and interrupted processes leave
`pending` and are treated as `unknown` and not blindly retried. A successful
target is not resent because another target failed. The state file stores no
token, message body, or raw chat ID. `dry_run` and `send_dry_run` do not write
this state. Diagnosis writes, recovery cleanup, and delivery updates share one
file lock for read-modify-write.

## Repository layout

```
ops/quant-monitor/
  scripts/                      runtime scripts
  systemd/                      unit and timer examples
  quant_monitor_domain/         monitor domain logic
  tests/                        tests
  requirements-linux-py312.lock pinned dependencies for CPython 3.12 / Linux x86_64
  qpk-runtime.sha               reviewed QuantPlatformKit pin
```

## Contributing

See the repository `CONTRIBUTING.md`.

## Security

See the repository `SECURITY.md`. Do not open a public issue for a vulnerability.
