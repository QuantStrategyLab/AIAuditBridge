# Security Policy

## 中文摘要

- 用途：本文档说明如何报告 `CodexAuditBridge` 的安全问题，以及密钥或凭证暴露时的处理顺序。
- 主要覆盖：`Reporting a Vulnerability`、`Secret and Credential Exposure`、`Scope Notes`。
- 阅读顺序：发现问题后先避免公开泄露，再通过私密渠道提供最小复现信息。
- 风险提示：涉及实盘、密钥、权限、Cloud Run、GitHub Actions、交易所或券商 API 的问题，不要开公开 issue 或贴出敏感日志。
- 英文正文保留更完整的命令、字段名和配置键；如果摘要和正文不一致，以正文中的实际命令和配置为准。

Thanks for helping keep `CodexAuditBridge` safe.

This repository is part of the QuantStrategyLab automation, research, or trading-support surface. Please do **not** open a public issue for vulnerabilities involving credentials, broker or exchange access, cloud resources, workflow tokens, private market data, account identifiers, order execution, or secret material.

## Reporting a Vulnerability

- Contact the maintainer directly at GitHub: `@Pigbibi`.
- If private vulnerability reporting is enabled for this repository, prefer that channel.
- Include the repository name, affected commit or branch, environment details, and exact reproduction steps.
- Share only the minimum logs, payloads, or screenshots needed to reproduce the issue, and redact secrets or account identifiers.

## Secret and Credential Exposure

If you suspect tokens, passwords, API keys, service-account keys, cookies, broker credentials, or workflow credentials were exposed:

1. Rotate the exposed secrets immediately.
2. Pause scheduled jobs, deployments, or external integrations if the exposure can affect automation, artifact publishing, notifications, or trading behavior.
3. Remove the exposed material from open pull requests, issues, logs, and artifacts.
4. Coordinate any required history rewrite or downstream credential update with the maintainer.

## Scope Notes

Security fixes should stay minimal and focused. Please avoid bundling unrelated refactors, formatting churn, research changes, or feature work with a security report or patch.
