# QuantAIService

QuantAIService 2.0 保存 Quant 项目准入策略，复用 PersonalAIService 的通用 API / 常驻助手任务服务。策略 prompt、证据、数字执行和采用条件由调用方管理。

当前为 AIAuditBridge 的本地未发布升级。新命令为 `quant-ai-service`，示例配置默认关闭。旧 VPS 模型进程、SDK 和 V1 接口已退役。

- [升级说明](docs/upgrade-v2/QUANT.zh-CN.md)
- [旧入口退役](docs/upgrade-v2/RETIRED-V1.zh-CN.md)
- [登录配置核对](docs/upgrade-v2/IDENTITY-INVENTORY.zh-CN.md)

`ops/quant-monitor` 保留日报消费和发送；迁移后需要批准的 V2 客户端和 QPK 环境。真实 OAuth、Dot/Grok 连接、发布、云端切换和远端改名尚未执行。现有 Cloudflare 看板尚未适配 V2。
