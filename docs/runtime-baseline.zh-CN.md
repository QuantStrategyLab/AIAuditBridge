# 独立运行时基线

QuantAIService 自带 `src/ai_service` 运行时、依赖和后续八个模板，不再安装或下载 Personal 的包。保留 `ai_service.client` 导入兼容；此模块由 Quant wheel 自己提供。两服务各用独立虚拟环境，避免相同兼容命名空间冲突。

初始运行时及测试经所有者授权从 Personal 的 `f9bdf0fb55e7195d459520155bfa01652e793a5e` 复制，含 #31 安全 MCP 日志。来源及 MIT 条款见 `LICENSE.task-service`；后续在本仓库维护，无同步任务或跨 owner 运行时依赖。服务发现报告 Quant 包名和版本。

CI 安装本仓 wheel、检查依赖、运行服务及 Quant 准入测试；日志测试排除 token、任务材料、原始异常及私有标识。不复制 Personal 部署配置和测试。本次不改生产配置、身份权限、provider、部署或交易逻辑。回退须同时恢复兼容代码和配置，不改写状态库。
