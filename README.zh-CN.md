# QuantAIService

独立打包、按项目隔离的 AI 任务运行时。

[English](README.md) | 简体中文

## 状态

实验性项目，使用风险自负。本项目不构成投资建议，也不保证任何交易结果。它是运维工具，不提交订单，也不授予交易权限。

## 简介

QuantAIService 运行按项目隔离的 AI 任务。API 任务和持久助手任务共用一套契约，使用该服务的每个仓库各自维护运行时、schema、提示词和数据验证。本包提供服务端 `quant-ai-service`、客户端 `ai-task`，以及用于 `ScenarioTaskSpec/v1` 和 `ScenarioInstance/v1` 的离线场景 SDK，两者均按 JSON Schema 校验。

## 不适用范围

它不负责策略提示词、数据验证、执行或结果采用，这些由各自的业务仓库负责。它不是本系列仓库的共享核心。它不提交订单，也不会自行授权任何工作。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
python -m pip install .
python -m unittest discover -s tests/task_service -v
```

用你自己的配置运行任务服务端。仓库内的 `config/quant.example.json` 默认关闭，仅作起点。

## 配置

| 变量 | 必填 | 默认值 | 用途 |
|---|---|---|---|
| `AI_SERVICE_URL` | 是 | 无 | 任务服务的基础 URL。 |
| `AI_SERVICE_TOKEN` | 否 | 无 | 客户端使用的 bearer token。 |
| `AI_SERVICE_AUDIENCE` | 否 | 无 | `AI_SERVICE_TOKEN` 未设置时，工作负载身份 token 使用的 audience。 |
| `AI_SERVICE_MODEL` | 否 | 无 | agent 任务所用的模型。 |
| `AI_SERVICE_SUMMARY_MODEL` | 否 | 无 | 摘要所用的模型。 |
| `AI_SERVICE_SUMMARY_PROFILE` | 否 | 无 | 摘要 profile 名称。 |

`AI_SERVICE_TOKEN` 与 `AI_SERVICE_AUDIENCE` 至少设置一个。服务端配置存放在以 [`config/quant.example.json`](config/quant.example.json) 为模板的 JSON 文件中，定义监听地址、SQLite 状态路径、接受的 token 签发方以及 provider 后端。请把所有 `CONFIGURE_*` 占位符替换为你自己的值。

## 仓库结构

```
src/        任务服务端、客户端与场景 schema
config/     示例服务配置
deploy/     部署模板
ops/        监控辅助
docs/       设计说明与契约
tests/      单元与契约测试
```

## 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 安全

见 [SECURITY.md](SECURITY.md)。安全漏洞不要开公开 issue，也不要让凭据、私有数据和外部服务 token 进入 Git 或日志。

## 许可证

MIT，见 [LICENSE](LICENSE)。task-service 组件另有自己的 MIT 许可证，见 [`LICENSE.task-service`](LICENSE.task-service)。
