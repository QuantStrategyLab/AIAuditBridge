# Drift OIDC allowlist rotation

Strategy drift workflows run from protected `main`, so GitHub emits their outer `workflow_ref` as `@refs/heads/main`. The service treats that claim as caller identity only. Executable delegated code is constrained independently by `job_workflow_ref`, pinned to an immutable QuantPlatformKit commit in `scripts/deploy_codex_audit_service.sh`.

To rotate the QPK reusable workflow without an untrusted or unavailable window:

1. Add both the current and next exact QPK SHAs to `ALLOWED_JOB_WORKFLOW_REFS`.
2. Merge and deploy AIAuditBridge.
3. Update all strategy `uses:` and `quant_platform_kit_ref` pins to the next SHA.
4. Verify CN, US, and crypto drift runs.
5. Remove the old SHA, merge, and deploy again.

Never use a wildcard or mutable branch for `job_workflow_ref`.
