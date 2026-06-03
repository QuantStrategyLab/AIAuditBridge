# Contributing

## 中文摘要

- 用途：本文档说明如何向 `CodexAuditBridge` 提交低风险、可审阅的变更。
- 主要覆盖：`Ground Rules`、`Documentation Standards`、`Branching and Pull Requests`、`Local Verification`。
- 阅读顺序：先确认仓库边界和变更范围，再运行适合本仓库的本地校验。
- 风险提示：涉及策略、artifact、自动化、密钥、云资源、券商或交易所行为的变更，必须先用测试环境、dry-run 或只读证据验证；不要只凭示例修改生产。
- 英文正文保留更完整的命令、字段名和配置键；如果摘要和正文不一致，以正文中的实际命令和配置为准。

Thanks for contributing to `CodexAuditBridge`.

## Ground Rules

- Prefer small pull requests with one clear purpose.
- Keep refactors separate from behavior, contract, workflow, or documentation changes.
- Preserve this repository's boundary as a self-hosted audit automation bridge; do not move broker execution, live-allocation decisions, private credentials, or unrelated platform logic into it.
- Add or update tests, examples, docs, or reproducible evidence when changing behavior or public contracts.

## Documentation Standards

- Keep `README.md` as the entry point for project purpose, boundary, repository layout, quick start, and links to deeper docs.
- Put long-form runbooks, artifact contracts, evidence notes, and architecture details under `docs/` when they outgrow the README.
- Document inputs, outputs, required permissions, risk controls, and validation commands for workflows or scripts that touch external systems.
- Keep English and Chinese user-facing docs aligned when a change affects operators, contributors, or downstream platform users.

## Branching and Pull Requests

- Create a topic branch for each change.
- Open a pull request with a concise summary, scope boundary, and concrete validation notes.
- Wait for CI to pass before merging.
- Do not include generated artifacts, private data, credentials, account identifiers, or local environment files unless the repository explicitly documents them as public examples.

## Local Verification

Run the lightweight whitespace check for every change and the repository test command when code, contracts, workflows, or examples change:

```bash
git diff --check
python3 -m unittest discover -s tests -v
```

For documentation-only changes, at minimum review Markdown links, headings, and bilingual consistency before opening the pull request.
