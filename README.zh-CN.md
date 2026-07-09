# Quant AI Audit Bridge


## QSL 架构角色

- **层级**：`运维/审计工具`。
- **职责**：AI 审计与 review 自动化桥接。
- **事实源/归属**：审计 prompt、服务策略、workflow 健康术语。
- **消费对象**：QuantStrategyLab 仓库、PR/workflow 元数据、Codex/API 提供方。
- **禁止事项**：提交券商订单或修改 live allocation。

[English README](README.md)

> 投资有风险。本项目不构成投资建议，仅用于学习、研究和工程审阅。

## 这个仓库是什么

AIAuditBridge 是 QuantStrategyLab 的 AI 审计自动化桥接工具。优先运行 Codex VPS/service-backed 审计 workflow，并对获批的 review 和低风险修复 PR 提供 OpenAI/Anthropic API fallback。

它产出研究、审计或编排类 artifact，不应自行提交券商订单，也不应直接修改 live allocation。

“健康”口径分为在线服务健康、组织 workflow 健康、后台任务健康、产物/策略证据健康四类。新增 dashboard 面板或自动化 gate 前，请先阅读 [`docs/health_taxonomy.md`](docs/health_taxonomy.md)。
服务还提供一个结构化的自动化 triage 接口，用于故障分诊和发布前 readiness 指导；它只是建议，不会绕过 merge 或 deploy 控制。

## 架构边界

AIAuditBridge 是 QuantStrategyLab 组织内的 AI 审计调用边界。各 source repository 只负责派发审计请求，不应在自身 workflow 中直接拼接 `codex exec`、直接调用 provider API、实现模型路由或 fallback 策略。

当前执行模型：

1. source repository 创建或定位审计 issue。
2. source repository 派发本仓库的 monthly review workflow。workflow 文件名仍为 `codex_audit.yml` 以保持 dispatch 入口稳定，但 Codex 执行已经是 service-backed。
3. AIAuditBridge 校验 source repository 和 task mapping，使用受限 GitHub token clone source repository，并运行指定 provider/backend。
4. 评论、分支、commit、push、PR 等 GitHub 写操作只由 AIAuditBridge 负责。

这个边界应留在 `QuantStrategyLab` 组织内。不要把 QuantStrategyLab 审计执行或 source repository 写 token 移到其他组织。

Codex 执行现在只走 service backend：workflow 从 GitHub-hosted runner 调用 QuantStrategyLab 自有的 HTTPS/443 Codex audit service。service 只返回 review 文本或结构化 patch 建议；clone、路径校验、patch apply、commit、push、PR 和 issue comment 仍由 AIAuditBridge 负责。

PR review 死循环收敛：

- **自动（默认）**：同一 PR 连续 `pr_review.auto_converge_after` 轮（默认 3）仍为 blocking 时，`review` check 自动放行，仍会发 findings 评论。设为 `0` 可关闭。
- **可选人工**：也可打 `review-ack`（`pr_review.ack_labels`）立即放行。

复用 `AIAuditBridge` 的 `codex_pr_review.yml@main` 的仓库会自动继承该行为。

当 `CODEX_AUDIT_AUTO_MERGE=true` 时，bridge 会先检查变更文件面和总增删行数，只在低风险或中风险且未超过 policy 上限时给生成的 PR 添加 `auto-merge-ok` label，请求源仓库的受控自动合并。bridge 会在打标前按需创建配置的 label；如果源仓 token 没有创建 label 的权限，需要先手动创建该 label。若 source checkout 里存在 `.github/codex_auto_merge_policy.json`，bridge 会在 Codex 执行修改前读取基线策略，否则才使用内置默认值。高风险、未知文件面、策略文件变更、文件移除/重命名/复制或无效 policy 配置不会添加 `auto-merge-ok`，而是给 PR 添加配置的人工复核 label（默认 `human-review-required`），并在源 issue 评论中列出风险原因和文件，等待人工复核。bridge 不会直接调用 GitHub native auto-merge。最终是否合并仍由源仓库自己的 CI 和 merge-guard workflow 决定。

当源 issue 中出现 CI 失败或 requested-changes review 产生的 `codex-pr-feedback` marker 时，bridge 会把本次运行视为有界重试。只要 marker 指向的 PR 仍然 open、同仓、base 是请求的 source ref，并且 head 分支属于同一个 monthly issue，bridge 会更新现有 PR 分支，而不是再创建一个新 PR。在清理该 PR 上旧的受控自动合并 label 前，bridge 会复用 baseline policy label；如果 policy 无效，或 auto-merge / human-review label 无法安全区分，则跳过 label mutation。

这样可以避免每个 source repository 都硬编码 Codex CLI，也不会依赖 `QuantStrategyLab` 组织外的仓库。

## 兼容性治理定位

`QuantStrategyLab/AIAuditBridge` 只作为 ops/control-plane 的消费侧参与兼容治理：

- 仅消费兼容矩阵和治理元数据，确保审计/评审边界行为一致；
- 不参与策略/交易运行时的依赖图、升级决策或 runtime 级联；
- 本仓库中的兼容关系只用于审计与 review 运营（control-plane），不应被源仓库当作交易策略运行时依赖。


## 支持的 source repository

| Source repository | 允许的 task |
| --- | --- |
| `QuantStrategyLab/CryptoLivePoolPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/HkEquitySnapshotPipelines` | `monthly_snapshot_audit` |
| `QuantStrategyLab/ResearchSignalContextPipelines` | `long_horizon_signal_shadow` |
| `QuantStrategyLab/UsEquitySnapshotPipelines` | `monthly_snapshot_audit` |

新增 dispatcher 时，需要同步更新 `scripts/run_monthly_codex_audit.py` 里的 `SOURCE_REPO_TASKS`，并补充回归测试证明对应 repository/task pair 会被接受。

## Codex service 配置

AIAuditBridge 只使用 service backend。workflow 运行在 `ubuntu-latest`，并调用 QuantStrategyLab 自有 HTTPS/443 Codex audit service。

需要在 `QuantStrategyLab/AIAuditBridge` 配置：

- Repository secret `CODEX_AUDIT_SERVICE_URL`，例如 `https://codex-audit.example.com`。
  URL 可能暴露源站基础设施信息，因此放在 secret，不放在普通 variable。
- 可选 repository variable `CODEX_AUDIT_SERVICE_AUDIENCE`，默认 `quant-codex-audit`。
- 可选 repository variable `CODEX_AUDIT_API_FALLBACK_ALLOWED_SOURCE_REPOSITORIES`，
  用逗号或换行分隔。只有变量列出的 source repository 可以使用 OpenAI/Anthropic fallback。
- 可选 repository variable `CODEX_AUDIT_API_FALLBACK_ALLOW_FIX`，默认 `true`。
  当 `CODEX_AUDIT_MODE=review_and_fix` 时，OpenAI/Anthropic fallback 会复用与 Codex
  service 相同的 patch contract，并可以开 remediation PR，而不只是发 review 评论。
- 可选 repository variable `CODEX_AUDIT_API_FALLBACK_PROVIDER_ORDER`，默认
  `openai,anthropic`。
- repository variable `OPENAI_MODEL`，API fallback 使用的 OpenAI 模型。
- repository variable `ANTHROPIC_MODEL`，API fallback 使用的 Anthropic 模型。
- monthly audit 的默认 provider 随 task 而变：`monthly_snapshot_audit`
  默认 `auto`，`long_horizon_signal_shadow` 默认 `codex`；如需固定 provider，
  请显式设置 `CODEX_AUDIT_PROVIDER`。workflow dispatch 使用 `task_default`
  把 provider 选择交给 task policy。
- repository variable `CODEX_AUDIT_SERVICE_MODEL`，VPS Codex service 主路径模型；
  `VPS Codex Service Ops` deploy 会写入 systemd unit。
- PR review 可以通过 repository variable
  `CODEX_PR_REVIEW_API_FALLBACK_ENABLED=true` 或 reusable workflow input
  `api_fallback_enabled` 在可恢复的 Codex service 失败后启用 direct API
  fallback。reusable workflow input 使用字符串 `true`/`false`；省略时先使用
  repository variable，再为兼容旧调用方默认 `true`。只走 Codex 的调用方应传入
  `api_fallback_enabled: "false"`。当未配置 service URL 时，是否允许 API-only
  PR review 由 `CODEX_PR_REVIEW_DIRECT_API_PRIMARY_ENABLED` 或 reusable
  workflow input `direct_api_primary_enabled` 单独控制；该项使用同样的变量和
  兼容默认规则，Codex-only 调用方应设为 `"false"`。
- workflow 已配置 `id-token: write`，用于向 service 提供 GitHub Actions OIDC token。

service host 启动示例：

```bash
CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=QuantStrategyLab/AIAuditBridge \
CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES='QuantStrategyLab/AIAuditBridge,QuantStrategyLab/CryptoLivePoolPipelines,QuantStrategyLab/HkEquitySnapshotPipelines,QuantStrategyLab/UsEquitySnapshotPipelines,QuantStrategyLab/ResearchSignalContextPipelines' \
CODEX_AUDIT_SERVICE_AUDIENCE=quant-codex-audit \
CODEX_AUDIT_SERVICE_MODEL=gpt-5.4 \
python3 -m service.ai_gateway_service
```

443/TLS 建议由平台负载均衡或反向代理负责，并把 `/v1/codex-audit` 转发到 service 端口。不要把 GitHub 写 token 传给这个 service。

service host 应使用已登录的 Codex CLI session。服务在启动 Codex
子进程前会清理 secret/API key 类环境变量，不会向 Codex 子进程注入 API key。

如果暂时没有自定义域名，`cloudflare/codex-audit-proxy/` 提供了一个最小 Cloudflare Worker，可用免费的 `workers.dev` HTTPS 入口，并把 VPS origin URL 保存在 Cloudflare secret 中。生产服务路径使用异步模式：先 `POST /v1/codex-audit/jobs`，再轮询 `GET /v1/codex-audit/jobs/{job_id}`。部署步骤和开源仓库注意事项见 `docs/async_service_deployment.md`。

维护者可以通过手动触发 `VPS Codex Service Ops` workflow，借助现有 `self-hosted,codex-vps` runner 巡检或部署 VPS 侧服务。部署时保持 Pigbibi `/v1/codex` gateway 不变，只在 nginx 上增加 `/v1/codex-audit` 路由到本仓库的 audit service。

### Service patch contract

`review_and_fix` 模式下，service 必须只返回一个 JSON object：

```json
{
  "final_message": "用于 issue comment 或 PR body 的 Markdown 总结。",
  "changes": [
    {
      "path": "relative/file/path.py",
      "content": "完整 UTF-8 文件内容"
    }
  ]
}
```

AIAuditBridge 会在本地写文件前拒绝绝对路径、`.git` 路径、疑似 secret 路径和被禁止的 data 路径。

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

- [`docs/architecture.md`](docs/architecture.md)：service 组件和 endpoint map。
- [`docs/async_service_deployment.md`](docs/async_service_deployment.md)：VPS service 与 Worker 部署。
- [`docs/health_taxonomy.md`](docs/health_taxonomy.md)：dashboard、quota、workflow、job 和 artifact 健康状态口径。
- [`docs/ai_autonomy_architecture.md`](docs/ai_autonomy_architecture.md)：AI 自治架构评审和阶段路线。

## 社区和安全

- 贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，确认 PR 范围、本地校验和文档要求。
- 讨论、issue 和 review 请遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- 涉及密钥、自动化、券商/交易所或云资源的漏洞请按 [SECURITY.md](SECURITY.md) 私密报告；不要为 secret 或实盘风险开公开 issue。

## 许可证

详见 [LICENSE](LICENSE)。
