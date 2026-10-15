# Explicit project lists for one worker connection

Version 2.0.8 adds `projects` to a worker grant. It is an explicit list of admitted
repositories belonging to one GitHub owner. Wildcards and automatic repository
discovery are not supported. Adding a project requires configuration changes,
not a new OAuth connection or a business-specific service branch.

```json
{
  "issuer": "https://identity.example/realms/example",
  "subject": "CONFIGURE_VERIFIED_SUBJECT",
  "identity": "org-dot-worker",
  "projects": ["example-org/research", "example-org/support"],
  "provider": "dot",
  "scopes": ["tasks:read", "tasks:work", "events:subscribe"],
  "claims": {"azp": "CONFIGURE_VERIFIED_CLIENT_ID"}
}
```

Use either the legacy `project` string or `projects`, never both. Every list entry
must have its own `config.projects` policy, including provider routes, models,
quotas and deadlines. The list must be nonempty, duplicate-free and contain exact
`owner/repository` names. The issuer, subject and signed `azp` claim still identify
one connection. Exactly one grant must match each token. Copying a grant once per
project with the same matching claims remains ambiguous and is rejected.

Project lists are restricted to provider-bound workers with `tasks:work` and only
`tasks:read`, `tasks:work` and optionally `events:subscribe`. They cannot include
`tasks:submit` or use GitHub Actions workload identity. Effective token scopes are
still the intersection with grant scopes. Workflow submitter grants keep one
exact repository/workflow/ref and are unchanged.

For a connection with multiple allowed projects, every MCP task or subscription
management tool requires an explicit `project` argument. `tools/list` advertises
the admitted values. For example:

```json
{"name":"claim_task","arguments":{"project":"example-org/research","task_id":"TASK_ID","attempt_id":"STABLE_ATTEMPT_ID"}}
```

REST calls select the project with `?project=example-org%2Fresearch`. Omission,
blank values, duplicate selectors and unauthorized projects fail. A task is still
looked up by both project and task ID; its provider and execution attempt remain
enforced. Legacy single-project connections keep their existing tool schemas and
default project, including a `projects` list containing just one entry.

Events keep one exact `arguments.project` per subscription. One application may
own several project subscriptions; it is not one wildcard subscription. Delivery
rechecks the current list and provider. Subscription management checks both
identity and selected project. Existing TTL, signing, callback verification and
delivery limits are unchanged.

Grok workers may use the same list with a distinct signed `azp`, `provider: grok`
and `tasks:read tasks:work`. A Grok routine webhook is only a wake-up trigger; its
key does not authenticate calls to the task service. The worker uses its own OAuth
connection to read, claim and report. There is no pending-task listing API for
timer-based polling in this release.

Changing an existing worker from one project to several changes its tool schema;
rescan that existing host connection after rollout and verify the new `project`
argument before sending work. This source change does not alter production
configuration, register OAuth clients or create subscriptions. Deployments that
wrap this runtime must install the matching 2.0.8 wheel before using lists.

During an identity cutover, retain the old worker identity until all tasks it has
claimed reach a terminal state. Another identity cannot report for its attempt.
Events do not replay history, so keep the old delivery route until the new route
is active and reconcile existing nonterminal task IDs explicitly. Removing the
old grant or subscription first can strand work; changing `worker`/`attempt`
directly in the database is not a supported handoff.

## Owner-wide worker scope (2.0.9)

Use `owner: "example-org"` instead of `projects` to cover every repository of one exact owner, including future repositories. Exactly one of `project`, `projects` and `owner` is required. Wildcards and cross-owner lists are rejected. Set service-level `owner` to validate every project policy and grant against that boundary.

Owner/list grants require a signed client `azp`, a provider and `tasks:work`; they allow only read/work and optional events. Workload identities and submit scopes are rejected. Token scopes remain intersected with grants. A new repository needs its own policy and independently admitted submitter, but no worker grant edit. Select an exact project for each request; event subscriptions remain exact-project. Grok uses webhook Routine wakeups, with no polling.

Safe logging from #31 remains enabled; owner scopes and repository lists are not logged. This change does not edit production grants, activate providers or deploy a release.


## Quant service configuration

Set the top-level `owner` explicitly for this deployment; there is no compiled-in organization name. Every admitted project and worker owner must match it. Legacy production configurations need this field before running 2.0.9. Validate a private candidate with `quant-ai-service --check-config --config PATH` before adoption. The runtime is owned by this repository and has no dependency on PersonalAIService.
