# Dependency notification triage

Pure adapter for audit report §9.15.5 / §9.15.6.

## Scope

- Classify structured Dependabot / engineering-review evidence onto existing
  `BriefingFinding` / `BriefingAction` lanes.
- Map Schwab narrow-lane audit decisions without side effects.
- Keep GitHub Codex App as the single PR review owner.
- Provide a **trusted structured input** dry-run CLI that previews routing only.

## Routing (fail-closed)

| Evidence | Lane |
| --- | --- |
| Known low-risk Dependabot patch/minor + successful CI + dependency-only | `quiet` |
| Known workflow / Docker / QPK / major / security / CI failure | `telegram` |
| Quiet-gate evidence missing, conflicting, or unknown (SHA, files, CI, dependency-only, …) | `telegram` (`review_required`, `confidence=unknown` / `review_required`) |
| Explicit ordinary source-code review or non-Dependabot review | `github_issue` |

Gate uncertainty is escalated to Telegram so an unattended engineering chain cannot
bury “cannot tell if quiet is safe” as ordinary GitHub-issue noise.

## Trusted intake dry-run (batch 2)

Entry: `scripts/run_dependency_notification_dry_run.py` →
`run_trusted_intake_dry_run(...)`.

```bash
python3 scripts/run_dependency_notification_dry_run.py --input sample.json
python3 scripts/run_dependency_notification_dry_run.py --input - < sample.json
```

Input schema (local JSON only):

```json
{
  "schema_version": 1,
  "events": [
    {
      "repository": "org/repo",
      "pr_number": 123,
      "author_login": "dependabot[bot]",
      "update_class": "minor",
      "dependency_names": ["anyio"],
      "changed_files": ["pyproject.toml", "uv.lock"],
      "base_sha": "<40-hex>",
      "head_sha": "<40-hex>",
      "ci_status": "success",
      "dependency_only_manifest_change": true,
      "qpk_pin_changed": false
    }
  ]
}
```

Behavior:

- Validates top-level schema, event object type, required fields, and a hard
  event-count limit (`MAX_TRUSTED_INTAKE_EVENTS`).
- Dedupes by `repository` / `pr_number` / `head_sha` before triage.
- Calls `triage_dependency_notification` and aggregates into
  `BriefingConsumptionResult`, then `dispatch_briefing_result(..., dry_run=True)`.
- Prints redacted JSON (action/counts/per-event decision fields + dry-run
  presence summaries). Does **not** emit raw title/body, tokens, provider
  responses, or unknown raw errors.
- Schema/JSON/limit/missing-field failures are fail-closed: non-zero exit and an
  auditable `telegram` + `review_required` result.

This path is **trusted local input + dry-run preview only**. It does not read
GitHub personal notifications, call the notifications API, invoke a model, send
Telegram, create GitHub issues, write remotes, or touch
`automation_run_ledger`.

## Controlled PR source adapter (batch 3)

Entry: `scripts/run_dependency_notification_source_dry_run.py` →
`service/dependency_notification_source.py` → existing
`run_trusted_intake_dry_run(...)`.

```bash
python3 scripts/run_dependency_notification_source_dry_run.py \
  --repos QuantStrategyLab/ExampleRepo,QuantStrategyLab/OtherRepo
# or:
# DEPENDENCY_NOTIFICATION_REPO_ALLOWLIST=org/a,org/b \
#   python3 scripts/run_dependency_notification_source_dry_run.py
```

Behavior:

- **Explicit repository allowlist only** (`--repos` / `--repo` /
  `DEPENDENCY_NOTIFICATION_REPO_ALLOWLIST`). Empty default is rejected; there is
  no org-wide scan.
- Hard caps: `MAX_ALLOWLIST_REPOS`, `MAX_PRS_PER_REPO`, and the existing
  `MAX_TRUSTED_INTAKE_EVENTS`. GitHub list pagination reuses
  `scripts/run_dependency_audit.github_list_all` with `max_pages=1` and never
  calls Schwab audit write/merge paths.
- GET-only: open PR list, PR files, commit check-runs. No POST/PATCH/PUT/DELETE,
  no issue/comment/merge, no workflow schedule changes, no model calls.
- Converts structured fields (author, files, base/head SHA, CI check-run status,
  Dependabot `version-update:semver-*` marker / security labels) into schema v1
  events. **Title/body free text never alone grants quiet**; missing
  `update_class` or other key evidence becomes `unknown` and the existing
  triage escalates to Telegram / `review_required`.
- Output is the redacted dry-run preview plus a safe `source` summary. Tokens,
  PR title/body, and raw API error bodies are not printed.
- Network / pagination / allowlist failures exit non-zero with a fail-closed
  Telegram summary.

Token gap: prefers existing `CODEX_AUDIT_GH_TOKEN` / `GH_TOKEN`. Workflow
`GITHUB_TOKEN` is only accepted when the allowlist is exactly
`GITHUB_REPOSITORY` (single-repo). Cross-repo allowlists need a broader read
token; this batch does not add secrets. Pure payload conversion
(`build_trusted_event` / `build_trusted_intake_payload`) works offline without
network when callers already have structured PR snapshots.

This path is still **dry-run only**. It does not enable live notification
sending, auto-merge, or `dependency_audit.yml` schedules.

## Non-goals

- No GitHub notifications API consumer (still open)
- No live Telegram / GitHub issue sends from this adapter or dry-run CLI
- No model calls
- No second PR reviewer, event bus, or parallel notification system
- No changes to trading, strategy, QPK, broker, credentials, production
  workflows, or auto-merge policy
- No org-wide repository discovery; no schedule wiring this batch

## Entry points

- `triage_dependency_notification(evidence)` → `DependencyTriageResult`
- `triage_schwab_dependency_audit_decision(decision)` → `DependencyTriageResult`
- `triage_to_briefing_result(result)` → `BriefingConsumptionResult` for
  `dispatch_briefing_result(..., dry_run=True|False)`
- `run_trusted_intake_dry_run(payload)` → redacted dry-run preview dict
- CLI: `scripts/run_dependency_notification_dry_run.py`
- `build_trusted_event` / `collect_or_fail_closed` /
  `run_source_dry_run` → schema v1 + dry-run preview from allowlisted PR GETs
- CLI: `scripts/run_dependency_notification_source_dry_run.py`
