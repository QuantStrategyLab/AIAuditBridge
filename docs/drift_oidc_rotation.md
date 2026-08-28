# Drift OIDC allowlist rotation

Strategy drift workflows run from protected `main`, so GitHub emits their outer `workflow_ref` as `@refs/heads/main`. The service treats that claim as caller identity only. Executable delegated code is constrained independently by `job_workflow_ref`, pinned to an immutable QuantPlatformKit commit in `scripts/deploy_codex_audit_service.sh`.

To rotate the QPK reusable workflow without an untrusted or unavailable window:

1. Add both the current and next exact QPK SHAs to `ALLOWED_JOB_WORKFLOW_REFS`.
2. Merge and deploy AIAuditBridge.
3. Update all strategy `uses:` and `quant_platform_kit_ref` pins to the next SHA.
4. Verify CN, US, and crypto drift runs.
5. Remove the old SHA, merge, and deploy again.

Current rotation retains the prior exact QPK workflow refs while CN, HK, US, and crypto move to `6b887d9954eb656141597eac077ca22053a525ef`. That version upgrades artifact actions to the supported runner runtime; it does not broaden caller identity or execution authority. [issue #64](https://github.com/QuantStrategyLab/AIAuditBridge/issues/64) tracks removal after the new pins complete scheduled verification. The deploy workflow verifies that every allowlisted QPK SHA resolves to `reusable-drift-check.yml` before changing the service.

Never use a wildcard for `job_workflow_ref`. Strategy drift delegation must use an exact QPK SHA. AIAuditBridge PR-review OIDC entries are retired and must not be restored; GitHub Codex App is the sole AI PR reviewer.

The canonical direct audit identity `QuantStrategyLab/AIAuditBridge/.github/workflows/codex_audit.yml@refs/heads/main` is also pinned exactly because live `workflow_dispatch` tokens can include it as `job_workflow_ref`.

The service also enforces that any allowed strategy `drift-check.yml` caller presents a `job_workflow_ref` for QuantPlatformKit's `reusable-drift-check.yml`. A different allowlisted reusable workflow cannot be substituted.
