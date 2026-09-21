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
python3 -m scripts.run_dependency_notification_dry_run --input sample.json
python3 -m scripts.run_dependency_notification_dry_run --input - < sample.json
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
python3 -m scripts.run_dependency_notification_source_dry_run \
  --repos QuantStrategyLab/ExampleRepo,QuantStrategyLab/OtherRepo
# or:
# DEPENDENCY_NOTIFICATION_REPO_ALLOWLIST=org/a,org/b \
#   python3 -m scripts.run_dependency_notification_source_dry_run
```

Behavior:

- **Explicit repository allowlist only** (`--repos` / `--repo` /
  `DEPENDENCY_NOTIFICATION_REPO_ALLOWLIST`). Empty default is rejected; there is
  no org-wide scan.
- Hard caps: `MAX_ALLOWLIST_REPOS`, `MAX_PRS_PER_REPO`, and the existing
  `MAX_TRUSTED_INTAKE_EVENTS`. Hitting a PR/event hard cap while open PRs or
  allowlisted repos remain is **fail-closed** (`source_truncated` → Telegram /
  `review_required`); remaining work is never silently dropped. GitHub list
  pagination reuses `scripts/run_dependency_audit.github_list_all` with
  `max_pages=1` and never calls Schwab audit write/merge paths.
- GET-only: open PR list, PR files, commit check-runs. Check-runs use bounded
  pagination (`MAX_CHECK_RUN_PAGES`); incomplete coverage, a full page at the
  cap, or a malformed response yields `ci_status=unknown` (never quiet success).
  No POST/PATCH/PUT/DELETE, no issue/comment/merge, no workflow schedule
  changes, no model calls.
- Converts structured fields (author, files, base/head SHA, CI check-run status,
  Dependabot `version-update:semver-*` marker / security labels) into schema v1
  events. **Title/body free text never alone grants quiet**; missing
  `update_class` or other key evidence becomes `unknown` and the existing
  triage escalates to Telegram / `review_required`.
- Output is the redacted dry-run preview plus a safe `source` summary. Tokens,
  PR title/body, and raw API error bodies are not printed.
- Network / pagination / allowlist failures exit non-zero with a fail-closed
  Telegram summary.

Token gap (adapter/CLI): prefers existing `CODEX_AUDIT_GH_TOKEN` / `GH_TOKEN`.
Workflow `GITHUB_TOKEN` is only accepted when the allowlist is exactly
`GITHUB_REPOSITORY` (single-repo). The manual Actions workflow (batch 4 /
9.15.9 follow-up) can mint a temporary read-only App token from existing
`CROSS_REPO_GITHUB_APP_*` credentials for one QuantStrategyLab allowlist entry;
the adapter itself still does not invent secrets. Pure payload conversion
(`build_trusted_event` / `build_trusted_intake_payload`) works offline without
network when callers already have structured PR snapshots.

This path is still **dry-run only**. It does not enable live notification
sending, auto-merge, or `dependency_audit.yml` schedules.

## Manual Actions verification entry (batch 4 / 9.15.9 follow-up)

Workflow: `.github/workflows/dependency_notification_source_dry_run.yml`
(`workflow_dispatch` only).

- Required input: `repos` — **exactly one** `QuantStrategyLab/<name>` entry
  (no empty, org-wide, other-owner, illegal path, or multi-repo lists this batch).
- Runs `python3 -m scripts.run_dependency_notification_source_dry_run` in
  GitHub-hosted Ubuntu so real GET calls use Actions' trusted Python/TLS.
- Permissions: `contents` / `pull-requests` / `actions` read only (workflow and
  App token). No write / `id-token` / administration / merge permissions.
- Self-repo allowlist (`GITHUB_REPOSITORY`): existing `GITHUB_TOKEN` /
  optional `CODEX_AUDIT_GH_TOKEN` / `GH_TOKEN` path.
- Cross-repo QuantStrategyLab allowlist: require existing
  `vars.CROSS_REPO_GITHUB_APP_ID` + `secrets.CROSS_REPO_GITHUB_APP_PRIVATE_KEY`,
  mint a temporary installation token via `actions/create-github-app-token`
  (`permission-contents/pull-requests/actions: read` only), inject **only** into
  `CODEX_AUDIT_GH_TOKEN`. Credential / mint / allowlist failures are fail-closed
  (non-zero); no `GITHUB_TOKEN` fallback for cross-repo reads. No new secrets.
- Still dry-run preview only: no Telegram/issue send, model, ledger, deploy,
  schedule, or write permissions. Non-zero CLI exit fails the job.

## Sender configuration-check dry-run (batch after 9.15.10)

Entry: `dispatch_briefing_result(..., send_dry_run=True)` and
`scripts/consume_daily_briefing.py --send-dry-run`.

- Reuses the existing dispatch path and dry-run preview assembly; does **not**
  add a second router.
- Reports non-secret booleans: `telegram_token_present`,
  `telegram_chat_ids_present`, `github_issue_target_valid`,
  `gh_executable_present`.
- Emits the same redacted preview shape as trusted intake
  (`telegram_dry_run` / `github_dry_run` with `present` + `safe_summary` only).
- Missing Telegram token/chat ids → `telegram_missing_env`; invalid issue
  target → `github_issue_target_invalid`; missing `gh` → `gh_executable_missing`.
  These remain fail-closed (`errors` non-empty / non-success CLI exit). Quiet
  actions stay quiet and do not require sender env.
- **Zero external side effects:** no Telegram HTTP/`sendMessage`, no
  `gh issue create`, no `automation_run_ledger` writes, no model / source-network
  / schedule / deploy / trading calls.
- This proves local configuration readiness for a future send identity only. It
  does **not** enable live notification sending.

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
- `dispatch_briefing_result(..., send_dry_run=True)` / `sender_prerequisites()`
  → sender configuration-check + redacted preview (no live send)
- CLI: `scripts/consume_daily_briefing.py --send-dry-run`
