# VPS Quant Monitor

VPS 策略健康监控与收盘简报（roadmap 任务 7/10）。源码位于公开仓库 `AIAuditBridge/ops/quant-monitor`。

## 快速开始

```bash
export QUANT_MONITOR_ROOT="$PWD"
bash scripts/sync_strategy_repos.sh
bash scripts/health_check.sh
bash scripts/daily_briefing.sh
```

`health_check.sh` 会先更新代码仓库，再从四个策略仓库选择最近 7 天内、
由 `main` 分支定时或手动 workflow 的成功 `preflight_backtests` 任务生成的
`lifecycle-preflight-*` 工件。工件经路径、文件类型、domain/profile、JSON/CSV
contract 和大小限制校验后原子切换；代码仓库与 lifecycle 数据分别保存在：

- `PROJECTS_ROOT`：策略代码和 `QuantPlatformKit`；
- `QUANT_PROJECTS_ROOT`：只读收益矩阵镜像；
- `LIFECYCLE_LOCAL_ROOT`：backtest、monitor snapshot 和 drift 状态。

任一 domain 缺少可信工件时只阻断该 domain，并写入
`data/lifecycle-artifacts/status.json`；不会回退到演示或合成数据。

启用 Binance live-run 同步时，在 VPS 环境中显式设置
`BINANCE_LIVE_RUNS_SYNC_ENABLED=1` 和 producer 发布后的
`BINANCE_LIVE_RUNS_START_AT=<UTC ISO timestamp>`。同步器只读取
`QuantStrategyLab/BinancePlatform` 的 `main.yml`、`main` 分支普通
`workflow_dispatch` strategy runs，并接受精确的
`binance-live-run-<run_id>-<run_attempt>` artifact 中的
`lifecycle-run.json`。单次运行缺失或失败会保存对应 UTC 日的未知屏障；GitHub
列表/API 不可用时只把 `crypto` 写成不可用并保留其他 domain。没有这两个配置时不访问远端。

## Telegram（量化哨兵）

Token 从 GCP Secret `quant-sentinel-telegram-bot-token` 加载；**不要**把 token 或 chat id 写进 git。

告警路由：

| 事件 | 处置 |
|------|------|
| lifecycle score / drift 劣化 | 写入对应策略仓的去重、issue-only AI optimization proposal |
| 数据或可信工件不可用 | Telegram |
| circuit breaker / runtime risk | Telegram |
| optimization proposal 记录失败 | Telegram |

监控证据只触发研究审查，不自动修改策略代码、live 参数、仓位、风险预算，不自动
merge 或 deploy。成功记录策略劣化后，monitor 正常结束，不再重复通知人工。

因此健康卡片里的 `canary_eligible` 和
`pause_request_pending_confirmation` 是**证据状态**，不是券商侧已执行的事实。
只有日后接入并回传执行回执的预授权运行时，才能把暂停或 canary 推进标为已完成。
当前 payload 的 policy mode 为 `evidence_only`：自动化仅限监控、工件校验和
issue-only 研究任务，任何策略阶段、canary 或券商订单状态都不会被它自行改变。

| 变量 | 说明 |
|------|------|
| `QUANT_SENTINEL_TELEGRAM_SECRET_NAME` | 默认 `quant-sentinel-telegram-bot-token` |
| `QUANT_SENTINEL_GCP_PROJECT` | VPS 上可读 secret 的 GCP 项目 |
| `GLOBAL_TELEGRAM_CHAT_ID` | **必填**，由 VPS systemd / 环境注入 |

```bash
export GLOBAL_TELEGRAM_CHAT_ID="<your-chat-id>"
bash scripts/load_telegram_env.sh /run/quant-monitor/telegram.env
```

systemd unit 通过 `RuntimeDirectory=quant-monitor` 与 `ExecStartPre=.../load_telegram_env.sh`
写入该临时 env；`health_check.sh` / `daily_briefing.sh` 再经 `source_telegram_env.sh`
导入，不把 token 或 chat id 写入仓库。

## VPS 部署

```bash
# 从本机（已 clone AIAuditBridge）
bash ops/quant-monitor/scripts/deploy_to_vps.sh

# VPS 上
sudo cp ops/quant-monitor/systemd/codex-quant.service.example /etc/systemd/system/codex-quant.service
# 编辑 unit：设置 GLOBAL_TELEGRAM_CHAT_ID、GCP project 等
sudo systemctl daemon-reload && sudo systemctl enable --now codex-quant.service
```

收盘简报 + AIAuditBridge 分发：`bash scripts/daily_briefing_pipeline.sh`

## Immutable release 安装（仅安装）

生产 oneshot 服务从 `/opt/quant-monitor/releases/<40-hex-SHA>` 读代码。用本地已有仓库中的**精确
commit** 安装不可变 release；**不** fetch/pull，**不**改 systemd/drop-in，**不**
daemon-reload，也**不**启停服务。switch、rollback 与 `deploy_to_vps.sh` 是独立步骤，本脚本不执行。

```bash
# release root 须对当前用户可写（测试可改 QUANT_MONITOR_RELEASE_ROOT）
bash ops/quant-monitor/scripts/install_immutable_release.sh \
  --sha <40-hex-commit> \
  --repo /path/to/AIAuditBridge \
  --runtime-data /home/ubuntu/quant-monitor-runtime/AIAuditBridge/ops/quant-monitor/data \
  --runtime-venv /home/ubuntu/quant-monitor-runtime/AIAuditBridge/ops/quant-monitor/.venv
```

行为摘要：

- 校验 SHA 为 40 位小写十六进制，且在 `--repo` 中作为 commit 存在；
- `git archive` 导出该 commit 的完整树到 release root 下临时目录，校验必需路径后原子
  `mv` 到 `/opt/quant-monitor/releases/<SHA>`（可用 `--release-root` / `QUANT_MONITOR_RELEASE_ROOT`）；
- 仅将 release 内 `ops/quant-monitor/data` 与 `.venv` 符号链接到显式传入的 runtime 目录，不复制、不删除 runtime 数据；
- 已存在目标只有在与该 commit 的完整 archive 树一致时才幂等复用；内容不一致则拒绝覆盖；
- 失败时只清理本脚本自己的临时目录。

## 策略健康快照（只读）

`health_cycle.py` 会把生命周期 dashboard 规范化为
`data/health/strategy_health_dashboard.v1.json`。也可以单独刷新：

```bash
bash scripts/refresh_strategy_health.sh
```

刷新脚本兼容支持或不支持 `--output-dir` 的 `quant-lifecycle dashboard` CLI；旧 CLI
的临时输出只在 monitor 数据目录内处理。没有可用输入时输出 `unavailable`，不会生成演示指标。

默认不向外同步。只有在显式设置 `STRATEGY_HEALTH_PUBLISH=1`、专用
`STRATEGY_HEALTH_SYNC_URL`，以及 `STRATEGY_HEALTH_SYNC_TOKEN` 或 root-owned
`STRATEGY_HEALTH_SYNC_TOKEN_FILE` 后，才运行：

```bash
bash scripts/publish_strategy_health.sh
```

发布脚本只接受 `strategy_health_dashboard.v1`，不回退使用其他 token，也不把 token
或原始错误写入输出。


### 专用镜像更新与安装元数据（2026-09-08 修复）

`setup_vps_runtime.sh` 现在把 QPK 的 Git archive 放到临时目录安装；运行源码仍由
`common_env.sh` 的 `PYTHONPATH` 指向专用镜像。构建生成的 egg-info 不再写回镜像。

同步只允许自动处理已确认由旧 editable 安装改写的两个**未暂存**已跟踪文件：
`src/quant_platform_kit.egg-info/PKG-INFO` 和 `SOURCES.txt`。先完成干净替代镜像，
再把整个旧目录（包括未跟踪文件）移动为
`QuantPlatformKit.preserved-before-metadata-refresh`，最后放入新镜像；安装替代目录
失败时恢复旧目录。未知/暂存修改、已有保留目录或并发同步锁均返回失败；不强制
checkout、不 stash、不删除旧状态，也不创建第二份累计备份。

这是可恢复的两次目录重命名，不是同时原子替换。意外断电/强制终止后须只读检查
保留目录与 `.sync-strategy-repos.lock` 的实际状态，再恢复被中断的同步；不要按定时
重试自动删除锁或备份。此次源码修复不代表 VPS 已采用：部署后仍须读回下一次正常
监测周期及网站结果时间；不能用本地测试代替线上恢复证明。
