# CodexAuditBridge

[English README](README.md)

> 投资有风险。本项目不构成投资建议，仅用于学习、研究和工程审阅。

## 这个仓库是什么

CodexAuditBridge 是 QuantStrategyLab 的审计自动化桥接工具。运行 self-hosted Codex 审计 workflow，用于 snapshot review 和低风险修复 PR。

它产出研究、审计或编排类 artifact，不应自行提交券商订单，也不应直接修改 live allocation。

## 架构边界

CodexAuditBridge 是 QuantStrategyLab 组织内的 Codex 调用边界。各 source repository 只负责派发审计请求，不应在自身 workflow 中直接拼接 `codex exec`，也不应依赖某个特定 Codex runner。

当前执行模型：

1. source repository 创建或定位审计 issue。
2. source repository 派发本仓库的 `.github/workflows/selfhosted_monthly_review.yml`。
3. CodexAuditBridge 校验 source repository 和 task mapping，使用受限 GitHub token clone source repository，并运行指定 provider。
4. 评论、分支、PR 等 GitHub 写操作只由 CodexAuditBridge 负责。

这个边界应留在 `QuantStrategyLab` 组织内。不要把 QuantStrategyLab 审计执行或 source repository 写 token 移到其他组织。如果后续替换 self-hosted Codex 依赖，优先采用 QuantStrategyLab 自己拥有的 HTTPS/443 Codex service；该 service 只返回 review 文本或结构化 patch 建议，clone、校验、commit、push 和 PR 仍由 CodexAuditBridge 负责。

## 支持的 source repository

| Source repository | 允许的 task |
| --- | --- |
| `QuantStrategyLab/AiLongHorizonSignalPipelines` | `long_horizon_signal_shadow` |
| `QuantStrategyLab/CryptoLivePoolPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/CryptoSnapshotPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/HkEquitySnapshotPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/ResearchSignalContextPipelines` | `long_horizon_signal_shadow` |
| `QuantStrategyLab/UsEquitySnapshotPipelines` | `monthly_snapshot_audit` |

新增 dispatcher 时，需要同步更新 `scripts/run_monthly_codex_audit.py` 里的 `SOURCE_REPO_TASKS`，并补充回归测试证明对应 repository/task pair 会被接受。

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

运行自动化前请先阅读 `.github/workflows/`、`scripts/run_monthly_codex_audit.py` 和 README 文件。

```bash
git status --short
python3 -m unittest discover -s tests -v
```

## 延伸文档

- 暂无独立 `docs/` 目录；请先阅读本 README、`README.md` 和 workflow 文件。

## 社区和安全

- 贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，确认 PR 范围、本地校验和文档要求。
- 讨论、issue 和 review 请遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- 涉及密钥、自动化、券商/交易所或云资源的漏洞请按 [SECURITY.md](SECURITY.md) 私密报告；不要为 secret 或实盘风险开公开 issue。

## 许可证

详见 [LICENSE](LICENSE)。
