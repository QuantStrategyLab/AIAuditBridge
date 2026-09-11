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

## 近期异常诊断覆盖（2026-09-11）

每日诊断入口改为 `health_cycle.py --diagnose-recent`。它读取最近 24 小时已保存的
监测文件，选择一个尚未尝试诊断的异常分类集合；即使最新监测已不含该异常，仍可
分析其原因。模型和结果工件同时带原观测时间、最新监测时间，以及“仍观察到／部分
观察到／最新记录未观察到”的区别。未观察到不等于账户或交易已恢复。

原 `--diagnose-latest` 接口保留。近期入口仍要求最新记录在两小时内、内容合法，
每个文件不超过 1 MiB，窗口最多 512 个文件；输入、时间或去重状态损坏时拒绝，
不会退回更旧记录来假装当前状态可用。它不采集数据，不重跑监控，不发通知。

沿用原诊断消费者独立状态文件和异常集合去重。新增 UTC 日期只用于限制该每日
消费者每天最多一次 AI 请求；成功、失败、结果未知或额度延期均不会释放当天次数。
缺 OIDC/服务配置时不占次数。已有异常集合尝试过就不重放；并不新增事件队列或
承诺分析每次复发。多个未诊断集合按最新优先，超出窗口的事件不自动补跑。

每日调度频率、Codex-only、只读权限和人工发布边界保持原样。回归测试使用合成
文件及 SDK 替身，CLI 无 OIDC 验证不调用模型；源码验收不能替代后续真实异常验收。
