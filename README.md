# CodexAuditBridge

> ⚠️ 投资有风险，不构成投资建议，仅供学习交流用途。


## 中文摘要

- 用途：本文档围绕 `CodexAuditBridge`，用于理解 `CodexAuditBridge` 的配置、运行、部署、研究或验收边界。
- 主要覆盖：`Required Setup`、`Provider Model`、`Task Model`、`Python Audit Environment`、`Safety Model`。
- 阅读顺序：先确认边界、输入输出和权限要求，再执行文档里的命令、CI、dry-run、发布或切换步骤。
- 风险提示：涉及实盘、密钥、权限、Cloud Run、交易所或券商 API 的变更，必须先在测试环境或 dry-run 验证；不要只凭示例直接修改生产。
- 英文正文保留更完整的命令、字段名和配置键；如果摘要和正文不一致，以正文中的实际命令和配置为准。
CodexAuditBridge runs monthly repository audits on a VPS-hosted
GitHub Actions runner that already has the Codex CLI installed and logged in.

The intended flow is:

1. A source repository publishes a monthly report or shadow-signal issue.
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
  must be installed on this bridge repository with actions write permission.
- Fallback: `CODEX_AUDIT_DISPATCH_TOKEN` token allowed to create
  `workflow_dispatch` events for `selfhosted_monthly_review.yml` in this bridge
  repository.

Source repository variables:

- `SELFHOSTED_CODEX_REVIEW_ENABLED`: defaults to `true`.
- `SELFHOSTED_CODEX_REVIEW_REPOSITORY`: defaults to
  `QuantStrategyLab/CodexAuditBridge`.
- `SELFHOSTED_CODEX_REVIEW_MODE`: defaults to `review_and_fix`.
- `SELFHOSTED_CODEX_REVIEW_PROVIDER`: dispatches the bridge provider. Supported
  values are `auto`, `codex`, `api`, `openai`, and `anthropic`. `auto` is the
  default production path: it tries the self-hosted Codex path first, then falls
  back to the configured API reviewers when Codex setup or execution fails. It
  fails loudly when no API fallback key is configured.
- `LEGACY_AI_REVIEW_ENABLED`: defaults to `false`.

The bridge only accepts source repositories listed in the workflow and script
allowlist:

- `QuantStrategyLab/CryptoSnapshotPipelines` for `monthly_snapshot_audit`
- `QuantStrategyLab/UsEquitySnapshotPipelines` for `monthly_snapshot_audit`
- `QuantStrategyLab/AiLongHorizonSignalPipelines` for
  `long_horizon_signal_shadow`

## Provider Model

This repository owns the AI review execution layer. Source repositories should
produce a monthly report issue and dispatch this bridge; they do not need to
implement their own model-provider workflows.

- `auto`: runs the self-hosted Codex path first. If setup, dependency bootstrap,
  or Codex execution fails, it posts a combined API review from the configured
  fallback reviewers. Configure both `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`
  for the dual-AI fallback. If neither key is configured, the workflow fails
  loudly.
- `codex`: runs local `codex exec` on the self-hosted runner. In
  `review_and_fix` mode it may create a fix PR and does not use API fallback.
- `api`: skips Codex and posts a combined review from the configured API
  reviewers. It does not edit files.
- `openai`: sends the monthly issue body and recent comments to the OpenAI Chat
  Completions API and posts a review comment. It does not edit files.
- `anthropic`: sends the monthly issue body and recent comments to the
  Anthropic Messages API and posts a review comment. It does not edit files.

Optional bridge repository configuration for API fallback:

- `OPENAI_API_KEY`: repository secret.
- `OPENAI_MODEL`: repository variable, default `gpt-5.4-mini`.
- `OPENAI_API_BASE_URL`: optional runtime environment override for compatible
  APIs, default `https://api.openai.com/v1`.
- `ANTHROPIC_API_KEY`: repository secret.
- `ANTHROPIC_MODEL`: repository variable, default `claude-sonnet-4-6`.
- `ANTHROPIC_API_BASE_URL`: optional runtime environment override, default
  `https://api.anthropic.com/v1`.
- `ANTHROPIC_VERSION`: optional runtime environment override, default
  `2023-06-01`.

## Task Model

- `monthly_snapshot_audit`: the default task for snapshot artifact repositories.
  It reviews monthly release issues and may open low-risk fix PRs.
- `long_horizon_signal_shadow`: a research-only task for
  `AiLongHorizonSignalPipelines`. It may update only shadow signal artifacts
  under `data/output/latest_signal.*` or `data/output/signal_history/*.json`.
  It does not access broker credentials, place orders, or change production
  strategy logic.

## Python Audit Environment

The runner bootstraps a small cached Python virtualenv before invoking Codex.
By default it installs `pandas`, which is enough for the monthly release
contract and status-summary checks in the snapshot source repositories.

Optional controls:

- `CODEX_AUDIT_INSTALL_PYTHON_DEPS=false` skips dependency bootstrapping.
- `CODEX_AUDIT_PYTHON_BOOTSTRAP_PACKAGES="pandas PyYAML"` overrides packages.
- `CODEX_AUDIT_PYTHON_VENV=~/.cache/codex-audit/python-venv` changes the
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
  --repo QuantStrategyLab/CodexAuditBridge \
  -f source_repo=QuantStrategyLab/UsEquitySnapshotPipelines \
  -f issue_number=123 \
  -f source_ref=main \
  -f task=monthly_snapshot_audit \
  -f mode=review_and_fix \
  -f provider=auto \
  -f auto_merge=false
```
