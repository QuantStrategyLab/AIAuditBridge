# QuantAIService

[English](README.md) | [简体中文](README.zh-CN.md)

QuantAIService owns its independently packaged, project-scoped task runtime. API tasks and persistent assistant tasks share one contract. Strategy prompts, source validation, execution and adoption stay in the business repositories that own them.

Each repository in this family owns its own runtime and schemas; there is no shared core.

See [runtime ownership and dependency baseline](docs/runtime-baseline.md).

## Status

- Since 2.0.10, worker grants can be scoped to an exact `owner` (including future repositories), or a compatible explicit same-owner `projects` list. Workflow submitters remain single-project, and project lists cannot grant submit permissions.
- Version 2 replaces the retired AIAuditBridge interfaces. Run `quant-ai-service` with a reviewed configuration; `config/quant.example.json` is disabled. No VPS model processes or V1 compatibility endpoints remain.
- Real OAuth registration, Dot/Grok connections, releases, repository renaming and deployment remain deferred. The current package is 2.0.11.

See [upgrade status](docs/upgrade-v2/QUANT.zh-CN.md), [retired entries](docs/upgrade-v2/RETIRED-V1.zh-CN.md) and [identity inventory](docs/upgrade-v2/IDENTITY-INVENTORY.zh-CN.md).

## Offline scenario SDK

`ai_service.scenarios` is an offline contract SDK for `ScenarioTaskSpec/v1` and `ScenarioInstance/v1`, validated with JSON Schema Draft 2020-12. It applies defaults, validates structure and binds a task to its producer; it never dispatches or authorizes work.

- Import functions from `ai_service.scenarios`.
- Package data lives in `src/ai_service/scenarios/*.schema.json`.
- The task schema references only the bundled instance schema URN, so external validators must register both resources; there is no network retrieval.

Long templates require Dot or Grok. API stages are limited to bounded transforms, protocol probes and explicitly small report audits. See [scenario contracts](docs/scenario-contracts.md) for defaults, limits and examples.

The offline compiler remains passive. The opt-in pilot caller in `ai_service.scenarios.pilot` and `ai_service.scenarios.pilot_cli` now supports an approved manual submit plus caller-side readback verification. Repository examples stay `enabled=false` and live rollout is pending. See the [synthetic protocol probe](docs/pilot-synthetic.md).

## Verification

```
python -m pip install .
python -m unittest discover -s tests/task_service -v
```

## Monitor

`ops/quant-monitor` retains report consumption and delivery. Its migrated runtime requires the approved V2 client and QuantPlatformKit package. Existing deployed monitors and dashboard have not been switched.
