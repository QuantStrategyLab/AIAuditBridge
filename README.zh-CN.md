# QuantAIService

QuantAIService 独立维护按项目隔离的完整任务运行时、依赖及场景模板。策略提示词、数据验证、执行和结果采用由业务仓库负责。

见[运行时与依赖基线](docs/runtime-baseline.zh-CN.md)。

当前为 AIAuditBridge 的本地未发布升级。新命令为 `quant-ai-service`，示例配置默认关闭。旧 VPS 模型进程、SDK 和 V1 接口已退役。

- [升级说明](docs/upgrade-v2/QUANT.zh-CN.md)
- [旧入口退役](docs/upgrade-v2/RETIRED-V1.zh-CN.md)
- [登录配置核对](docs/upgrade-v2/IDENTITY-INVENTORY.zh-CN.md)

`ops/quant-monitor` 保留日报消费和发送；迁移后需要批准的 V2 客户端和 QPK 环境。真实 OAuth、Dot/Grok 连接、发布、云端切换和远端改名尚未执行。现有 Cloudflare 看板尚未适配 V2。
