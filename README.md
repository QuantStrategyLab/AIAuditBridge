# CodexAuditBridge

[Chinese README](README.zh-CN.md)

> Investing involves risk. This project does not provide investment advice and is for education, research, and engineering review only.

## What this repository is

CodexAuditBridge is a QuantStrategyLab audit automation bridge. It runs self-hosted Codex audit workflows for snapshot reviews and low-risk fix pull requests.

It produces research, audit, or orchestration artifacts. It should not submit broker orders or mutate live allocations by itself.

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

```bash
git status --short
Review .github/workflows/ and docs/ before running automation.
```

## Useful docs

- No separate `docs/` directory yet; start with this README and the workflow files.

## License

See [LICENSE](LICENSE).
