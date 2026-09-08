# 日报 AI 摘要接线与后续链路缺口

## 本次范围与版本

本轮从 AIAuditBridge `73eb1a2d89c25e98b7c8d493d8e6ea6d20e944fd` 接续。该基线的 Cursor 后端已由主任务发布并部署；本文件所述日报改动在交接时仍为本地未提交改动，**没有发布、部署或真实模型调用证据**。此前 Cursor 部署和 CI 不能代替本次日报验证。

本轮只接入现有日报消费入口，不修改风险判定、策略参数、候选晋级、通知权限或执行器。没有新增服务、registry、定时器、workflow、OIDC allowlist 或跨仓依赖 pin。

## 已接入的实际入口

`ops/quant-monitor/scripts/daily_briefing_pipeline.sh` 仍先生成日报，再调用 `scripts/consume_daily_briefing.py --dispatch`。仅显式设置 `QUANT_MONITOR_AI_SUMMARY=true` 时增加 `--ai-summary`。默认不调用 AI。

CLI 先执行既有规则告警分发，再由 `service/briefing_consumer.py:summarize_briefing` 增加独立的 `ai_summary` 输出。摘要成功、失败或额度延期都不改变原 `action`、通知分发和退出码。摘要不发新通知、不写 Issue、不提供晋级或交易授权。`--ai-summary --dry-run` 对这条摘要路径零认证请求、零模型调用；既有独立 `--dual-review` 的 dry-run 只限制分发，**不能将二者组合称为全流程零模型模式**。

摘要只消费规则分类时同一次读取的白名单快照：四个已知领域的源文件名、实际策略行状态计数、报告生成时间和策略观测日期。策略名称、账户、指标明细、错误原文、任意自由文本，以及报告中未经重新计数的 `summary` 不送模型。部分领域缺失时显式输出 `missing_domains`，不声称覆盖全平台。

报告生成时间 `report_generated_at` 来自 builder 的 `as_of`；策略观测日期来自各行 `as_of`，二者不互相覆盖。缺失或非法时钟、未来观测、不可用数据、空输入均不生成摘要。摘要的保守时效上限是报告 36 小时、策略观测 7 个自然日；这不是交易日校验，也不修改现有风险或告警有效期。旧记录的 critical 告警不会因摘要暂不可用而消失。

## 认证、模型与费用边界

- 必须存在 GitHub Actions 的 OIDC 请求环境；仅配置静态服务令牌会返回 `unavailable / github_oidc_required`，不会提交任务。环境变量存在不等于授权，SDK 仍领取 OIDC，服务仍验证身份、仓库和权限。
- 需要 `CODEX_AUDIT_SERVICE_URL`，以及 `AI_GATEWAY_SOURCE_REPO` 或真实 Actions 的 `GITHUB_REPOSITORY` 作为请求来源。缺配置受控返回 unavailable。
- 复用当前 SDK 的 `/healthz` 能力预检和 `/v1/ai/execute/jobs` 排队、轮询、完成校验，固定 `research_stage=research_summary`、`mode=review_only`，模型/推理等级由现有研究阶段策略和准入选择。
- 默认 `AI_GATEWAY_RESEARCH_PROVIDERS=codex`；可显式选择既有批准格式 `cursor` 或 `codex,cursor`。客户端声明不能越过服务侧 Cursor enabled、费用、目录新鲜度、额度和质量门。没有 `analyze` 或付费 API fallback。
- 完成结果必须匹配允许的 provider、研究阶段、model、effort 和成功输出；Cursor 订阅路由还由既有 SDK 绑定排队 job_id 及路由。失败不返回底层错误或候选建议；额度不足只保留白名单 `retry_at`，未知为 null，不即时重试。

## 线上尚未闭合的条件

现有 `codex-daily-briefing.service` 示例运行于 VPS 的专用镜像目录，timer 每日 22:30 UTC。源码配置只有报告/通知相关环境，没有 GitHub Actions OIDC。给这个 timer 增加摘要开关本身不能授权 `/execute/jobs`；静态 dashboard bearer 不可替代。主任务报告的已部署基线也未包含本次日报改动。

现有获准的 `codex_audit.yml`、`strategy_optimization_watcher.yml`、`portfolio_research_proposal_diagnosis.yml` 有 OIDC 能力，但没有消费 VPS 日报文件的已接通工件路径。本轮没有新建空 workflow 或伪造 token。真实接通还需在获准运行环境中提供真实日报输入与合法身份，采用本次 CLI/service 源码及支持研究路由的 SDK，再观察一次实际摘要执行。尚未执行以上步骤，不能报告“线上日报 AI 全链路成功”。

后续最小验收：部署精确版本时保留现有环境/凭据和开关默认值；先验证无身份返回 unavailable 且原告警正常；核验日报来源、日期和可用覆盖；在已有明确权限与费用边界内验证一次合法 OIDC 请求的真实任务结果。真实调用、workflow 或通知不是本次离线验收的一部分。

## 另外两条链路的已确认缺口（本轮未改）

1. **晋级审阅**：`scripts/run_dual_review_pipeline.py --from-evidence` 可调用 `service/dual_review_primary.py:run_codex_primary_review`。现有主审使用 `task=dual_review / complexity=high`，未设置 `research_stage=promotion_review`；`dual_review_orchestrator.py` 默认 secondary 为 `dual_api`，`dual_review_gateway.py` 调用 `client.analyze`。不能把这条旧链路归为 Codex-only，也未在本轮解除其既有人工/权限门。日报 builder 不产生 `primary_review` 字段，当前日报 pipeline 不启用 `--dual-review`，所以它也不是已经自动发生的日报晋级链路。
2. **监测到实验**：`strategy_optimization_watcher.yml` 的真实后续调用是最多一次 `run_research_task_diagnosis`，输出研究诊断评论和任务快照。`service/research_task.py` 固定目标 `diagnose_degradation`，`parameter_bounds_sha256=None`，验证器也要求它为 None；记录只有证据标识，不提供可执行实验所需的冻结数据、配置、参数范围和 runner 输入。当前不能据此自动跑回测或 shadow，更不能由模型臆造这些输入。下一切片应先绑定某个实际策略仓的可信输入和已有 runner，再考虑实验执行，保留 research_only/no_order/size_zero 和人工晋级边界。

## 验证材料

使用 `/usr/local/bin/python3`（Python 3.13.7，pytest 9.1.1），禁用外部 pytest 自动插件。首批新增入口用例在旧代码上实际得到 `3 failed, 3 passed`；新增完整 SDK/来源及 shell fixture 进一步复现来源仓库未绑定与 bash 空数组异常，最小修复后通过。结果和精确命令见本地 `docs/validation/2026-09-09-daily-summary/validation.json` 及对应日志。

实际测试通过 CLI 运行当前 SDK，用纯合成 OIDC/HTTP 响应覆盖领取身份、health、提交和轮询；没有真实网络模型或通知。这是源码和合同集成验证，不是实盘、真实回测、真实摘要或生产恢复证据。
