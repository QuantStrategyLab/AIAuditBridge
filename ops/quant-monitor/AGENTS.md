# VPS Quant Monitor AGENTS

VPS Codex 定时监控（`codex-quant.timer` 每 30 分钟）+ 收盘简报（`codex-daily-briefing.timer` 22:30 UTC）。

路径：`AIAuditBridge/ops/quant-monitor`

## 环境变量

| 变量 | 说明 |
|------|------|
| `QUANT_MONITOR_ROOT` | 本目录 |
| `AIAUDIT_BRIDGE_ROOT` | `~/Projects/AIAuditBridge` |
| `QUANT_PLATFORM_KIT_ROOT` | `~/Projects/QuantPlatformKit` |
| `GLOBAL_TELEGRAM_CHAT_ID` | systemd 注入，勿提交 git |
| `GH_TOKEN` | `gh` 拉仓 + 开 Issue |
| `QSL_MONITOR_ISSUE_OWNER` | 3σ 漂移 Issue @ 的用户（默认 `Pigbibi`） |

凭证：`scripts/load_telegram_env.sh` 从 GCP `quant-sentinel-telegram-bot-token` 加载。

## 每 30 分钟（health_check.sh）

1. `sync_strategy_repos.sh` — `git pull` 四策略仓 + QPK
2. `health_cycle.py` — `build_dashboard` + `run_drift_detection`
3. `overall_score < 60` → Telegram 量化哨兵
4. drift ≥ 0.50（~2σ）→ `create_issues_for_domain` 开 Issue（不 @）
5. drift ≥ 0.75（~3σ）→ Telegram + Issue **@owner**

## 每日收盘后（daily_briefing_pipeline.sh）

1. `daily_briefing_builder.py` → `data/daily-reports/YYYY-MM-DD/<domain>.json`
2. `AIAuditBridge/scripts/consume_daily_briefing.py --dispatch`
3. 正常 → quiet；review → Issue；critical → Telegram

## 部署

```bash
bash ops/quant-monitor/scripts/deploy_to_vps.sh
```

## Codex 执行纪律

- 不要手填 token/chat id 到仓库
- 报警只走量化哨兵 bot
- 日报默认不通知人，除非 `briefing_consumer` 判定 critical
