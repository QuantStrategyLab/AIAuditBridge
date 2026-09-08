# Cursor 研究后端接入（2026-09-09）

本轮复用现有 AI gateway HTTP、OIDC/服务认证、异步任务、去重、额度存储与模型目录。默认请求仍走 Codex。Cursor 是另一执行后端，下面可选多个供应商的模型；不是把 Grok 当成整个 Cursor，也不把 Cursor 结果标成 Codex。

## 接口和采用

`POST /v1/ai/execute/jobs` 增加 `allowed_providers`：只接受 `["codex"]`（缺省）、`["cursor"]`、`["codex", "cursor"]`。Cursor 要求 `review_only` 与有效 `research_stage`。新 `/healthz` 能力为 `subscription_research_routing=v1`；旧 `codex_research_routing=v1` 保留。

准入后固定 `provider/research_stage/model/reasoning_effort`，写入任务及公开状态，参与去重。SDK 新路由要求提交回执与每次 poll 的任务编号及四字段一致；显式型号和推理等级不能静默替换。旧请求没有允许 Cursor 的字段，仍只能提交 Codex；新 SDK 也拒绝旧 Codex 请求收到的 Cursor 结果。

Codex→Cursor 仅在请求显式允许、服务 `AI_GATEWAY_CURSOR_FALLBACK_ENABLED=true`、Codex 准入确认未开始且额度预留/账户能力不可用时选择。显式型号不跨后端换型。启动后超时、失败、结果缺失、通信不明不会触发另一后端；没有 API fallback。已有 `analyze/review` API consumer 保留原用途与预算保护。

AAB 两个实际调用者 `run_research_task_diagnosis.py` 与 `run_portfolio_research_proposal_diagnosis.py` 通过 `AI_GATEWAY_RESEARCH_PROVIDERS=cursor` 或 `codex,cursor` 显式采用；默认 `codex`。其权限、去重、输出验收与 advisory 定位保持。SDK 安装和 QPK consumer 的采用必须单独验证，源代码可用不等于已安装。

## 模型与费用

目录新增 `subscription_rosters.cursor`，完整保留目标服务账户 CLI `models` 的实际型号列表，来源 `cursor_cli_account`、成功时间 `updated_at` 独立于 API 目录。官方 CLI `status/models` 仅由目标已认证客户端消费认证，不复制凭据或保留账号身份。未知新型号可以显示，但没有配置的质量和推理映射不能自动使用。OpenAI/Anthropic API 列表不构成 Cursor 订阅能力证据。

`policy.example.json` 是未激活示例，仅列根任务确认在目标 roster 中的 Grok 4.6 非 fast 四档。policy 可添加任意已审查精确 alias；每个 alias 对应一个明确 effort，运行时直接传 alias，不再追加参数覆盖。研究摘要/偏离分析/优化/晋级审阅的质量下限分别为 0/1/2/3，最低 effort 为 low/medium/high/xhigh；输入复杂度可抬高下限。映射是人工审查的调度配置，不宣称模型质量已通过真实 A/B 评测。

Cursor 官方区分 Cursor Models 和 Other Models 两个消费池；第三方模型从 Other Models 按对应模型 API 单价消耗额度，超额可另外付费。型号可见不证明余额，也不证明免费。见 [Cursor 模型与价格](https://cursor.com/docs/models-and-pricing)。

启用还要求 policy 的 `on_demand_disabled_verified=true`、未过期 `valid_until` 和全账户 `max_daily_calls`。默认示例未确认费用、已过期，不能执行。额度计数复用持久存储，跨 HTTP 请求串行准入与预留；计数区分 Cursor/Codex，Cursor 实际费用和余额保留未知，不能报为 0。损坏/缺配置的额度存储拒绝 Cursor；这些门不授予交易或候选晋级权限。

## 目录刷新

`python3 scripts/sync_model_catalog.py --subscriptions-only` 只调用 Cursor 的只读账户/型号命令，不读 API 目录凭据、不调用付费模型。API 与 CLI 更新共享文件锁，锁覆盖重新读取、合并和原子保存，互不覆盖对方字段。刷新失败保留 last-known-good 的 models 和原 updated_at，并标 stale；stale/未来时间/超过 24 小时快照拒绝准入。

`deploy_cursor_research.sh install` 安装未激活 policy/env 和每天 06:20 UTC 的 CLI 刷新 oneshot/timer。原每月 API model-catalog timer 保持。两者复用同一脚本和目录，没有第二个 HTTP 服务。安装脚本不登录、不执行模型、不重启 gateway、不覆盖已存在配置。首次目录刷新可由部署端显式启动该 oneshot。

## 运行约束与部署验收

执行器每次新建临时任务工作区，复制服务拥有的 AGENTS 和研究真实性、故障验收两个 skill；这些文本与任务输入一起通过 stdin 提交。source_repository/source_ref 仅为来源元数据，不声称已 checkout 或读取对应代码。只接受 CLI 成功终态 `type=result/subtype=success/is_error=false` 的非空 result；失败固定脱敏。参见 [CLI 参数](https://cursor.com/docs/cli/reference/parameters)、[输出协议](https://cursor.com/docs/cli/reference/output-format)、[权限配置](https://cursor.com/docs/cli/reference/permissions)。

当前固定 ask、sandbox enabled、禁自动更新；项目权限拒绝 Read/Shell/Write/MCP/WebFetch。额外传 `--allowed-tools "" --exclude-workspace-context`，并将规则与证据直接放入 stdin。AGENTS 和 skill 是任务指导；allowed-tools 是经 CLI 发送的服务端 no-tools 限制，不声称完整本地 OS 隔离。独立材料审查指出标准 Grep/Ls 不都消费 Read deny，原生 sandbox 默认 system read 不能描述成 workspace-only。根任务决定此最小首期可先部署 `AI_GATEWAY_CURSOR_ENABLED=false`，先验收无模型服务及目录刷新；自动启用仍须费用确认和实际 canary 的零工具调用验证。不通过不启用，不因沙箱失败改为 disabled。

部署需成套安装 service、scripts/sync_model_catalog.py、ops/cursor-research/workspace，以及新 SDK；保留现有 OIDC、secret、allowlist、drop-in 和旧 release。只复制 adapter 文件不足以接通。至少验收：旧 Codex 请求兼容；未认证/未确认费用 Cursor 零启动；目录只读刷新成功及失败保留 stale；SDK 错任务或错 route 拒绝；未授权来源在准入前拒绝；一旦启动失败不切换；授权的单次合成 advisory 才能验证实际 CLI，不把离线 mock 当真实执行成功。

## 本轮离线验证

真实安装 SDK 到隔离临时 venv 后，15 个相关测试文件通过：255 tests、109 subtests。覆盖新旧接口、任务身份、Codex 准入回退、Cursor 输出和失败面、模型目录、额度、两个实际诊断调用者、已有健康/恢复/API 预算等。精确命令、解释器和脱敏日志保留在 `docs/validation/2026-09-09-cursor/validation.json`。首次接口回归 9 项 RED；模型目录刷新缺入口 1 项 RED；Cursor 计数缺入口 1 项 RED；调用者真实 SDK mock 接线 1 项 RED；研究错误透出 3 项 RED。以上是离线验证，未运行真实模型或写 Issue/通知，也未由本 writer commit/push/部署。

## 独立审查后的增量（最终冻结）

上述 255/109 是初版完整接线验证；审查后修复以 `docs/validation/2026-09-09-cursor/review-increments.json` 的最终 70 tests / 34 subtests 为准。每项先实际复现，再最小修复：

- 安装脚本仅在配置目录不存在时创建，不改已有 owner/mode；临时 0700/0750 目录及内容得到保留。
- 旧 `/review` 固定 Codex verifier 的记账不再取未经准入的 payload.provider；Cursor 或任意字符串不能错扣/中断该既有路径。
- 异步接收在同一锁内先用原始请求身份查活动任务，再检查并发、准入、预留和创建。重复请求返回原任务及原 route，不重新准入、不重复扣额，也不因新额度状态从已存在 Codex 任务再派 Cursor。原始请求身份保留阶段、显式型号/effort、允许后端、复杂度及来源，客户端声称的 provider 不算选择结果。
- 新研究请求空/auto 型号和 effort 使用专属阶段表，不继承旧非研究执行的全局模型环境。真实部署中的旧 `gpt-5.4` 默认无需修改；显式研究型号仍接受校验或延期，不能静默换型。

独立 reviewer 对重复请求的纯内存复核得到 3 次 202、后 2 次去重，只有 1 job、1 Thread、1 admission 和 1 Cursor 计数。结果仍为离线材料验收；生产保持未激活直至根任务分别验证部署、目录和后续授权 canary。
