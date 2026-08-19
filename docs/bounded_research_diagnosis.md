# 自动受限研究诊断

状态：`IMPLEMENTED_NON_LIVE_RESEARCH_DIAGNOSIS`

这是 AIAuditBridge 在策略监测之后的一个小闭环，不是策略执行器。

## 自动做什么

每天的 `Strategy Optimization Watcher` 先从受信任的两个 P3 脱敏绩效
工件构造比较。只有比较结果同时绑定 P1 输入摘要、P2 冻结配置摘要、P3
证据 ID、策略 revision 与 producer revision 时，才会生成
`qsl.research_task.v1`。

对每个尚未诊断的 Issue，调度器每次最多处理一个任务：

1. 重新验证任务的完整 JSON 形状、canonical SHA-256 和固定 no-order
   authority；
2. 调用既有 AI Gateway 的文本分析接口；
3. 在原 Issue 写入一条带幂等标记的中文诊断和下一轮**离线**研究建议。

普通退化不发送 Telegram。运行数据不可用、证据记录失败、熔断或其他
运行风险仍走 VPS quant-monitor 的去重 Telegram 路径；因此人工只收到
需要及时处理的运维/风险信号，而不是每一条策略波动。

## 明确不做什么

- 不读取 raw bars、账户、凭证或订单；
- 不运行回测，不修改代码、参数或配置；
- 不创建 PR，不部署，不启动 paper 或 shadow；
- 不授权 P4、P5 或 P6；P6 仍必须由所有者明确决定。

AI Gateway 不可用、任务不完整或 Issue 评论失败时，调度器不采取替代动作；
已有 Issue 和受限任务仍保留为下一次计划运行的审计起点。

## 启用条件

代码合并后，VPS Codex service 必须通过受控的 `VPS Codex Service Ops`
部署一次，才能将精确的
`strategy_optimization_watcher.yml@refs/heads/main` OIDC 身份加入 allowlist。
仓库还须已有 `CODEX_AUDIT_SERVICE_URL` secret；若不存在，调度器会记录
`not_configured` 并安全跳过，不影响 watcher 的 Issue/任务索引行为。
