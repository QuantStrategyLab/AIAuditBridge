# QuantAIService

QuantAIService 2.0 provides Quant project admission policies for the shared `personal-ai-service` task runtime. API tasks and persistent assistant tasks use one project-scoped contract. Strategy prompts, source validation, execution and adoption stay in business repositories.

The source is a local, unpublished upgrade of AIAuditBridge. Run `quant-ai-service` with a reviewed configuration; `config/quant.example.json` is disabled. No VPS model processes or V1 compatibility endpoints remain.

See [upgrade status](docs/upgrade-v2/QUANT.zh-CN.md), [retired entries](docs/upgrade-v2/RETIRED-V1.zh-CN.md) and [identity inventory](docs/upgrade-v2/IDENTITY-INVENTORY.zh-CN.md). Real OAuth registration, Dot/Grok connections, releases, repository renaming and deployment remain deferred.

`ops/quant-monitor` retains report consumption and delivery. Its migrated runtime requires the approved V2 client and QuantPlatformKit package. Existing deployed monitors and dashboard have not been switched.
