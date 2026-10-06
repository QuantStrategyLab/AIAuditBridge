# Security Policy

Thanks for helping keep `AIAuditBridge` safe.

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
