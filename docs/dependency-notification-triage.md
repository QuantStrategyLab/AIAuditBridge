# Dependency notification triage (batch 1)

Pure adapter for audit report §9.15.5 first batch.

## Scope

- Classify structured Dependabot / engineering-review evidence onto existing
  `BriefingFinding` / `BriefingAction` lanes.
- Map Schwab narrow-lane audit decisions without side effects.
- Keep GitHub Codex App as the single PR review owner.

## Routing (fail-closed)

| Evidence | Lane |
| --- | --- |
| Known low-risk Dependabot patch/minor + successful CI + dependency-only | `quiet` |
| Known workflow / Docker / QPK / major / security / CI failure | `telegram` |
| Quiet-gate evidence missing, conflicting, or unknown (SHA, files, CI, dependency-only, …) | `telegram` (`review_required`, `confidence=unknown` / `review_required`) |
| Explicit ordinary source-code review or non-Dependabot review | `github_issue` |

Gate uncertainty is escalated to Telegram so an unattended engineering chain cannot
bury “cannot tell if quiet is safe” as ordinary GitHub-issue noise.

## Non-goals (this batch)

- No GitHub notifications API consumer
- No Telegram / GitHub issue sends from the adapter itself
- No model calls
- No second PR reviewer, event bus, or parallel notification system
- No changes to trading, strategy, QPK, broker, credentials, production
  workflows, or auto-merge policy

## Entry points

- `triage_dependency_notification(evidence)` → `DependencyTriageResult`
- `triage_schwab_dependency_audit_decision(decision)` → `DependencyTriageResult`
- `triage_to_briefing_result(result)` → `BriefingConsumptionResult` for
  `dispatch_briefing_result(..., dry_run=True|False)`

Production notification intake remains an open gap until a trusted structured
evidence source is wired in a later authorized batch.
