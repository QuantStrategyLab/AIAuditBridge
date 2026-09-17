# Provider 调用场景与模式（2026-09-17）

命名调用场景、默认 provider/mode/stage、Cursor canary 阶梯与停止条件。不自动部署、不跑真实模型、不 resume deferred claim。

实现入口：`service/provider_scenarios.py`。消费者用 `resolve_execute_kwargs(scenario_id, research_providers=...)` 取 execute 参数；未知场景 fail-closed。

## 场景矩阵

| 场景 | 端点 | 默认 providers | mode | research_stage | Cursor canary |
|---|---|---|---|---|---|
| `research_task_diagnosis` | execute | codex；可经 env | review_only | drift_analysis | 是（首个 canary） |
| `portfolio_proposal_diagnosis` | execute | 同上 | review_only | drift_analysis | 是（诊断通过后） |
| `daily_briefing` | execute | 同上 | review_only | research_summary | 是（诊断通过后） |
| `research_summary` | execute | 固定 codex | review_only | research_summary | 否 |
| `promotion_primary_review` | execute | **固定 codex** | review_only | promotion_review | **永不** |
| `platform_bugfix` | execute | 固定 codex | review_only / review_and_fix | — | 否 |
| `soxl_rsi2_codegen` / `global_etf_codegen` / `cn_index_etf_research` | execute | 固定 codex | review_only | optimization | 否 |
| `semantic_quality_acceptance` | execute | 固定 codex | review_only | drift_analysis | 否（验收探针） |
| `api_analyze` | analyze | OpenAI/Anthropic | — | — | 否；独立 API 预算 |
| `api_dual_review` | review | claude+gpt（+可选 Codex verifier） | review_only | — | 否 |

规则：

1. 默认研究链仍是 Codex。`AI_GATEWAY_RESEARCH_PROVIDERS` 只影响 `cursor_eligible` 场景——与选 Codex/API 一样靠请求选型，没有单独的 `*_ENABLED` 总开关。
2. Cursor 仅 `review_only` + 有效 `research_stage`；服务侧靠可信 spend policy、roster 新鲜度与日调用上限准入（对标 Codex 账户/额度门）。
3. Codex→Cursor fallback 仅当请求显式 `["codex","cursor"]` 且服务 `AI_GATEWAY_CURSOR_FALLBACK_ENABLED=true`；启动后失败不换后端；无 API fallback。
4. 晋级主审、codegen、platform_bugfix、语义验收探针即使 env 写 cursor 也强制 Codex。
5. analyze/review API 不替代 Cursor/Codex 订阅执行，也不承接 Cursor 额度失败。

## 启用阶梯（须逐步授权）

1. **契约已合入**：provider 常量、`resolve_execution_adapter`、可信 policy、health/limiter。
2. **本矩阵落地**：场景代码 + 诊断/briefing 接线；默认仍 Codex。
3. **VPS 安装**（另授权）：policy/env、目录只读刷新；示例 policy 费用未确认时仍不能执行。
4. **费用确认**：`on_demand_disabled_verified=true`、未过期 `valid_until`、`max_daily_calls`。
5. **单次 canary**：仅 `research_task_diagnosis`，`AI_GATEWAY_RESEARCH_PROVIDERS=cursor`，1 次合成 advisory；验证零工具、trust 工作区、结果身份字段。
6. **扩展**：仅 portfolio diagnosis → daily_briefing；每次单独授权与停止条件。
7. **永不自动进入**：promotion_primary_review、任一 codegen、platform_bugfix、交易/风控路径。

## 停止条件

任一出现即停车该 Cursor 路径，保持 Codex 默认：

- CLI 非成功终态、trust/sandbox/工具逃逸迹象、结果缺 provider/stage/model/effort
- policy 不可信、费用未确认、日调用触顶、目录 stale
- 启动后超时/失败试图换后端或落到 API
- canary 输出被当成晋级、下单或解除风控依据

恢复须新的明确授权。
