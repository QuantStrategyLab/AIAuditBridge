# Scenario contracts

`ai_service.scenarios` is an offline SDK for the `ScenarioTaskSpec/v1` task contract and the `ScenarioInstance/v1` instance contract. Both are validated with JSON Schema Draft 2020-12. The module applies defaults, validates structure and binds a task to its producer; it never dispatches, schedules or authorizes work.

## Adding the SDK

```python
from ai_service.scenarios import (
    ContractError,
    load_instance,
    compile_task_spec,
    validate_task_spec,
    operation_key,
    load_service_config,
    ServiceConfiguration,
    schema,
)
```

## Package data and validation

- Schemas live in `src/ai_service/scenarios/*.schema.json` and are available from installed wheels.
- The task schema (`urn:ai-service:scenario-task:v1`) references only the bundled instance schema (`urn:ai-service:scenario-instance:v1`) by URN.
- External validators must register both resources. The internal validator builds a registry from just the two installed documents; no other reference can resolve and there is no network retrieval.

## Defaults

`load_instance` copies the value as finite JSON, then applies schema defaults before validation. Notable defaults:

- `enabled`: `false`
- `schedule.kind`: `manual`, `schedule.timezone`: `UTC`, `schedule.catchup_policy`: `record_gap`
- stage `worker_type`: `dot`
- `limits.monthly_budget_usd`: `0`
- API stages get a tighter default `timeout_seconds` of `60`.

`config_revision` is an immutable reference of 40 or 64 lowercase hexadecimal characters.

## Approval references

References record existing approvals; they do not confer authority.

- An enabled instance requires `approvals.activation_ref`.
- `restricted` inputs require `approvals.data_use_ref`.
- A positive stage budget requires `approvals.cost_ref`.

The `data_use_ref` and `cost_ref` checks run when the instance is enabled. A disabled example may keep null refs and a zero budget.

## Templates

| template | allowed phases | worker types | required `inputs.fields` |
| --- | --- | --- | --- |
| `backtest` | `author`, `request`, `review` | `dot`, `grok` | `study_ref`, `data_manifest_ref`, `parameters_ref`, `windows_ref`, `cost_model_ref`, `initial_state_ref` |
| `forward_observation` | `request`, `review` | `dot`, `grok` | `registration_ref`, `calendar_ref`, `sessions_ref`, `initial_state_ref`, `ledger_ref`, `finality_policy_ref` |
| `compliance_watch` | `discover`, `review` | `dot`, `grok` | `targets_ref`, `coverage_ref`, `sources_ref`, `rules_ref`, `window_ref` |
| `report_audit` | `review`, `confirm` | `dot`, `grok`, `api` | `materials_ref`, `checklist_ref` (with `small_review`, see below) |
| `bounded_transform` | `run` | `dot`, `grok`, `api` | `source_ref`, `operation`, `invariants_ref` |
| `investigation` | `analyze`, `confirm` | `dot`, `grok` | `symptoms_ref`, `code_revision`, `read_allowlist_ref`, `checklist_ref` |
| `content_pipeline` | `select`, `group`, `history_match`, `draft` | `dot`, `grok` | `media_manifest_ref`, `history_ref`, `privacy_policy_ref`, `language` |
| `protocol_probe` | `agent` (`dot`/`grok`), `api` (`api`) | `dot`, `grok`, `api` | `fixture_ref`, `assertions_ref`, `cleanup_ref`, `ttl_seconds` |

Five templates require `dot` or `grok`: `backtest`, `forward_observation`, `compliance_watch`, `investigation` and `content_pipeline`. `backtest` and `forward_observation` also require a non-null `executor_ref`. `protocol_probe` accepts only `public` or `synthetic` inputs, and its `ttl_seconds` is bounded to 1..900.

For `report_audit`, `inputs.fields.small_review` is optional for `dot` and `grok` stages, but must be explicitly `true` when any stage uses the `api` worker.

## API worker constraints

API workers are permitted only for `bounded_transform`, `protocol_probe` and `report_audit`. For any API task:

- no tools (`tool_sets` maxItems 0)
- `max_calls`: 1
- `max_active`: 1
- `timeout_seconds` <= 60
- `max_input_tokens` <= 16000
- `max_output_tokens` <= 2500
- `retry_count`: 0

The default `max_output_tokens` is 1024. An API instance with `monthly_budget_usd` 0 can load but cannot compile, because `validate_task_spec` requires a positive budget and at least one approval ref for API tasks. Grok workers use a webhook Routine; there is no polling.

## Versioning and references

Only `template_version` `1` is accepted. Unknown fields in instances and tasks, including fixed template input objects, are rejected; legacy service JSON extensions remain unchanged. References are symbolic IDs: the SDK never imports, executes or fetches them. Domain reference resolution and execution happen later in caller-owned adapters, and these schemas do not run numerical calculations or validate domain evidence.

## Example: disabled report_audit instance

```json
{
  "schema_version": "scenario.instance.v1",
  "id": "daily-audit",
  "project": "example-org/research",
  "template": "report_audit",
  "template_version": "1",
  "enabled": false,
  "config_revision": "0000000000000000000000000000000000000000",
  "inputs": {
    "manifest_ref": "manifest.report.2026-01",
    "data_classification": "sanitized",
    "fields": { "materials_ref": "ref.materials.monthly", "checklist_ref": "ref.checklist.standard" }
  },
  "stages": [
    {
      "phase": "review", "role": "reviewer", "worker_type": "dot",
      "route_ref": "route.review.default", "model_ref": "model.review.default",
      "tool_sets": [],
      "limits": {
        "max_active": 1, "timeout_seconds": 600, "max_request_bytes": 2000000,
        "max_calls": 1, "max_input_tokens": 16000, "max_output_tokens": 1024,
        "max_media_bytes": 0, "monthly_budget_usd": 0, "retry_count": 0
      }
    }
  ],
  "executor_ref": null,
  "gate_refs": [],
  "output": {
    "schema_ref": "schema.report.audit.v1", "evidence_store_ref": "store.evidence.default",
    "public_projection_ref": "projection.report.public", "retention_policy_ref": "retention.default",
    "sink_ref": "sink.reports.default"
  },
  "schedule": { "kind": "manual", "timezone": "UTC", "catchup_policy": "record_gap", "finality_delay_seconds": 0 },
  "approvals": { "activation_ref": null, "data_use_ref": null, "cost_ref": null, "external_action_ref": null }
}
```

The snippet below keeps the disabled instance disabled and compiles a task from a trusted, already approved enabled instance supplied by existing configuration.

```python
from datetime import datetime, timezone

from ai_service.scenarios import compile_task_spec, load_instance


def build_review_task(disabled_instance_json, trusted_enabled_instance_json):
    load_instance(disabled_instance_json)  # The disabled example stays disabled.
    enabled_instance = load_instance(trusted_enabled_instance_json)

    return compile_task_spec(
        enabled_instance,
        occurrence="2026-01-31T00:00:00Z",
        phase="review",
        role="reviewer",
        input_manifest_sha256="0" * 64,
        submitted_at=datetime(2026, 1, 31, tzinfo=timezone.utc),
    )
```

## Operation key and input digest

`operation_key` hashes canonical JSON of `[project, instance_id, config_revision, occurrence, phase, role]` with SHA-256 and the `scn:` prefix. The input digest (`input_manifest_sha256`) is not part of the key: changed input on the same identity must conflict at the submitter. There is no automatic retry; `retry_count` is fixed at 0.

`compile_task_spec` sets `deadline` to `submitted_at` plus the stage `timeout_seconds`.

## Binding a task to its producer

`validate_task_spec(task, instance=trusted_instance)` re-loads the trusted instance, derives the expected binding for the task `phase` and `role`, and binds route, model, inputs, output, limits, template, config revision and approval refs. Offline validation without the trusted instance only checks schema, timezone and operation key; it is not authorization.

## Service configuration compatibility

`load_service_config(mapping_or_path)` returns `ServiceConfiguration(service, instances)`:

- Plain legacy service JSON is copied unchanged and returned with an empty `instances` tuple. No grant, provider or default normalization happens.
- An explicit envelope exactly `{"schema_version": "scenario.service.v1", "service": <old service>, "instances": [...]}` yields a separate, normalized instances tuple.

This loader is opt-in SDK only and is not wired into the server CLI. Do not pass the envelope to `ai-service` or `quant-ai-service --config`; the server CLI still takes legacy service JSON.

## Scope

Not implemented in this module: an endpoint or schedule engine, dispatcher, workflow DSL, auth or grant changes, live API enforcement, budget reservation, V2 envelope integration and result contracts. The current V2 seven-field submission contract is unchanged; compile output is a `TaskSpec`, not a submit-ready V2 envelope. Domain reference resolution and numeric execution stay in caller-owned adapters.
