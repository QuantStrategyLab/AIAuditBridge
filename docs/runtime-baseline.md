# Independent runtime baseline

QuantAIService owns the complete task runtime under `src/ai_service`, its dependency declarations, and its future eight scenario templates. It no longer installs or downloads `personal-ai-service`. The `ai_service.client` import is retained for existing consumers; it is delivered by this wheel, not a cross-owner shared package. Install the two services in separate virtual environments because both independently provide this compatibility namespace.

The initial runtime and service regression tests were copied, with the owner's authorization, from PersonalAIService commit `f9bdf0fb55e7195d459520155bfa01652e793a5e` (including PR #31 safe MCP logging). Attribution and the MIT terms are in `LICENSE.task-service`. Subsequent maintenance belongs to this repository; there is no synchronization job or runtime dependency on PersonalAIService. Server discovery reports this distribution's name and version.

CI builds and installs this wheel in a clean environment, checks dependency consistency, and runs the task service suite and Quant admission tests. The logging suite checks anonymous authentication denials and excludes tokens, task material, raw exceptions and private identifiers. Deployment-specific Personal tests and configs are not copied.

No production configuration, grants, identity, provider, deployment or trading logic is changed. Unpublished wheels and local synthetic tests do not demonstrate runtime login or business recovery. Roll back code and compatible configuration together; do not rewrite state.
