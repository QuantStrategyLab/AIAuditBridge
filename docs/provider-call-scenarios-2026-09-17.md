# Provider 调用场景与模式（2026-09-17）

实现入口：`service/provider_scenarios.py`。消费者用 `resolve_execute_kwargs(...)` 取 execute 参数。

## 三池经济模型

| 池 | 载体 | 计费 | 准入 |
|---|---|---|---|
| Codex 订阅 | CLI execute | 订阅容量（非 API USD） | 账户 rate limits / 研究路由 |
| Cursor 订阅 | CLI execute | 订阅容量；on-demand 须已确认关闭 | 可信 policy + roster + 日调用上限 |
| OpenAI/Anthropic API | analyze / review | API 美元预算 | `api_budget_admission` |

Cursor 与 Codex 同类；API 失败/预算耗尽不得顶替任一订阅执行。Cursor 美元成本未知，记 `cost_incomplete`，不得报 0。

## 智能适配 vs 强制指定

**智能适配（默认）**：场景填入 `mode` / `research_stage` / `allowed_providers` / `complexity`；可 pin `reasoning_effort`（如晋级主审 `xhigh`）。`model`/未 pin 的 effort 省略，由网关订阅准入选择。`AI_GATEWAY_RESEARCH_PROVIDERS` 仅软影响 `cursor_eligible` 场景；固定 Codex 场景忽略该 env。

**强制指定**：调用方可传 `allowed_providers` / `model` / `reasoning_effort` / `complexity`。与场景冲突则 `ValueError`，禁止静默改写到其他 provider 或 effort。例：晋级场景强制 `cursor` → 失败；语义验收强制 `gpt-5.6-terra` + `medium` → 透传。

## 场景矩阵

| 场景 | 默认 providers | mode | stage | 复杂度 | pin | Cursor |
|---|---|---|---|---|---|---|
| `research_task_diagnosis` | env 软选 | review_only | drift_analysis | medium | — | canary |
| `portfolio_proposal_diagnosis` | env 软选 | review_only | drift_analysis | medium | — | canary |
| `daily_briefing` | env 软选 | review_only | research_summary | low | — | canary |
| `research_summary` | 固定 codex | review_only | research_summary | low | — | 否 |
| `promotion_primary_review` | 固定 codex | review_only | promotion_review | high | effort=xhigh | **永不** |
| `platform_bugfix` | 固定 codex | review_only/fix | — | medium | — | 否 |
| codegen / cn_index | 固定 codex | review_only | optimization | high* | — | 否 |
| `semantic_quality_acceptance` | 固定 codex | review_only | drift_analysis | medium | 调用方强制型号 | 否 |
| `account_operational_diagnosis` | 固定 codex | review_only | drift_analysis | high | — | 否 |
| `api_analyze` / `api_dual_review` | API | — | — | — | — | 否 |

\* Global ETF codegen 调用方强制 `complexity=medium`、固定 Luna 型号。

## 启用阶梯

1. 契约 + 场景矩阵已合入；默认仍 Codex。
2. VPS：policy/CLI/目录刷新；示例 policy 未确认 on-demand 关闭前不能跑。
3. 确认 `on_demand_disabled_verified` + `valid_until` + `max_daily_calls`。
4. 单次 canary：`AI_GATEWAY_RESEARCH_PROVIDERS=cursor` + `research_task_diagnosis`。
5. 再扩 portfolio → briefing；晋级/codegen/platform_bugfix 永不自动进 Cursor。

## 停止条件

CLI 非成功、trust/工具逃逸、身份字段缺失、policy/roster/日调用失败、试图跨后端或落到 API、把 advisory 当晋级/下单依据 → 停车 Cursor 路径，保持 Codex。
