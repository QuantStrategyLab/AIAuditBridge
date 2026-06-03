# CodexAuditBridge

[English README](README.md)

> 投资有风险。本项目不构成投资建议，仅用于学习、研究和工程审阅。

## 这个仓库是什么

CodexAuditBridge 是 QuantStrategyLab 的审计自动化桥接工具。运行 self-hosted Codex 审计 workflow，用于 snapshot review 和低风险修复 PR。

它产出研究、审计或编排类 artifact，不应自行提交券商订单，也不应直接修改 live allocation。

## 输出边界

- 生成报告应作为证据或审阅材料，不是自动交易指令。
- 保留来源可追溯性和 artifact 时间戳。
- 输出用于下游策略或平台改动前，需要人工 review。
- 凭据、私人数据和外部服务 token 不能提交到 Git，也不能写入日志。

## 仓库结构

- `tests/`：单元测试、契约测试和回归测试。
- `.github/workflows/`：CI、定时任务、发布或部署 workflow。
- `scripts/`：运维脚本和本地辅助工具。

## 快速开始

```bash
git status --short
Review .github/workflows/ and docs/ before running automation.
```

## 延伸文档

- 暂无独立 `docs/` 目录；请先阅读本 README 和 workflow 文件。

## 许可证

详见 [LICENSE](LICENSE)。
