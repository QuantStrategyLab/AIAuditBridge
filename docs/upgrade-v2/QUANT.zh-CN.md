# QuantAIService 2.0

本次升级的通用任务服务由 `personal-ai-service==2.0.0` 提供，QuantAIService 只保存 Quant 项目身份准入和部署配置。新入口是 `quant-ai-service`；客户端使用 `ai_service.client.TaskClient` 的 submit、get、message、cancel、wait，不提供旧 `AiGatewayClient` API 兼容层。

示例 `config/quant.example.json` 默认关闭，只有 synthetic 项目身份与 placeholder。真实连接需要批准云端的 HTTPS 入口、已有 OAuth 身份提供方、助手独立连接身份，以及 Dot 订阅或 Grok Routine Webhook。当前没有注册真实助手、配置账户、部署、调用模型或启用按量付费。

AI 服务的 task/read/work scopes 只授权任务传输与 advisory 结果回传，不能授权交易、账户权限、风险预算、自动合并或部署。Bot/Dot 的实际工具与云电脑权限必须在产品端独立控制；项目配置不能替代云电脑隔离。

SOXL、Global ETF、CN index ETF 的 prompt、冻结输入、来源校验、codegen 路径与运行条件已在本地迁到对应策略仓库；LongBridge 特定修复规则迁到平台仓库；生命周期及通用审计由调用方构造任务。真实市场与研究数据继续只在批准云端处理，本机只有源码和 synthetic 测试。

旧 `service/`、`adapters/`、`client/`、模型部署和固定业务 workflow 已从活动源码删除；旧源码见 Git 基线及 [退役说明](RETIRED-V1.zh-CN.md)。云端尚未切换，本改动还不是生产发行版。`personal-ai-service` 和 `quant-ai-service` 2.0 尚未发布，离线验收只使用本次源码构建的 wheel，正式采用前须固定发布来源与摘要。历史审计、producer ID、来源 hash 和冻结记录保持原样。

接口及产品文档依据见共享实现的 `docs/upgrade-v2/IMPLEMENTATION.zh-CN.md`。

现有云端已配置 GitHub 看板登录，但尚未提供原生助手所需的完整 OAuth 授权能力。当前配置与可复用范围见 [登录配置核对](IDENTITY-INVENTORY.zh-CN.md)。

QuantPlatformKit 的生命周期客户端已在本次隔离工作区迁到 V2 任务接口，移除该模块的旧 VPS 路由和 provider fallback。研究中的 pending/outcome_unknown 保留原任务身份，恢复时只查询原任务；相关 226 项 synthetic 测试通过。该改动尚未发布，策略调用方现有依赖不会自动升级。SOXL、Global ETF 等 runner 已迁入策略仓，冻结工程材料审查迁入 QPK。

SOXL 新研究、codegen、手动学习与 Global ETF advisory 审查已在本地策略工作区迁移；当前 107 项 synthetic 测试通过，另有真实 Docker 集成项未运行。SOXL 确定性解释独立脚本另有 8 项通过，真实冻结研究集成未在本机运行。共享任务格式由 QPK 校验，SOXL 参数 policy 由 UES 校验。

CN 的本地研究入口已迁入 CnEquityStrategies，保留保护政策、来源、每日准入、前向观察和人工边界；证据审查也已改用三角色 V2 quorum，并删除其旧 SDK 依赖与看板仓库 checkout。相关 108 项离线测试通过。旧 AAB workflow 已退役，新业务 workflow 位于所有者仓库且默认关闭；不能据此切换生产服务。

## 收尾验证

当前新服务及保留的日报/令牌单元测试共 67 passed；监控和日报联合选择集另有 123 passed + 47 subtests（包含重复测试，不能相加）。通用 watcher/漂移/工程材料相关 QPK 选择集 109 passed + 49 subtests，LongBridge 3 passed。五个本地 wheel 已重建，隔离导入验证不访问源码 checkout。新的 CI 以批准的 shared/QPK 40 位源版本运行测试，发布资格未启用时只构建未发布包装器；尚未运行 GitHub CI。

监控的部署资格、锁定依赖、准确发布源版本及真实云端运行仍需下一阶段验证。旧部署脚本默认拒绝激活，不把旧 QPK mirror 当成新版依赖。现有 Cloudflare 看板尚未适配 V2，也不是 V2 API 入口。
