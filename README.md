# CryptoCodexAuditBridge

CryptoCodexAuditBridge runs monthly repository audits on a VPS-hosted
GitHub Actions runner that already has the Codex CLI installed and logged in.

The intended flow is:

1. A source repository publishes a monthly report issue.
2. The source workflow dispatches this repository.
3. This repository checks out the source repository on the self-hosted runner.
4. `codex exec` audits the monthly issue and may make low-risk fixes.
5. The outer runner posts the audit result back to the source issue and opens a
   pull request from `codex/monthly-review-issue-<issue>-<stamp>` when Codex
   changed files.

Codex itself is kept inside a local clone. It does not receive GitHub tokens in
the prompt, and it is instructed not to comment, push, merge, or manage secrets.

## Required Setup

Runner:

- A self-hosted runner with labels `self-hosted` and `codex-vps`.
- `codex` available in `PATH`.
- `codex login` completed for the same OS user that runs the GitHub runner.

Bridge repository credentials, choose one:

- Preferred: `CROSS_REPO_GITHUB_APP_ID` repository variable and
  `CROSS_REPO_GITHUB_APP_PRIVATE_KEY` repository secret. The GitHub App must be
  installed on the snapshot source repositories, currently
  `CryptoSnapshotPipelines` and `UsEquitySnapshotPipelines`, and granted
  contents write, issues write, and pull requests write.
- Fallback: `CODEX_AUDIT_GH_TOKEN` repository secret with access to the source
  repository. It needs repository metadata read, contents read/write, issues
  read/write, and pull requests read/write.

Source repository dispatch credentials, choose one:

- Preferred: the existing cross-repo GitHub App variable/secret pair
  `CROSS_REPO_GITHUB_APP_ID` and `CROSS_REPO_GITHUB_APP_PRIVATE_KEY`. The app
  must be installed on this bridge repository with contents write permission.
- Fallback: `CODEX_AUDIT_DISPATCH_TOKEN` token allowed to create
  `repository_dispatch` events in this bridge repository.

Source repository variables:

- `SELFHOSTED_CODEX_REVIEW_ENABLED`: defaults to `true`.
- `SELFHOSTED_CODEX_REVIEW_REPOSITORY`: defaults to
  `QuantStrategyLab/CryptoCodexAuditBridge`.
- `SELFHOSTED_CODEX_REVIEW_MODE`: defaults to `review_and_fix`.
- `LEGACY_AI_REVIEW_ENABLED`: defaults to `false`.

## Python Audit Environment

The runner bootstraps a small cached Python virtualenv before invoking Codex.
By default it installs `pandas`, which is enough for the monthly release
contract and status-summary checks in the snapshot source repositories.

Optional controls:

- `CODEX_AUDIT_INSTALL_PYTHON_DEPS=false` skips dependency bootstrapping.
- `CODEX_AUDIT_PYTHON_BOOTSTRAP_PACKAGES="pandas PyYAML"` overrides packages.
- `CODEX_AUDIT_PYTHON_VENV=~/.cache/crypto-codex-audit/python-venv` changes the
  persistent venv path.

## Safety Model

- The monthly publish job remains deterministic and does not call model APIs.
- This bridge calls the local `codex` CLI from the self-hosted runner. The
  Codex subprocess receives a scrubbed environment with token/secret-like
  variables removed.
- Fixes are submitted as PRs by default.
- Automatic merge is available only through the explicit `auto_merge` input and
  should remain disabled until branch protection and CI gates are confirmed.
- Changes under `data/` and secret-like paths are blocked by default.

## Manual Dispatch

```bash
gh workflow run selfhosted_monthly_review.yml \
  --repo QuantStrategyLab/CryptoCodexAuditBridge \
  -f source_repo=QuantStrategyLab/UsEquitySnapshotPipelines \
  -f issue_number=123 \
  -f source_ref=main \
  -f mode=review_and_fix \
  -f auto_merge=false
```
