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
GitHub API 限流或上游不可用时，`status.json` 会记录脱敏的 `reason_code` /
`http_status` / `rate_limit_reset_at`，并在多 domain 同根因时附加
`shared_upstream`；健康周期 Telegram 告警会压缩为上游故障，不会伪装成策略或交易异常。

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
# 从本机（已 clone AIAuditBridge）；REVIEWED_MAIN_SHA 为已审阅的 40 位 main SHA
AIAUDIT_BRIDGE_SOURCE_SHA="$REVIEWED_MAIN_SHA" bash ops/quant-monitor/scripts/deploy_to_vps.sh

# VPS 上
sudo cp ops/quant-monitor/systemd/codex-quant.service.example /etc/systemd/system/codex-quant.service
# 编辑 unit：设置 GLOBAL_TELEGRAM_CHAT_ID、GCP project 等
sudo systemctl daemon-reload && sudo systemctl enable --now codex-quant.service
```

收盘简报 + AIAuditBridge 分发：`bash scripts/daily_briefing_pipeline.sh`

策略健康 domain 报告仍走原来的 quiet / issue / Telegram 分流。LongBridge 日运行投影是另一份离线输入，不并进策略健康。在 AIAuditBridge 仓库根目录：

```bash
python3 scripts/consume_daily_briefing.py --runtime-projection projection.json
python3 scripts/consume_daily_briefing.py --runtime-projection projection.json --dispatch
```

默认只预览。`--dispatch` 才发送，并按平台、业务日和投影中的目标集合复用既有逐目标送达；同一目标集合换顺序、改观察时间或改正文不会另发。这个目标集合是否等于生产固定配置，还要云端接线验收。`--dry-run` / `--send-dry-run` 不联网、不写送达状态。投影里的成交笔数尚未接通，不能读成零成交。这不是云端真实日报，也不关闭各平台原来的通知。

GCS 输入默认关闭，也不改 22:30 UTC 的简报定时。只有 `QUANT_MONITOR_RUNTIME_DIGEST_ENABLED=true`，并且同时配置合法的 `QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX`（末段必须是 `runtime_daily`）、IANA `QUANT_MONITOR_RUNTIME_TIMEZONE` 和 `QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY`（scope 末段为 `paper`）时，pipeline 才按该时区的业务日读取 `{prefix}/longbridge/paper/{day}.json`。日期和目标键来自这三项配置，不从对象内容反推。读取用现有 `gcloud storage cat`，20 秒超时、正文上限 1MiB、不重试；缺对象、无权限、超时或非法内容都不发送。发送仍是原来的 `--dispatch`、量化哨兵和 `health_cycle.json`。domain 简报照常单独执行：它失败不会跳过 runtime，runtime 失败也不会取消已跑的 domain；任一项失败则 pipeline 以非零结束。日志不写正文、对象 URI 或 chat id。本地 `--runtime-projection` 仍然可用，并与 `--runtime-projection-gcs` 互斥。VPS 是否已安装该开关、云端身份是否能读该对象、以及首份送达，都还没有验收。

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

### QPK 运行源码固定（R12）

`ops/quant-monitor/qpk-runtime.sha` 保存已审的精确 40 位 QuantPlatformKit commit。
`setup_vps_runtime.sh` 与 `sync_strategy_repos.sh` 都只从该文件读取 pin：缺失、非
40hex、fetch/对象不可用或 checkout 后 HEAD 不匹配时直接失败，不回落 `origin/main`。
setup 的 AAB dirty 检查包含该 pin 文件，拒绝消费未提交篡改。sync 仅固定 QPK；其余
四策略仓仍同步 `origin/main`。metadata-only 恢复路径也 checkout 同一 pin，并保留原
镜像目录。

### 第三方依赖锁定（R12 续）

`ops/quant-monitor/requirements-linux-py312.lock` 只锁定 monitor 在 **CPython 3.12 /
Linux x86_64 / glibc ≥ 2.34** 上实际需要的直接与传递依赖（含既有 QPK 构建后端
`setuptools==84.0.0` 与 `wheel`/`pip`），并带 PyPI wheel hash。它不是整台 VPS 或全
组织可复现证明，也不锁定四策略仓或通用 gateway 依赖。

`setup_vps_runtime.sh` 在改 mirror/venv 之前：

1. 要求 lock 存在于当前 `QUANT_MONITOR_ROOT`，且字节与受审 `SOURCE_SHA` 中
   `ops/quant-monitor/requirements-linux-py312.lock` 完全一致；缺失、未跟踪或脏
   工作区拒绝。
2. 校验当前解释器为 3.12、OS 为 Linux、机器为 x86_64，并用数值解析确认 glibc ≥
   2.34；不支持的环境直接说明本 lock 仅覆盖该平台，不静默 fallback。
3. 用当前 venv 的 `python -m pip install --require-hashes --only-binary=:all: -r
   requirements-linux-py312.lock` 安装完整 lock，不再执行无界 `pip/wheel -U` 或无
   版本 `numpy/pandas/google-cloud-storage`。
4. QPK 仍从临时 git archive 安装，但使用 `--no-deps --no-build-isolation`，复用已
   锁 setuptools/wheel，不隐式下载 build deps；最后 `pip check`。pip 成功不等于业
   务验收。

需要单独准备 venv 时，可显式设置 `QUANT_MONITOR_VENV` 指向一个新的绝对目录；其
父目录必须已存在，目标不能已存在，也不能与 AAB 源码、monitor/data、现用 `.venv`、
`QUANT_PROJECTS_ROOT`、`LIFECYCLE_LOCAL_ROOT` 或 QPK 镜像重叠。此模式只从现有 QPK
镜像读取精确 pin 的 Git archive，不 fetch、
checkout 或改写共享镜像；本地缺少该 commit 时会在创建 venv 前失败。未设置该变量
时仍安装到 `$QUANT_MONITOR_ROOT/.venv`，默认流程不变。暂存不会修改 systemd 配置；
服务是否使用该路径须由独立的配置变更明确决定。

生产 venv 是否已按此 lock 迁移须另做安装/读回；本说明不声称生产已切换。
