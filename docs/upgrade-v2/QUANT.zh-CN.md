# QuantAIService 2.0

本次升级的通用任务服务由 `personal-ai-service==2.0.2` 提供，QuantAIService 只保存 Quant 项目身份准入和部署配置。新入口是 `quant-ai-service`；客户端使用 `ai_service.client.TaskClient` 的 submit、get、message、cancel、wait，不提供旧 `AiGatewayClient` API 兼容层。

示例 `config/quant.example.json` 默认关闭，只有 synthetic 项目身份与 placeholder。真实连接需要批准云端的 HTTPS 入口、已有 OAuth 身份提供方、助手独立连接身份，以及 Dot 订阅或 Grok Routine Webhook。当前 Quant 私有 Dot 插件已登录并绑定 QuantStrategyLab/UsEquityStrategies，真实订阅挑战、直接读取/领取/回传公开合成任务已通过；自动事件触发尚未验收，未启用按量付费或运行真实业务任务。

AI 服务的 task/read/work scopes 只授权任务传输与 advisory 结果回传，不能授权交易、账户权限、风险预算、自动合并或部署。Bot/Dot 的实际工具与云电脑权限必须在产品端独立控制；项目配置不能替代云电脑隔离。

SOXL、Global ETF、CN index ETF 的 prompt、冻结输入、来源校验、codegen 路径与运行条件已在本地迁到对应策略仓库；LongBridge 特定修复规则迁到平台仓库；生命周期及通用审计由调用方构造任务。真实市场与研究数据继续只在批准云端处理，本机只有源码和 synthetic 测试。

旧 `service/`、`adapters/`、`client/`、模型部署和固定业务 workflow 已从活动源码删除；旧源码见 Git 基线及 [退役说明](RETIRED-V1.zh-CN.md)。两个服务 2.0.2 已通过正式 GitHub Release 发布并部署，采用时固定 release 的准确源码和 SHA256 清单；私有核心 wheel 只位于私有 release。生产调用方准入仍未开启，旧生产进程等待实际业务调用链验收后停用。历史审计、producer ID、来源 hash 和冻结记录保持原样。

接口及产品文档依据见共享实现的 `docs/upgrade-v2/IMPLEMENTATION.zh-CN.md`。

新服务使用独立 Keycloak realm、固定公开客户端与 PKCE S256，并已通过真实 GitHub OAuth；邮箱只用于云端登录，不进入 task JWT 或任务材料。旧看板配置的初次核对见 [登录配置核对](IDENTITY-INVENTORY.zh-CN.md)。

QuantPlatformKit 的生命周期客户端已在本次隔离工作区迁到 V2 任务接口，移除该模块的旧 VPS 路由和 provider fallback。研究中的 pending/outcome_unknown 保留原任务身份，恢复时只查询原任务；相关 226 项 synthetic 测试通过。QPK 与 CN/UES 的迁移已合并并发布，生产业务 workflow 不会因此自动启用。SOXL、Global ETF 等 runner 已迁入策略仓，冻结工程材料审查迁入 QPK。

SOXL 新研究、codegen、手动学习与 Global ETF advisory 审查已在本地策略工作区迁移；当前 107 项 synthetic 测试通过，另有真实 Docker 集成项未运行。SOXL 确定性解释独立脚本另有 8 项通过，真实冻结研究集成未在本机运行。共享任务格式由 QPK 校验，SOXL 参数 policy 由 UES 校验。

CN 的本地研究入口已迁入 CnEquityStrategies，保留保护政策、来源、每日准入、前向观察和人工边界；证据审查也已改用三角色 V2 quorum，并删除其旧 SDK 依赖与看板仓库 checkout。相关 108 项离线测试通过。旧 AAB workflow 已退役，新业务 workflow 位于所有者仓库且默认关闭；不能据此切换生产服务。

## 收尾验证

当前新服务及保留的日报/令牌单元测试共 67 passed；监控和日报联合选择集另有 123 passed + 47 subtests（包含重复测试，不能相加）。通用 watcher/漂移/工程材料相关 QPK 选择集 109 passed + 49 subtests，LongBridge 3 passed。五个本地 wheel 已重建，隔离导入验证不访问源码 checkout。新的 CI 以批准的 shared/QPK 40 位源版本运行测试，发布资格未启用时只构建未发布包装器；正式 actionlint、test、admitted-service GitHub CI 已通过；2.0.2 共享核心 56 项测试和 wrapper 3 项边界测试通过。

监控的部署资格、锁定依赖、准确发布源版本及真实云端运行仍需下一阶段验证。旧部署脚本默认拒绝激活，不把旧 QPK mirror 当成新版依赖。现有 Cloudflare 看板尚未适配 V2，也不是 V2 API 入口。

## 当前验收边界（2026-10-08）

Quant v2.0.2 的准确源码为 `8e771462fd14e4552d6add55b2920f6bd20dc0ee`，共享核心为 `8b36ee8137c8cfa3900f9120066990c71ab23246`。Dot 的 scopes 只有 tasks:read、tasks:work、events:subscribe，不包含 submit、仓库写入、交易或部署。

本轮两个公开合成任务回传 completed，revision=1、output={"sum":12}；由人工在 Dot 聊天中给定真实任务 ID 触发，不能代替自动事件验收。Webhook 获 2xx 时两个监控的 last_run_time 仍为空。自动调度、追加材料/取消的原生验收、Grok Routine 和真实业务资格仍待验证。原生模型不能从 webhook 接口确认，因此未声称使用指定 Astra 模型。
