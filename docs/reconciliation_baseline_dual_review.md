# 旧实盘基线：强制双 AI 审计

本流程处理已经获授权运行、但由于迁移、配置漂移或运行异常而进入
`RECONCILE_ONLY` 的旧实盘实例。它不是策略研究晋级流程，也不会修改订单、仓位或
资金。

## 输入边界

平台适配器只能在只读 broker 会话中生成 QPK 的
`broker_reconciliation_evidence.v1`。原始账户、现金、持仓、订单和成交明细必须留在
受控环境；传入审计的只可以是 QPK 生成的
`broker_reconciliation_baseline_candidate.v1` 中的摘要和时间窗。

候选至少需要两次间隔采样且所有状态摘要一致。候选的内容地址
`candidate_sha256` 是后续审计和控制面操作的唯一绑定值。

## 审计门槛

调用 `run_dual_review_pipeline.py --trigger reconciliation_baseline` 时，必须提供：

- `strategy_profile`；
- `reconciliation_candidate_sha256`；
- 不包含敏感资料的候选摘要，例如来源收据数量和观察窗口。

该触发器不会采用“主审置信度足够即可放行”的普通优化。它始终运行 Codex 主审与
独立 GPT/Claude 复审；缺少候选绑定、任一审计不可用、结论分歧或拒绝都不能通过。
输出中的 `evidence_binding_sha256` 必须与候选的 `candidate_sha256` 完全一致。

## 私有控制面职责

审计通过仍不等于恢复实盘。只有私有、受权限保护的控制面可以：

1. 验证候选和每份来源收据来自受信任的运行时，且内容地址未变；
2. 验证双审结果为通过，并绑定同一个候选摘要；
3. 在同一 runtime target 上原子写入五个预期状态摘要并转换到 `ACTIVE_LKG`；
4. 保存不可变审计记录、操作人/服务身份、时间和目标版本，以便回滚和复核。

任一条件不满足，控制面必须保持 `RECONCILE_ONLY`。AIAuditBridge 不持有券商密钥，
也不拥有下单或直接修改 Cloud Run runtime target 的权限。
