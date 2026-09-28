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
- Telegram 送达复用 `data/alert-state/health_cycle.json`，按事件和目标哈希记录 `pending` / `sent` / `failed` / `unknown`。发送前写不进去就停止，不退回无状态发送。明确成功才是 `sent`；服务明确拒绝是 `failed`，最多再试一次；超时、响应丢失或进程中断留下的 `pending` 都按 `unknown`，不盲重发。已成功目标不因其他目标失败而重发。状态不保存 token、正文或原始 chat id。`dry_run` / `send_dry_run` 不写这份状态。旧 fingerprint 且没有目标记录时，整次事件保持不重发，也不补写成各目标已送达。健康事件归属在首次 `pending` 时和送达记录一起写入，不表示已经成功；`fingerprint` 仍只在该事件全部目标 `sent` 后更新。健康恢复清 `health_sent_events` 里本周期每个健康事件的已确认 `sent`，不只清 fingerprint 指向的一条；`unknown` / `pending` / `failed`、日报送达和诊断尝试保留。诊断写入、恢复清理和送达更新共用同一文件锁完成读改写。这里不切换各平台自己的休市或订单心跳。
