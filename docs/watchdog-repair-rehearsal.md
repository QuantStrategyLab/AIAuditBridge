# 固定 watchdog 修复演练

本入口只验证一个已发生、已修复的无资金故障：UESP #489 中，前一 watchdog
自身的 failure 被当作新一轮调度失败，导致告警逐日继承。生产修复已存在，
本演练不会将旧代码重新部署，也不代表自然故障自动修复或自动上线已经通过。

## 边界与运行

复用 `codex_audit.yml`，仅 main 的人工触发 `watchdog_repair_rehearsal=true`
可运行，其他任务互斥；不增加定时器、Git 写权限或服务部署。
只允许首次 run attempt，失败或结果不明时不自动重放。

流程是固定历史输入 → Codex 诊断并选择已审修复动作 → 临时目录应用固定源码 →
程序验证 → 保存演练结果工件。AI 的输出只用于选择动作，不能指定文件内容、
命令、路径或生产目标；源码来自注明版本并校验内容的历史 fixture。
已审修复仅放行已完成但失败的旧 watchdog 心跳，同时保留其 failure；
研究失败、watchdog 缺失、取消、超时仍须拒绝。

沿用 GitHub OIDC 和服务端模型/额度准入，固定 Codex、`drift_analysis`、
`review_only`、`read-only`。本机离线检查与模拟响应测试不算真实模型调用。
真实调用失败、延期或 AI 不选择该动作时，输出明确结果并停止；不调用付费 API。

发布范围仅为 GitHub Actions 的 `watchdog-repair-rehearsal.json` 工件，保留 7 天。
不会创建候选、Issue、PR、通知、订单或人工决定，不授予长期自动合并/部署权限。
后续真实故障接入必须有具体事故、已审适用动作和目标恢复条件，不能从此演练外推。

## 日报及诊断的实际状态（2026-09-11）

- `AI_DAILY_SUMMARY_ENABLED=true` 已精确读回。既有每日 22:45 UTC（北京时间次日
  06:45）工作流消费 VPS 22:30 UTC 已生成的报告，定时器和原通知逻辑保持原样。
- [run 34563189573](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/34563189573)
  实际消费 2026-09-10 日报并返回 `available`，Codex `gpt-5.6-luna / low`；
  摘要仅供参考，其他任务 skipped。这是启用后的单次人工验收，后续自然定时周期另计。
- 现有诊断开关为 true；最近自然
  [run 34546891381](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/34546891381)
  工件为 `skipped / no_data_errors`，没有伪造故障来触发模型。
- 固定身份保护历史诊断
  [run 34439482142](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/34439482142)
  工件为 `succeeded`，已复核原证据，不重复调用。该结果属于历史演练。
