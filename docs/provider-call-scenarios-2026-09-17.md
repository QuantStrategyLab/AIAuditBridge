# Provider 调用场景与模式（2026-09-17）

实现入口：`service/provider_scenarios.py`。消费者用 `resolve_execute_kwargs(...)` 取 execute 参数。

## 三池经济模型

| 池 | 载体 | 计费 | 准入 |
|---|---|---|---|
| Codex 订阅 | CLI execute | 订阅容量（非 API USD） | 账户 rate limits / 研究路由 |
| Cursor 订阅 | CLI execute | 订阅容量；on-demand 须已确认关闭 | 可信 policy + roster + 日调用上限 |
| OpenAI/Anthropic API | analyze / review | API 美元预算 | `api_budget_admission` |

**API 永不顶替订阅执行。** 订阅 defer/额度满 → 停或延期，不改打 analyze/review。

## Provider 选型（已定案）

| 场景类 | Provider | 说明 |
|---|---|---|
| 诊断 / briefing（canary） | 默认 Codex；可显式 Cursor | 仅 `drift_analysis` / `research_summary` |
| 独立 `research_summary` advisory | 默认 Codex；可显式 Cursor | `review_only` / `research_summary`；**禁止** `codex,cursor` 链 |
| 晋级主审 / codegen / bugfix / 验收探针 | **Codex only** | 网关拒绝 Cursor |
| dual-review secondary / analyze | **API** | 独立预算 |
| Codex→Cursor fallback | **默认关** | 仅 canary stage + 显式链 + `AI_GATEWAY_CURSOR_FALLBACK_ENABLED`；启动后失败不换后端 |

网关强制：`allowed_providers` 含 `cursor` 时，`research_stage` 必须属于 canary（`drift_analysis`/`research_summary`），否则 `cursor_stage_not_canary`。

## 智能适配 vs 强制指定

**智能适配**：场景填 `mode` / `stage` / `providers` / `complexity`；晋级 pin `xhigh`。型号留给订阅准入。

**强制指定**：`allowed_providers` / `model` / `effort` / `complexity`；冲突 `ValueError`。固定 Codex 场景可覆盖 `research_stage`（仍不得选 Cursor）。

## 启用阶梯

1. 默认 Codex；Cursor 未确认 on-demand 关闭前不能跑。
2. Canary：显式 `AI_GATEWAY_RESEARCH_PROVIDERS=cursor` + diagnosis（不要先开 fallback 链）。
3. 再扩 portfolio → briefing → 独立 `research_summary` advisory（仍禁止该场景的 fallback 链）。
4. 晋级/codegen/bugfix 永不 Cursor；fallback 默认保持 false。

## VPS 验收（2026-09-18）

| 项 | 结果 |
|---|---|
| AppArmor + `cursor-sandbox-apparmor` + bubblewrap；`--sandbox enabled` | 可用（未改 disabled） |
| `AI_GATEWAY_CURSOR_HOME` + 去掉 `--exclude-workspace-context` | 已部署（`31838f8`） |
| `research_task_diagnosis` | PASS（`cursor-grok-4.6-medium`） |
| `portfolio_proposal_diagnosis` | PASS |
| `daily_briefing`（`research_summary`） | PASS（`cursor-grok-4.6-low`） |
| Cursor @ `promotion_review` / `optimization` | `cursor_stage_not_canary` |
| Fallback | 仍 false |

HTTP `/v1/ai/execute/jobs` 仍需 OIDC（static token 拒执行）。日调用计数含上述 canary；policy `max_daily_calls` 耗尽前勿再压测。