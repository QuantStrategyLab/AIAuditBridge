# Quant VPS deployment

This path controls `quant-ai.service` only. It does not call another service's
deploy helper, copy configuration, synchronize Secrets, change grants, run
strategy jobs, or install packages from the network on the server. All examples
use public placeholders. The existing MIT license covers these files. Bundle
extraction, offline installation, health probes and receipts are adapted from an
existing service deploy design; Quant has its own helper, settings, key and
concurrency.

## Build and review

Normal CI builds a Python 3.12 / manylinux2014 x86_64 wheelhouse and uploads
`quant-vps-bundle` with `bundle.tar.gz`, `release.json`, `bundle.sha256` and
`SHA256SUMS`. PR CI can build review artifacts, but deployment accepts only a
successful **push CI on the default branch** at the exact source SHA. Artifacts
expire after 30 days; missing/expired artifacts require a new reviewed build and
digest. No `latest`, implicit branch resolution or rebuild during deployment.
A CI artifact is not a published release or evidence of functional parity.

For review, download the exact run with
`gh run download RUN_ID -n quant-vps-bundle -D candidate`. Independently record
the source SHA, release ID, run ID, bundle digest and wheel digest. The outer
bundle digest is an approval input; the server also checks full manifest coverage
and hash-pinned requirements, rejects unsafe tar members, duplicate packages,
another service's wheels, and version mismatches, and installs with `--no-index` and
`--require-hashes`. Resolved dependencies are pinned inside that immutable bundle;
a new CI build can resolve different compatible dependencies and needs review.

## Workflow inputs

`.github/workflows/deploy-vps.yml` is manual only. It never runs on push or PR.

| Input | Meaning |
| --- | --- |
| `action` | `status` (default), `deploy`, or `rollback` |
| `ref` | Full reviewed source SHA; for rollback use the pinned baseline SHA; for status an audit SHA |
| `dry_run` | `true` by default: validate request/artifact only, no SSH or SSH credential access |
| `artifact_run_id` | Exact successful default-branch CI run; required for deploy |
| `bundle_sha256` | Independently approved 64-character SHA256; required for deploy |
| `rollback_ref` | Full baseline source SHA, checked against server status before deploy and by the helper on rollback |

Dry-run status/rollback validates inputs only, and does **not** claim server
health. Non-dry status uses the restricted SSH command and HTTP checks on the
server; it does not restart the service. All non-dry actions must be dispatched
from the default branch. Use the `quant-vps` environment with required reviewers
and default-branch protection configured by the repository owner. The workflow
does not create settings, credentials or permission rules.

Environment variables: `QUANT_VPS_HOST`, `QUANT_VPS_USER`, `QUANT_VPS_PORT`
(default `22`), `QUANT_VPS_KNOWN_HOSTS` (verified host-key lines), and
`QUANT_PUBLIC_HOST` (HTTPS origin). Environment secret: `QUANT_VPS_SSH_KEY`.
Use a dedicated Quant-only key/account, never another service's deploy identity. No
Secret sync input exists. Host-key checking is strict; no host-key discovery is
performed. Environment setup is a separate approved operator task.

## Server prerequisites (separate operator approval)

This deploy path does not install the helper or change a server. Before first use an
operator must inspect the current unit/drop-ins and running release, archive
exact originals privately on the server, prove rollback compatibility on a copy
of the DB, and install the reviewed `quant_ai_deploy.py` as the root-owned,
non-writable `/usr/local/sbin/quant-ai-deploy`. Do not revive a historical monitor
installer. This is the task service, not `ops/quant-monitor`.

Restrict the dedicated key to
`restrict,command="sudo -n /usr/local/sbin/quant-ai-deploy"` with no shell,
forwarding or alternate command. Any sudoers entry must preserve only
`SSH_ORIGINAL_COMMAND` for this exact root-owned helper; validate it with
`visudo -cf`. Review the actual account and existing SSH settings before adding
this rule; do not overwrite another account's authorized keys. This repository
does not provision users, SSH keys, sudoers, IAM or Secrets automatically.

Create `/etc/ai-services/quant-deploy.json` as a root-owned regular file, mode
`0600`, under root-owned parents. All writable release, unit and lock parents
must be inaccessible to the deploy/service accounts. Required example settings
(replace every placeholder; no production baseline is embedded):

```json
{
  "releases_dir": "/opt/ai-services/releases",
  "config_path": "/etc/ai-services/quant.json",
  "db_path": "/var/lib/quant-ai/tasks.sqlite",
  "dropin_path": "/etc/systemd/system/quant-ai.service.d/50-approved-runtime.conf",
  "lock_path": "/run/lock/quant-ai-deploy.lock",
  "python": "/usr/bin/python3.12",
  "port": 8091,
  "baseline_release": "YYYYMMDD-quant-vVERSION-SHORTSHA",
  "baseline_source_sha": "FULL_BASELINE_SOURCE_SHA",
  "config_sha256": "APPROVED_COMPATIBLE_CONFIG_SHA256"
}
```

The helper requires Python 3.10+; the configured installer must be Python 3.12
with venv support on Linux x86_64. The baseline directory must already contain
its complete original venv, including any old shared package dependencies. The
runtime drop-in must exist. Confirm its exact bytes equal the helper's rendering
for the approved baseline and the existing `User=quant-ai`, port, config, working
directory and other drop-ins are correct. Rollback renders that same baseline
ExecStart; it cannot reconstruct arbitrary unit edits. Preserve originals and
prove that equality before enabling this helper.

### Initial configuration and rollback compatibility

The new Quant wrapper requires an exact top-level `owner`; an old deployment
may omit it. The workflow deliberately does not migrate configuration. Under a
separate explicit config approval, prepare a server-local candidate that adds
only the configured owner, preserving all single-project worker selectors,
submitters, claims, providers and policies. Validate both new and baseline
runtime compatibility **before** installation, privately preserve original
bytes/hash, then pin the approved candidate hash in `quant-deploy.json`.
Do not put owner-wide worker grants on the old runtime. If either runtime rejects
the candidate or DB compatibility is unknown, stop: this deploy path is not yet
ready. Owner-wide grant migration follows a verified upgrade as a separate change.
After any later grant/config change this helper intentionally refuses rollback
until an approved compatible config and its pinned hash have been restored.

## Effects and failure handling

Pause all submitters and subscription renewal for the maintenance window. A
pre/post-stop read-only DB gate rejects nonterminal tasks (including unknown and
cancel-requested attempts), every active subscription flag and pending delivery,
including expired subscriptions and orphan deliveries. Missing tables or read
failure stop the operation. The zero-count check does not prevent new traffic;
operator quiescence remains required. Never clear the DB to pass the check.

Deploy verifies the bundle, reserves a fresh immutable release, creates its venv
offline, validates config as `quant-ai`, backs up the runtime drop-in, drains,
stops, rechecks config/drain, replaces only the runtime drop-in, reloads systemd
and restarts only Quant. It preserves the DB and service config. Failed staged
releases/backups remain for diagnosis; IDs are never reused. A post-stop drain
failure resumes the old unchanged runtime only if the config digest still
matches. Unknown state or digest drift is not overwritten.

Local health requires the expected unit ExecStart/running state, discovery 200
and anonymous MCP 401. Failure attempts the pinned baseline after another
stop/config/drain gate. Public HTTPS checks use the same 200/401 boundary and
trigger explicit pinned rollback on failure. A failed deployment remains failed
even when rollback succeeds. Explicit rollback checks the full approved baseline
SHA, drains twice, and activates only that existing release; it does not install
or fetch an old bundle. Inspect receipts before retrying any uncertain outcome.

Receipts identify `quant-ai.service`, actual release/version, healthy state and
rollback outcome; they contain no config values, subprocess output or tokens.
HTTP health does not establish OAuth, tool schema, native subscriptions or a
completed task. Those require independent Quant Dot acceptance after upgrade.

GitHub reference: [artifact download](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)
and [environment protection](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments).
