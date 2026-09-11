# 自动受限研究诊断

状态：`IMPLEMENTED_NON_LIVE_RESEARCH_DIAGNOSIS`

这是 AIAuditBridge 在策略监测之后的一个小闭环，不是策略执行器。

## 2026-09-11 SOXL 自然研究接严格验证

本次新增的 `run_watcher_validation` 接续既有 SOXL watcher learning：从原 Issue
中相同 GitHub App 写入、同一任务且成功的 learning 终态重新构造开发摘要，核对
P1 输入身份后，将摘要及其 canonical SHA-256 交给 UESP 既有验证入口。
它只执行已固定的 0.65 基准 / 0.55 候选、5/10/15 bps、三组 walk-forward
窗口和 2025-08-04 至 2026-08-04 OOS；不选择新参数，不把原人工任务的结果
改绑到自然任务。严格校验继续使用 QPK `7363011d56926d39f4fffeb036e511391114e39f`。

验证开始前在原 Issue 写 started 标记；已有成功终态会重新核对来源和严格门后复用，
已知失败或只有 started 的未知结果不会自动重跑。结构性验证通过仍为
`promotion_eligible=false`，只提供收益/回撤对照和待人工质量判断，不启动 shadow、
创建晋级票据或变更实盘。

发布顺序是先发布 UESP 新入口，再发布 AAB 接线，最后将 AAB 仓库变量
`SOXL_WATCHER_VALIDATION_CONSUMER_REVISION` 绑定已验收的 UESP 完整 40 位 commit。
变量为空时保持原 learning 流程；分支名或非法 revision 会在任务接续前拒绝。
已固定的 learning consumer 与 UES runtime 不变，验证 consumer 和 QPK strict gate
各用独立环境。单个任务保持最多一次 learning 数值调用和一次 validation 调用；
每次调用原 1200 秒上限不变，整个 job 上限从 35 调整至 55 分钟以容纳串行两阶段。
结果仍写回原 Issue，并保留独立的 `soxl-watcher-validation-<run>-<attempt>` 工件。

本入口已完成本地实现和合成验证；生产是否启用以实际仓库变量和 workflow 读回为准。合成跨仓联调验证了
AAB 摘要 → UESP CLI → 24 次固定数值测试 → QPK 六项严格门 → 原任务终态复用；
P1 材料和数值回放为测试替身，不构成真实数据、自然事件或策略晋级验收。

## 自动做什么

每天的 `Strategy Optimization Watcher` 先从受信任的两个 P3 脱敏绩效
工件构造比较。只有比较结果同时绑定 P1 输入摘要、P2 冻结配置摘要、P3
证据 ID、策略 revision 与 producer revision 时，才会生成
`qsl.research_task.v1`。

对每个尚未诊断的 Issue，调度器每次最多处理一个任务：

1. 重新验证任务的完整 JSON 形状、canonical SHA-256 和固定 no-order
   authority；
2. 调用既有 AI Gateway 的 Codex execute 通道（`review_only`、请求携带已冻结策略 revision、最多 600 秒），不使用付费 analyze/review API，也不在失败时回退到 API；
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

## 组合研究线索诊断

`portfolio_research_proposal_diagnosis.yml` 是与上述 P3 退化诊断分开的低频
消费者。它只从 `UsEquitySnapshotPipelines` 下载已保留的、脱敏的
`qsl.portfolio-candidate-readiness.v1` artifact；它不读取行情、GCS、凭证、账户
或订单。

只有 artifact 自身以 canonical SHA-256 通过校验，并且两个单策略均为 `P1
ACCEPTED`、P3 `COMPLETE`、且共用同一 cutoff 时，才会寻找上游已创建的同一条
readiness Issue。每个 `(proposal_id, readiness_sha256)` 最多获得一条 AI 评论。AI
只能列出组合候选设计问题与独立证据缺口；它不得选择具体权重、创建 P2、共同 P1
root 或组合 P3，也不得启动回测、paper、shadow、live 或订单。

该 workflow 的 OIDC 身份会在下一次受控 VPS Codex service 部署后才进入精确
allowlist。部署前或 AI Gateway 未配置时，它只记录 `not_configured`/`unavailable`
并安全退出；不会采取替代动作。

## 启用条件

代码合并后，VPS Codex service 必须通过受控的 `VPS Codex Service Ops`
部署一次，才能将精确的
`strategy_optimization_watcher.yml@refs/heads/main` OIDC 身份加入 allowlist。
仓库还须已有 `CODEX_AUDIT_SERVICE_URL` secret；若不存在，调度器会记录
`not_configured` 并安全跳过，不影响 watcher 的 Issue/任务索引行为。

## 2026-09-08 HITL 接线状态

本次偏离研究链采用 Codex only：Codex 负责根因假设和研究建议，现有 Python
优化器与 BacktestOrchestrator 执行数值计算；严格 WFA/OOS 和 paired shadow
通过后才进入网站等待人工，接受仅记意图。其他单独授权的 API 场景保持独立。
本页 watcher 的诊断阶段仍只生成研究评论，未绑定真实候选的研究执行 job；
不能把 Codex 返回、模拟测试或建议评论写成已完成真实回测/shadow。

`source_ref` 是请求与作业元数据；当前 execute 服务不按它检出源码，诊断只消费上述脱敏摘要，不能声称已读取对应 revision 的代码。

## Codex 研究任务的模型与额度准入

以下为 2026-09-08 的本地实现，尚未发布或部署。新研究请求通过 `research_stage`
声明阶段，服务按阶段下限与请求复杂度中较高者选择；这不新增执行或交易权限。

| 阶段 | 默认模型 | 推理等级 | 当前消费者 |
| --- | --- | --- | --- |
| `research_summary` 摘要整理 | gpt-5.6-luna | low | 预留，未增加调用 |
| `drift_analysis` 根因分析 | gpt-5.6-terra | medium | watcher 诊断 |
| `optimization` 优化建议 | gpt-5.6-sol | high | QPK 优化决策 |
| `promotion_review` 候选材料复核 | gpt-6-astra | xhigh | 预留，未替代严格验证门 |

这些是部署默认值，不表示每个账户都有这些模型，也不表示模型能证明策略有效。
实际可用性和推理选项来自**执行主机** Codex app-server 的
[`model/list`](https://learn.chatgpt.com/docs/app-server)，不使用付费 API 模型目录。
显式模型/推理设置须同时满足阶段下限、主机支持和额度要求；不满足就延期。
复杂度可以提高配置，剩余额度不会降低质量下限。

额度通过同一主机的 `account/rateLimits/read` 获取，不把本机桌面余额当作 VPS 余额。
研究快照须在 180 秒内且模型清单完整；看板的短超时不能使不完整快照长期阻塞研究。
初始准入保留每周 30% 余额供账户所有者使用；优化建议只在周余额超过 50% 时启动；
短窗口还保留 20%。已有窗口分别检查，缺失窗口不是零使用量，不同模型额度桶不相加。
这是保留额度的启发式准入，不能预测或保证单次任务消耗；官方说明消耗随模型、上下文
和任务复杂度变化，见 [Codex pricing](https://learn.chatgpt.com/docs/pricing)。

额度不足返回 HTTP 429 / `status=deferred` / `retry_at`。已知余额不足时给出相关窗口
重置时间，信息缺失时为 null。客户端不轮询、不调用 API、不把延期计为模型服务故障；
watcher 保留未完成任务，QPK 将研究状态记为 `deferred`，不误写成“不建议优化”。
`retry_at` 是最早重新检查的提示，不是自动启动承诺；每次仍须重新检查证据和额度。

新客户端在提交前检查 `/healthz` 的 `codex_research_routing=v1` 能力，旧服务不接收
这类研究提交；完成后还检查阶段、模型和推理信息。部署顺序为服务 → SDK → QPK/工作流
消费者。安装了不支持新参数的旧 SDK 时停止该调用，不移除参数重试或回退 API。
研究作业去重包含阶段、模型、推理等级，升级请求不会复用较低配置的旧作业。

当前未增加日历优化、15 分钟排班、跨运行的研究队列、每日候选上限或新模型调用阶段。
既有 watcher 排班继续使用原设置；“一次重研究、每天至多一个新候选”的总量限制和
断点恢复须在真实候选执行 job 绑定后接入。普通数值回测与 shadow 采样由现有程序执行，
无需等待中的 Codex 任务持续解释；严格 WFA/OOS、paired shadow 和人工门保持原标准。

## 后续 AIAuditBridge 专项优化与审计

日报摘要沿现有 22:30 UTC 报告时段选择日期；GitHub 排班延迟到次日时仍消费该时段的报告，
不会仅因执行日改变而读取尚未生成的目录。缺失、无效或过期报告仍由原消费者拒绝，
不回退到更旧数据；此修正不打开摘要开关、触发调用或改变 OIDC/Codex-only 约束。

用户已提出后续单独审计 AI 服务仓库。本次仅接通上述研究路径，后续范围为：

- 按任务所需代码/工具访问、时延、上下文规模、质量评测及可用额度分配 Codex 与 API；
- 研究/调参/回测编排保持 Codex only；低延迟、短文本结构化等 API 场景须有明确费用额度；
- 检查路由与回退是否遵守场景授权，额度桶、成本记录、并发、超时、重试与断点恢复是否一致；
- 用固定输入比较模型输出质量、实际消耗和失败率，再调整模型/推理配置；
- 复用既有实验记录做重复任务合并、无效方案记忆、结果缓存、shadow 到期提醒与人工决策摘要。

智能分配不等于 API 自动补位。当前未扩大 API 费用权限，未创建新任务、自动化或排班。

## SOXL 固定收益归因

`VPS Research Input Readback` 的显式 `operation=soxl_attribution` 复用原专用 OIDC、
固定四成员 P1 输入及隔离策略环境。只执行一次固定研究；默认仍是 `readback`，不增加调度。
该模式不调用模型服务，也不生成晋级、shadow 或账户决定。解释可由 Codex 基于产出的数字完成。

固定比较 `baseline_mid_065`、`soxx_buy_hold`、`fixed_full_weights`，各使用原 5/10/15 bps
成本假设和截至 2025-07-31 的同一开发窗口。只保存资产美元收益、初始本金口径的贡献百分点、
交易模型成本、总收益及回撤等聚合字段；不保存逐日行情、持仓或收益序列。输入临时副本按原流程清理。
每组需满足期末减期初权益等于资产贡献减成本，三组的窗口和初始资金一致；缺项、非有限数、
来源不符、残差超限或超时均停止，不重跑、不换源。

本研究使用已经看过的历史窗口，标为 `retrospective_research`；成交成本、现金零利息及外部流为
原模拟假设，不冒充真实券商账务。资产收益贡献不等于经济因果证明，各规则间的差值也不能相加
当成唯一的因子贡献。该结果只能解释当前固定对照，不用于重调旧候选或宣称新的样本外胜出。
