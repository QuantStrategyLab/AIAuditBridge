# CodexAuditBridge

[Chinese README](README.zh-CN.md)

> Investing involves risk. This project does not provide investment advice and is for education, research, and engineering review only.

## What this repository is

CodexAuditBridge is a QuantStrategyLab audit automation bridge. It runs self-hosted Codex audit workflows for snapshot reviews and low-risk fix pull requests.

It produces research, audit, or orchestration artifacts. It should not submit broker orders or mutate live allocations by itself.

## Architecture boundary

CodexAuditBridge is the organization-local Codex boundary for QuantStrategyLab. Source repositories dispatch review requests to this repository; they should not embed raw `codex exec` commands or depend on a specific Codex runner themselves.

Current execution model:

1. A source repository creates or identifies an audit issue.
2. The source repository dispatches `.github/workflows/selfhosted_monthly_review.yml` in this repository.
3. CodexAuditBridge validates the source repository and task mapping, clones the source repository with a scoped GitHub token, and runs the selected provider.
4. Only CodexAuditBridge performs GitHub writes such as comments, branches, and pull requests.

Keep this boundary inside the `QuantStrategyLab` organization. Do not move QuantStrategyLab audit execution or source-repository write tokens to another organization. If the self-hosted Codex dependency is replaced later, prefer a QuantStrategyLab-owned HTTPS/443 Codex service that returns review text or structured patch suggestions while CodexAuditBridge keeps clone, validation, commit, push, and PR ownership.

## Supported source repositories

| Source repository | Allowed task |
| --- | --- |
| `QuantStrategyLab/AiLongHorizonSignalPipelines` | `long_horizon_signal_shadow` |
| `QuantStrategyLab/CryptoLivePoolPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/CryptoSnapshotPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/HkEquitySnapshotPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/ResearchSignalContextPipelines` | `long_horizon_signal_shadow` |
| `QuantStrategyLab/UsEquitySnapshotPipelines` | `monthly_snapshot_audit` |

When adding a new dispatcher, update `SOURCE_REPO_TASKS` in `scripts/run_monthly_codex_audit.py` and add a regression test that proves the repository/task pair is accepted.

## Output boundary

- Treat generated reports as evidence or review material, not automatic trading instructions.
- Keep source traceability and artifact timestamps visible.
- Require human review before using outputs in downstream strategy or platform changes.
- Keep credentials, private data, and external service tokens out of Git and logs.

## Repository layout

- `tests/`: unit, contract, and regression tests.
- `.github/workflows/`: CI, scheduled jobs, release, or deployment workflows.
- `scripts/`: operator scripts and local helpers.

## Quick start

Review `.github/workflows/`, `scripts/run_monthly_codex_audit.py`, and the README files before running automation.

```bash
git status --short
python3 -m unittest discover -s tests -v
```

## Useful docs

- No separate `docs/` directory yet; start with this README, `README.zh-CN.md`, and the workflow files.

## Community and security

- See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request scope, local verification, and documentation expectations.
- Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for maintainer and contributor conduct.
- Report credential, automation, broker, exchange, or cloud-resource vulnerabilities through [SECURITY.md](SECURITY.md); do not open public issues for secrets or live-execution risk.

## License

See [LICENSE](LICENSE).
