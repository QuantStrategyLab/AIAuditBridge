# Drift OIDC allowlist rotation

Strategy drift workflows run from protected `main`, so GitHub emits their outer `workflow_ref` as `@refs/heads/main`. The service treats that claim as caller identity only. Executable delegated code is constrained independently by `job_workflow_ref`, pinned to an immutable QuantPlatformKit commit in `scripts/deploy_codex_audit_service.sh`.

To rotate the QPK reusable workflow without an untrusted or unavailable window:

1. Add both the current and next exact QPK SHAs to `ALLOWED_JOB_WORKFLOW_REFS`.
2. Merge and deploy AIAuditBridge.
3. Update all strategy `uses:` and `quant_platform_kit_ref` pins to the next SHA.
4. Verify CN, US, and crypto drift runs.
5. Remove the old SHA, merge, and deploy again.

Current rotation: retain `644cd9002ae92f2aaca6f7efb4afa4986fae05ea` only until CN, US, and crypto are verified on `fcddef20eea5deb876e739263042acdcb3e9cd1b`; [issue #64](https://github.com/QuantStrategyLab/AIAuditBridge/issues/64) tracks removal by 2026-07-18. The deploy workflow verifies that every allowlisted QPK SHA resolves to `reusable-drift-check.yml` before changing the service.

Never use a wildcard for `job_workflow_ref`. Strategy drift delegation must use an exact QPK SHA. The existing AIAuditBridge PR-review entry remains on protected `main` only while organization consumers still call `codex_pr_review.yml@main`; migrate that entry to a SHA only together with all consumer workflow pins.

The service also enforces that any allowed strategy `drift-check.yml` caller presents a `job_workflow_ref` for QuantPlatformKit's `reusable-drift-check.yml`. A different allowlisted reusable workflow cannot be substituted.
