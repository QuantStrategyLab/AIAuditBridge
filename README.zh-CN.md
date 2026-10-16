# QuantAIService

[English](README.md) | [简体中文](README.zh-CN.md)

QuantAIService 独立维护按项目隔离的完整任务运行时、依赖及场景模板。API 任务和持久助手任务使用同一套契约。策略提示词、数据验证、执行和结果采用由各自业务仓库负责。

本系列每个仓库各自维护运行时和 schema，没有共享核心。

见[运行时与依赖基线](docs/runtime-baseline.zh-CN.md)。

## 状态

- 自 2.0.10 起，worker grant 可限定为准确的 `owner` 范围（包括今后新增仓库），也兼容同一 owner 下兼容的显式 `projects` 列表。工作流提交方仍保持单项目形式，项目列表不能授予提交权限。
- 版本 2 替代已退役的 AIAuditBridge 接口。使用经审阅的配置运行 `quant-ai-service`；示例配置 `config/quant.example.json` 默认关闭。旧 VPS 模型进程和 V1 兼容接口已退役。
- 真实 OAuth 注册、Dot/Grok 连接、发布、仓库改名和部署仍未执行。当前包版本为 2.0.11。

见[升级说明](docs/upgrade-v2/QUANT.zh-CN.md)、[旧入口退役](docs/upgrade-v2/RETIRED-V1.zh-CN.md)和[登录配置核对](docs/upgrade-v2/IDENTITY-INVENTORY.zh-CN.md)。

[Quant 专用 VPS 工作流](deploy/vps/ci-deploy/README.md)支持手动 deploy、rollback 和 status，默认 dry-run。CI 生成可审阅的 Linux bundle；服务器入口安装和实际部署仍须单独批准。

## 离线场景 SDK

`ai_service.scenarios` 是面向 `ScenarioTaskSpec/v1` 和 `ScenarioInstance/v1` 的离线契约 SDK，使用 JSON Schema Draft 2020-12 校验。它只负责套用默认值、校验结构并把任务绑定到其生产者，不派发任务，也不授予权限。

- 从 `ai_service.scenarios` 导入函数。
- 包内数据位于 `src/ai_service/scenarios/*.schema.json`。
- 任务 schema 仅引用随包分发的 instance schema URN，外部校验器必须同时注册两份资源；不进行网络获取。

长模板必须使用 Dot 或 Grok；API 阶段仅限有界转换、协议探针和明确标记的小型报告审计。默认值、限制和示例见[场景契约](docs/scenario-contracts.md)。

离线编译器保持被动。`ai_service.scenarios.pilot` 与 `ai_service.scenarios.pilot_cli` 中的可选 pilot 调用方现支持经批准的手动提交和调用方读取核对。仓库示例保持 `enabled=false`，live 切换仍未执行。详见[合成协议探针](docs/pilot-synthetic.md)。

## 校验

```
python -m pip install .
python -m unittest discover -s tests/task_service -v
```

## 监控

`ops/quant-monitor` 保留日报消费和发送；迁移后需要批准的 V2 客户端和 QuantPlatformKit 环境。现有已部署的监控和看板尚未切换。
