# VPS Quant Monitor AGENTS

VPS Codex 定时监控（`codex-quant.timer` 每 30 分钟）+ 收盘简报（`codex-daily-briefing.timer` 22:30 UTC）。

路径：`AIAuditBridge/ops/quant-monitor`

## 环境变量

| 变量 | 说明 |
|------|------|
| `QUANT_MONITOR_ROOT` | 本目录 |
| `AIAUDIT_BRIDGE_ROOT` | `/home/ubuntu/quant-monitor-runtime/AIAuditBridge`（专用运行副本） |
| `QUANT_PLATFORM_KIT_ROOT` | `$QUANT_MONITOR_ROOT/data/lifecycle-projects/QuantPlatformKit`（专用镜像） |
| `GLOBAL_TELEGRAM_CHAT_ID` | systemd 注入，勿提交 git |
| `GH_TOKEN` | `gh` 拉仓 + 开 Issue |
| `QSL_GITHUB_REPO` | 非策略类 briefing Issue 的默认仓库；策略证据按 domain 写入对应策略仓 |

凭证：`scripts/load_telegram_env.sh` 从 GCP `quant-sentinel-telegram-bot-token` 加载。

## 每 30 分钟（health_check.sh）

1. `sync_strategy_repos.sh` — 更新四策略仓 + QPK 的只读专用镜像
2. `sync_lifecycle_artifacts.py`，随后 `health_cycle.py` — `build_dashboard` + `run_drift_detection`
3. lifecycle `overall_score < 60` 或 drift ≥ 0.50 → 生成 monitoring evidence
4. evidence → 对应策略仓的去重、issue-only AI optimization proposal
5. 分数或漂移本身不发 Telegram；数据/工件不可用或 Issue 记录失败才通知人工

## 每日收盘后（daily_briefing_pipeline.sh）

1. `daily_briefing_builder.py` → `data/daily-reports/YYYY-MM-DD/<domain>.json`
2. `AIAuditBridge/scripts/consume_daily_briefing.py --dispatch`
3. 正常 → quiet；review/critical → issue-only AI optimization proposal
4. data unavailable、circuit breaker 或 proposal 记录失败 → Telegram

## 部署

```bash
bash ops/quant-monitor/scripts/deploy_to_vps.sh
```

## Codex 执行纪律

- 不要手填 token/chat id 到仓库
- 报警只走量化哨兵 bot
- 策略健康证据只进入可审计的 issue-only 优化队列，不自动改策略、参数、仓位或部署
- 策略劣化记录成功后不通知人；只对数据/运行风险和记录失败 fail-closed 通知
