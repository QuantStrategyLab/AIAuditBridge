# VPS Quant Monitor AGENTS

VPS Codex (`codex-quant.service`) 每 30 分钟执行本清单。通知走「量化哨兵」Telegram bot。

路径：`AIAuditBridge/ops/quant-monitor`（VPS 上通常为 `~/Projects/AIAuditBridge/ops/quant-monitor`）。

## 环境变量

量化哨兵凭证从 GCP Secret Manager 加载（见 `scripts/load_telegram_env.sh`），**不要**手填 token 到 git。

| 变量 | 说明 |
|------|------|
| `QUANT_SENTINEL_TELEGRAM_SECRET_NAME` | 默认 `quant-sentinel-telegram-bot-token` |
| `QUANT_SENTINEL_GCP_PROJECT` | VPS 可读 secret 的 GCP 项目 |
| `GLOBAL_TELEGRAM_CHAT_ID` | **必填**（systemd Environment，勿提交到仓库） |
| `GH_TOKEN` | 只读同步策略仓库 + 开 Issue |
| `QUANT_MONITOR_ROOT` | 本目录绝对路径 |

## 每 30 分钟

1. `bash scripts/sync_strategy_repos.sh`
2. `bash scripts/health_check.sh`
3. 若 `strategy_health_score < 60` → Telegram 报警
4. 若 drift ≥ 2σ → 开 GitHub Issue；≥ 3σ → Issue @owner 并 Telegram

## 收盘后（daily）

`bash scripts/daily_briefing_pipeline.sh` → 写 `data/daily-reports/YYYY-MM-DD/<market>.json` → `AIAuditBridge` consume + dispatch
