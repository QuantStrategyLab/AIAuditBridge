# P2 notification / independent monitoring replacement — draft only

This PR supplies a deterministic Cloud Run Job image, a generation-bound GCS
notification ledger, independent hosted Scheduler triggers, service probes,
heartbeat observation and an external Cloud Monitoring dead-man. It has no AI
process, SSH, shell evaluation, broker/order/strategy actuator, new dependencies,
IAM resources or secret values. All resources are absent by default; Scheduler
is paused, notification sending and the external alert are disabled.

## Exact relation to #344

Keep #344 draft. Its two removed workflows are **different** operations:
monitor-runtime-digest-once is a frozen paper notification; sync_quant_sentinel_telegram
writes secret versions and restarts a service. The cloud notify job replaces the
former without sudo, ubuntu/systemd, a host directory or a VPS credential cache.
The latter must be retired by an owner-approved separate maintenance process;
this PR must not automatically copy secrets, expand IAM or restart anything.
Full #344 adoption still requires existing approved identity/object/Telegram
route mapping, ledger import and cloud receipt acceptance; source/CI alone does
not unblock its operational cutover.

## Input and ledger contract

Use separate existing notify, monitor, observer and scheduler service accounts.
The monitoring project must differ from all monitored projects. No credential
file is copied: GCS uses short-lived runtime metadata credentials, and only the
notify container receives the existing Telegram Secret Manager reference pinned
to an approved numeric version. The Terraform creates no identities, secret
versions, API enablement or permissions. Missing existing access is a blocker,
not permission to grant more access.

Create approved versioned GCS config/heartbeat objects before cutover. Notify
config: projection_uri, day, expected_target_keys (paper only), state_uri,
chat_id, heartbeat_uri, max_age_seconds (60..172800). Sending refuses
a stale/future projection; historical preview is never a send heartbeat. Preserve the old source commit and projection/day/target
binding during the synthetic comparison. The deployed replacement image must
contain the accepted notification code; the deployment digest is an explicit
owner input. The existing frozen CLI's object/observation/paper checks and
runtime renderer are reused. This does not silently broaden paper to live.

Copy the existing complete quant_monitor_alert_state.v1 ledger into state_uri
through a restricted approved migration client; preserve all deliveries,
pending/unknown/failed/sent, fingerprints and diagnosis state. No token/body/chat
ID is in ledger records, only the existing event/target hashes. GCS object
versioning/retention and restricted approved writers are prerequisites outside
this PR. The job refuses missing/malformed ledger. It CAS-writes pending before
send; concurrent claim conflicts never send. A send timeout or lost completion
write remains unknown/pending and is never retried. Explicit failed delivery
keeps the existing two-attempt maximum. A sent record suppresses replay; a
legacy fingerprint alone is not invented as a sent receipt. After quiescing
all old writers, compare a restricted client's state digest/counts and shadow
previews before accepting send. Never run old and new senders concurrently.

Monitor/observer config: checks [{url}], heartbeats [{uri,max_age_seconds,
expected_mode}], heartbeat_uri. URLs are approved HTTPS endpoints; no bearer
is forwarded to probes, redirects are disabled, and a reachable endpoint is
only endpoint health. Observer detects stale/future/failed heartbeats and
rejects preview as send acceptance. The monitor probes monitored services;
the observer watches monitor/notify heartbeats on a separate job/identity.
The managed absence alert for observer success uses an **existing independent
notification channel**, not this notifier. Validate its channel and result
label with a synthetic failed/missing execution before enabling it.

## Approval, acceptance and rollback

Owner approves projects, cost, existing identity bindings, image digest,
non-secret config object identities, exact original bot/chat route and ledger
migration, secret version reference, schedule/timezone, external alert channel,
deployment and scheduling/send enablement. Changing a signing/auth identity or
missing IAM access is a separate approval; this PR never expands IAM.
Run unit tests and Terraform fmt/validate on hosted PR CI without cloud auth.
Then, only after separate deployment approval, verify preview/no-secret logs,
invalid object/scope refusal, CAS races/crash recovery, original getMe identity
via a protected client, confirmed synthetic delivery/duplicate suppression,
service-down alert, observer-down alert and no live trading/config changes.
Do not declare #344 ready until these are accepted and Secret sync retirement
has an approved alternative.

Rollback: pause new schedules first; inspect execution and unknown outcomes,
retain every ledger generation, and reconcile before returning to an approved
previous sender. Restoring the old timer/sender requires explicit owner approval;
never reset a ledger or auto-replay unknown deliveries. Stopping/deleting VPS
services and removing old runner/cron entries are not performed by this PR.

Sources: [Scheduler job invocation](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule),
[Cloud Run metrics](https://docs.cloud.google.com/monitoring/api/metrics_gcp_p_z).
