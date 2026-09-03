# QuantStrategyLab 组织通用约束

- 单仓、单分支、单 PR；默认最小写集，超过 5 个文件或 300 行新增先说明。
- 未明确授权，不新增服务、bot、workflow、依赖或状态系统。
- tests-first，沿用现有测试框架；禁止执行真实运行。
- CI 最多一次纠正；仍未收敛则进入 `DESIGN_REVIEW_REQUIRED`。
- candidate 不得自动激活 paper/live，不得触发 Scheduler、Broker、secret 或 production deployment。
- 不得以 AI confidence、title、comment 或 label 作为放行依据。
- Promotion Manifest 的激活、恢复、资金风险扩大和权限变更必须人工批准。
