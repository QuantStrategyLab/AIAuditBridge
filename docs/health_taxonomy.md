# QuantStrategyLab AI 健康状态口径

这个文档用于统一 AIAuditBridge、dashboard、月度审计和自动合并流程里的“健康”含义，避免把服务可用性、后台任务、产物质量混成一个状态。

## 1. 在线服务健康

在线服务健康只回答：AIAuditBridge / AiGateway 服务当前能否稳定响应请求。

主要来源：

- `/healthz`：进程存活探针。
- `/v1/ai/health`：在线接口的错误率、P95 延迟、请求量和降级原因。
- `/v1/ai/quota`：GPT / Claude Admin Usage、Codex rate-limit 快照和本服务内部成本估算。

Dashboard 里的“服务健康”和“用量与额度”属于这一类。

注意：

- `degraded` 表示在线接口因为错误率或延迟超过阈值而降级，不表示策略退化。
- Codex 额度展示的是剩余额度或窗口内可用性，不等同于 OpenAI / Anthropic API key 账单。
- 成本暂不可用只表示对应 Usage/Cost API 没有返回金额，不表示没有发生调用。

## 2. 组织 workflow 健康

组织 workflow 健康回答：QuantStrategyLab 仓库的 GitHub Actions 最近是否有失败、运行中或未知状态。

主要来源：

- `/v1/ai/org-health`：按仓库汇总最近 workflow run。
- GitHub Actions run / check rollup。

Dashboard 里的“组织健康”属于这一类。

注意：

- workflow 失败不一定是服务故障，也可能是源仓库测试失败、权限配置、外部服务超时或历史取消 run。
- `unknown` 通常表示没有可用 run、GitHub API 暂不可读或 token 未配置，需要看具体 reason。

## 3. 后台任务健康

后台任务健康回答：某一次 Codex 审计、PR review、月度报告生成或修复任务是否完成。

主要来源：

- `/v1/ai/execute/jobs/{job_id}`：异步 Codex job 状态。
- AIAuditBridge workflow summary、issue comment、PR comment。
- 源仓库 CI / merge guard。

注意：

- job `succeeded` 只说明这次后台任务完成，不说明在线服务长期健康。
- job `failed` 不一定说明系统不可用；需要结合失败原因区分 quota、provider、路径保护、CI 或人工审计阻断。
- AIAuditBridge 可以请求低风险 PR 自动合并，但最终合并仍由源仓库 branch protection、CI 和 merge guard 决定。

## 4. 产物和策略证据健康

产物和策略证据健康回答：月度报告、策略健康报告、snapshot manifest、发布产物是否新鲜、完整、可审计。

常见来源：

- 月度审计 issue / report bundle。
- strategy health report、live decay report、artifact freshness workflow。
- Pages / RSS / Telegram 等发布产物。

注意：

- 这类健康是审计证据，不是在线服务健康。
- “策略退化”“watch”“review_for_retirement”是研究/运营判断，不能自动解释为服务异常。
- 发布成功不等于研究链路健康；研究报告通过也不等于生产发布可用。

## 5. 自动处理和人工审计边界

可以默认交给 AI 自动处理的范围：

- 文档、测试、报告 helper、workflow 文案等低风险变更。
- 明确受路径白名单和行数上限约束的修复 PR。
- branch protection 与 CI 全绿后的低风险依赖/兼容性元数据更新。

必须人工审计的范围：

- secret、token、GitHub App、OIDC、Cloudflare/VPS 权限和部署边界。
- branch protection、auto-merge policy、review gate、路径白名单等安全控制面变更。
- 涉及交易、实盘 allocation、券商/交易所执行、策略启停或高风险研究结论的变更。
- 连续失败、根因不明确、需要外部账号登录或外部服务确认的任务。

## 6. Dashboard 展示原则

Dashboard 只展示可解释的状态，不把不同健康口径合并成一个大指标：

- “服务健康”：在线接口可用性和延迟。
- “组织健康”：GitHub Actions / repo 级运行状态。
- “用量与额度”：GPT / Claude / Codex 和本服务内部估算。
- “有效性”：已登记变更的后验效果样本。
- “影子审计分歧”：AI 影子审计与确定性规则的差异。
- “自治决策 / 人工审计队列”：低风险自动动作和需要人工处理的任务。
