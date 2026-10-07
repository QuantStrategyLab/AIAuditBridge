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

定时 AI 消费者的失败结果附加固定枚举 `failure_stage` / `failure_category`，保留原
`status`、`reason` 和退出码。阶段只标识当前处理边界，不证明根因、模型是否收到请求或
能否重试：日报外层的 `briefing_input_processing` 包含读取和规则处理，
`summary_processing` 包含摘要输入校验、调用和响应处理；只有既有明确返回码才进一步
标识摘要校验、配置、执行或结果处理。cycle 阶段仍合并读取、JSON/结构和时间校验。
`diagnosis_attempt_persistence` / `diagnosis_deferred_state_update` 标识状态更新尝试，
也可能在内部读取、目录或锁步骤失败。类别是固定接口异常类型或既有返回码投影，未知
异常类型用 `unknown_error`；不输出原异常、路径、账号、提示词、响应或凭据。
这些字段仅进入消费者结果，不进入告警/诊断 fingerprint、attempt 状态或送达身份，
不新增调用、重试或通知。旧产物只有粗原因时仍不能据此确定缺文件或模型故障。

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

### QPK 采用兼容验证与 profile 缺数（DATA-03，2026-10-06）

设计/实现：QPK [#647](https://github.com/QuantStrategyLab/QuantPlatformKit/pull/647)
已合为 `28675796cabbe137a1fa3970b70d1aa98e952c88`，显式 live 选择不能用 research
CSV 补空或拼接。AAB 审计基线 `ddd85c80413ee0c1bd0d663fc80e607692c58ef9` 固定
QPK `086166458d4e3f61bb8937054cf7e69ff6ef914a`。两版之间 12 commit / 26 file
的静态比较及初始有界离线消费者验证，本身不表示源码 pin 已升级或生产采用已验收。

本阶段：固定 old/new QPK 的 218 个 package blobs 和上述 AAB 必要源码，分别用
同一组明确 synthetic fixture 走实际 sync ZIP 校验、local PerformanceStore、
ReturnCollector、monitor/drift/dashboard、日报 builder 与规则分类。两侧各 12 项通过；
research 默认指标和普通 snapshot 格式一致。它们包括成功复现缺陷的 characterization，
不是 adoption PASS。普通兼容测试的网络、进程、云、模型和通知尝试均为 0；独立 guard
自测有 2 次网络、2 次云尝试被阻断，实际外部 IO 为 0。没有真实账户取数、模型调用或
部署。当前验证环境 pandas 2.2.3 / numpy 2.3.5，不代替本节上方的精确 lock 验证。

Binance 单次同步最多近 7 日。7 个日 checkpoint 只有 6 个有效日收益，低于 monitor
原有 `min_observations=10`，这是正常 warm-up 缺样本。两次重叠同步在本地保留并去重，
导入计数 7、0、4、0 后积累 11 个不同 checkpoint / 10 个日收益。未知屏障后的新连续段
须重新满足样本数；达到 10 仍保留截断状态，不能冒充完整期间或可比较 drift。
实际 store 的保留量、连续段、account/stream 绑定及有效运行 source 尚未读回。

初始审计复现：当 crypto 域同时有一条不足样本的 live profile 和另一条可用 research
profile，health 的 domain 非空检查不报缺数，normalized dashboard 仍为 `ready`；
同周期日报按可信 `status.json` 的 expected profiles 检查，正确标记缺 live profile /
`unavailable`，规则路由为数据不可用 Telegram。这个差异是源码复现，不证明线上曾发生。

工程阶段已合并：[AAB #314](https://github.com/QuantStrategyLab/AIAuditBridge/pull/314)
正常合并为 `59cc6dded575ca28d9e23a9bd7c2d2c7e6592e58`；
[exact PR CI37464400980](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37464400980)
和 [main CI37465133758](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37465133758)
均五 job 全部成功，Python 阶段 2110 passed / 15 skipped / 941 subtests passed。
首轮 [CI37462661452](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37462661452)
因两个新 fixture 依赖本地额外 scripts 路径而失败；测试已显式加载同目录真实 normalizer，
原失败留存，未重跑旧 run。合并 tree 与受审候选一致，分支已清理。修复复用已有
artifact-status 校验，从一次已验证
读取取得同一 expected-profile 集合及 provenance，供 health 与日报消费。配置内缺 profile
进入运营数据不可用告警，保留其它有效 profile 的结果；缺 profile 的旧 snapshot/drift
不能成为新的策略劣化或 AI optimization evidence。本周期缺 drift 时，dashboard 低分也
不能绕过该资格检查。完整 coverage 保持原路径，fresh
`not_configured` 与 stale/invalid status 分别处理；复用既有送达状态验证去重、未知投递和恢复。
不从用户标签或已出现样本反推 expected 集合，不降 min10，不借 CSV 凑样本，也不把整个
dashboard 统一标为 research。AAB caller/规则修复、精确 lock 兼容、pin 采用、运行读回和
真实业务周期分阶段验收。本次源码 `qpk-runtime.sha` 从上述审计基线调整为
`28675796cabbe137a1fa3970b70d1aa98e952c88`；生产服务实际加载的 pin、运行 source、
实际保留记录与生产采用仍未验，不能从源码文件值推定。

实施验证：原源码 RED 保留一次读取与缺 profile 的真实失败，新合同尚未实现的错误亦保留；
补充 RED 复现“缺当前 drift 但 dashboard 低分仍产生 optimization finding”，候选修复后
既有 monitor fail-closed suite 为 64/64、无 skip。固定 old/new QPK 的实际路径各 13/13
通过，包含未 mock 产品计算/collector/store/normalizer/dispatcher 的真实 health main
及 daily main；新版 QPK 下，两者均标缺 live profile / 数据不可用，valid profile 本地结果
保留，optimization findings/issues 为 0。publisher 的实际 schema-check 代码块读取同一
normalized 文件后，其 SHA256 未变；curl/远端发送未执行。所有本阶段普通测试的 guard
计数均为 0。前述离线环境未装 ruff，未本地运行 lint、整仓 suite 或精确 lock 验证；
远端 exact PR/main CI 已通过。CI Python 主套件的 QPK `7363011...`、AAB 审计基线 pin
`086166...` 及本次源码 pin `286757...` 分别记录，不能把 CI 通过当作精确 runtime lock
或生产采用证明。
[main CI 安装日志](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37465133758/job/112274151998)
为 pandas 3.0.6 / numpy 2.5.3，ops lock 为 3.0.5 / 2.5.2；该 CI 未验证精确 runtime lock。
新增独立精确 lock 阶段已通过：官方原 hash 锁定的 27 包下载后在隔离 venv 离线安装，
实际 pandas 3.0.5 / numpy 2.5.2，`pip check` 成功。已合 test `065152df...` 的 64/64、
QPK `086166...` 与 `286757...` 的实际消费者 synthetic 路径各 13/13，均无失败或 skip；
导入的相关 AAB 路径匹配 `59cc6dde...`。产品测试进程的外部尝试均为 0，官方 wheel
下载有真实网络，不能将全任务表述为零网络。这关闭有界精确依赖兼容阶段；本次源码
pin 调整依据该结果、上述 12 commit / 26 file 审查和已合 health 修复。部署、实际记录
覆盖和运行采用仍未验，不把旧 QPK characterization 当 live 收益资格。
旧 QPK 仍体现旧 research/CSV 行为，不因此取得 live 收益资格。

采用边界：源码合并或仅安装 immutable release 不会自行切换服务。实际服务源码根
采用新 pin 后，正常 health 周期的 `sync_strategy_repos.sh` 才会 fetch/checkout 该
QPK commit；该同步不拉 AAB main、不安装依赖、不重启服务。`common_env.sh` 在
QPK 镜像 `src` 已存在时将其置于 `PYTHONPATH` 前面，同周期后续 Python 子进程可
直接使用新源码，无须等 venv 重装；日报 builder 自己不做镜像同步。需只读确认实际
源码根、pin、解释器与模块来源，不能只看 checkout 或 package 版本。完整
`deploy_to_vps.sh` 会安装 lock/QPK、更新 systemd、重启 timer 并启动一次 health，
不作为本次源码验证动作。真实 history/stream 尚未验收不阻止此源码候选；实际缺数仍
保持 `unavailable`，生产验收另核 account/stream、保留连续段与原 min10，不能用
synthetic 结果替代真实记录。本次仅更新该 pin 与本节，不改 lock、阈值或数据选择。

现有 publish 脚本直接发送 health cycle 的 normalized 文件，只检查 schema，不二次
normalize。QRS 当前 `strategy_health_dashboard.v1` receiver 对整体 `unavailable`
会清空展示行并重算 summary，保留安全 error codes；因此本阶段“保留其它有效 profile”
是指 AAB 本地 store、周期结果和日报，不承诺当前看板保留部分行。单独
`refresh_strategy_health.sh` 的重建路径尚未采用 expected-profile 校验，不能代替本阶段
health→publish 接线验收；本次不改变 QRS/UI 合同。

English summary: the fixed old/new source consumer fixtures passed 12 checks per
version, including reproduction of the initial partial-domain coverage defect.
This is synthetic compatibility evidence, not pin adoption or production acceptance.
Merged AAB #314 now shares trusted artifact-status expected profiles between
health and daily reporting, preserves valid measurements and routes missing data as an
operational condition. The monitor suite passed 64 checks and each actual old/new QPK
consumer run passed 13, including real health/daily entrypoints. Exact PR CI37464400980
and main CI37465133758 each passed all five jobs; Python reported 2110 passed,
15 skipped and 941 subtests passed. The initial fixture-loader CI failure was fixed
without changing production imports. CI installed pandas 3.0.6 / numpy 2.5.3 while
the ops lock pins 3.0.5 / 2.5.2. A separate exact 27-package lock venv now passed
pip check, merged regression 64/64 and each fixed QPK consumer run 13/13, with
zero product-test external attempts; official wheel acquisition used network.
The source pin now selects the reviewed QPK 286757; production adoption remains
unverified. Once the active service source selects that pin, its normal health sync
can update the QPK mirror used by later Python children without reinstalling the
venv or restarting services. Merging source or installing an inactive release does
not prove that switch occurred. Native history/stream qualification and actual
module identity remain deployment acceptance checks; sample thresholds and data
selection are unchanged. The current QRS aggregate receiver clears rows
when unavailable; valid AAB measurements remain local, and partial-row UI display is
not claimed. Standalone dashboard refresh is a separate, unqualified rebuild path.

#### DATA-03 下一步：有界运行采用计划（待独立评审/执行）

[既有只读 run37471802240](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37471802240)
使用 `a9de1f359c9545c45c69c456b5b753b8a1c6e987`，读回 health 静态根
`38d172262ed60be3eb9579256649f5a1aa31f45a` / pin `086166...`，daily 静态根
`1d06c4a2687a14545280855faf2cf854e524f8c7`；两者均 `failed/exit-code`，前后配置和
invocation 稳定。venv/解释器仍 unknown；三项 QPK 候选文件 hash 不证明镜像 commit
或真实 import。旧 release 是已观测回退配置，不是已知健康版本；不重发已用的一次诊断。

PR #318 合并后的独立扩展读回
[run37486273513](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37486273513)
基于 `28a85ff022e77623d90cd05eef1a70448cba3c39`，在 **2026-10-06 15:18:00 UTC**
观察到上述两个旧根仍 `failed/exit-code`（health 2、daily 1），配置和 invocation 前后稳定；
这不是故障首次发生时间。固定 QPK HEAD 为 `086166458d4e3f61bb8937054cf7e69ff6ef914a`，
ReturnCollector hash 匹配该旧版本；health pin 与 HEAD 匹配，daily 未读取 pin，不能代填。
已取得声明 venv 的 cfg hash、两个 Python 链接类别及同次 PATH 候选元数据；`python` 链接
类别 unknown 不等于损坏。原 interpreter/import gates 仍 unknown，未执行目标解释器或导入。
这些剩余未知项不阻止先准备独立新环境，仍阻止声称服务已采用或恢复。

随后唯一一次 [inactive staging run37498059845](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37498059845)
使用已通过 main CI 的 `665f61e437eae65ed802c10caeea7b0effad698f`，在 2026-10-06
16:44:32 UTC 以 `qpk_commit_unavailable` / exit 1 停止，未到创建 venv 或 pip 阶段；
候选 metadata 步骤跳过。该 enum 不区分对象缺失和镜像/读取问题，当前磁盘状态未补读。
后续源码候选改为在 runner 临时目录准备独立的公共 QPK 精确对象；失败 run 保留且不重发。

修复后的独立 [inactive staging run37504798174](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37504798174)
以 `35ac71176127f07e00fe04dbc793777f3c595bc0` 在 2026-10-06 17:36:36 UTC 完成
候选 metadata 验收：27 个锁定 distribution 版本与四项 QPK 文件 hash 匹配，QPK pin 为
`28675796cabbe137a1fa3970b70d1aa98e952c88`。仅此 job 执行，其余 11 jobs 跳过；
receipt 明确 `application_import_proof=false` / `runtime_adoption_proof=false`。
新 venv 已安装，尚未安装 immutable release、验证完整 consumer 导入或切换服务。

PR #322 合并并通过 main CI 后，唯一 inactive release run37516689700
（head `fa4e2b0a430254368f5198cb9a8b6da89a6e1d57`、job112451294961）
在 installer 阶段失败。checkout/main/固定35ac source gate 已通过；白名单仅有
`failure=installer` 和 wrapper 外层 exit1，原生 installer 退出码/异常/stdout marker
没有输出，不能据此推断权限原因。未出现 final tree/link 成功回执，consumer import
步骤 skipped，其余12 jobs skipped；残留与最终 target 状态 unknown，不重跑或清理。

固定35ac Git tree 不含 tracked data/.venv 或其子项，已排除此 archive 源冲突。
两次运行的 GitHub runner 标识一致不证明 Unix euid/gid 或 release parent 写权限。
下一步拟在既有 inspect-quant-paths 内补固定元数据读集：release parent 的有效访问条件、
固定 target 类型和两条 runtime link 匹配类别；无目录枚举、数据内容、写探针或自由路径。
本次三文件候选在既有入口新增 `inactive_install_metadata`，仍待源码评审与独立只读运行，
当前不能声称 release 安装、真实导入或服务恢复。固定读集仅为 release parent、35ac target
及其 data/.venv 两个叶子；逐层 NoFollow，不枚举目录、不读文件内容或未知链接目标。
进程 euid/egid/groups 仅在内存参与目录 owner/group 匹配，输出布尔值，不输出身份或组列表。
只有平台同时支持 dir_fd、effective_ids 和 no-follow 时才读取 effective R/W/X；不支持为
unsupported，系统调用不可达为 unknown，不退回 real uid 检查，不创建写探针。
两个元数据快照及有效身份须一致；变化使整组 unknown。缺失父目录或权限不可达不等于
目标不存在；仅直接固定叶子的缺失才能记 absent。链接只分类 expected/other，不跟随目标，
未枚举的其它残留保持 unknown。此组不会升级旧 interpreter/import/adoption gates，也不能
恢复已经丢弃的 installer 原生退出信息。

随后独立只读 [run37521955303](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37521955303)
（main `b7651526b6d03cc9cb0d25055326763f20939d78`）在 2026-10-06 19:51:14–15 UTC
取得稳定 metadata：release parent 为 0755 目录，当前 runner effective read/execute=true、
write=false，owner/group 匹配均 false；固定35ac target 为 absent。其它残留未枚举；原安装
child exit 仍 unknown。原 /opt 安装路线保持 blocked，不改权限/身份、不重跑安装。

consumer 依赖导入不必等待 /opt 安装。PR #324 新增独立 `check-quant-consumers-offline`
模式，沿既有 protected workflow/runner、main gate 和固定35ac checkout，只在 RUNNER_TEMP
本次 run 独占的新目录创建临时纯源码快照。Git archive 严格选择已审38个.py及lock/pin，共40文件，
不包含 .git、ignored文件、运行data、.venv、工作流或测试。先核规范相对路径、普通blob、
tar完整清单/大小/hash，拒绝重复、缺失、额外、链接/设备/稀疏条目，再安全写入新目录并复核
整个快照。既有目录拒绝覆盖或重用；失败不清理未知残留，不写 /opt、旧data/venv或shared镜像。

后续新隔离进程使用已验DATA03 venv和普通Python导入，复用既有源码manifest、QPK origin/
RECORD/固定hash、pyc和副作用守卫；应用根只指向这份40文件快照，.git/data/.venv仍禁止。
不加入自定义加载器或FileFinder行为。输出只证明 temporary-source consumer dependency/import
readiness；immutable-release、shell-selection、runtime-adoption和health-recovery proof均为false。
这不是将被拒绝的部署改址。原两个旧服务在上述观察时仍failed，不是healthy LKG。
快照是临时验证副本，runner清理后的保留状态不作保证。

PR #324 合并并通过 main CI 后，唯一 imports-only
[run37533618835](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37533618835)
（controller `1131116e3d405ed3dca56f5c7406df2c6d3af190`、job112508913937）在
2026-10-06 21:24:20 UTC 验证固定35ac快照成功：40文件、480370字节、无runtime links。
随后导入在 `qpk_drift` 停止，固定回执为 `failure=consumer_import`、write=1，
network/process/read=0；导入期写入被守卫拒绝。没有实际写入路径、事件细分或异常正文，
不能据此把某个依赖认定为已证实的主机根因。其余13 jobs skipped；所有完整应用导入、
immutable-release、shell-selection、runtime-adoption、health-recovery proof均为false。
失败保留临时残留、不重跑；本次未安装release、改权限或切换服务，/opt部署路线仍blocked。

离线 Python 3.12 夹具复现固定 QPK286757 的两个模块常量初始化：
`common/strategy_plugins.py:34` 和 `strategy_lifecycle/performance_store.py:53` 调用
`tempfile.gettempdir()`；冷态标准库会创建、写入并删除探测文件，原守卫在创建前拒绝。
这只是与主机固定分类一致的本地机制证据。最小初始化修正是在完整快照/目录绑定验证后、
应用导入和守卫生效前，将本隔离进程的 `tempfile.tempdir` 显式设为已验证的本run任务目录，
仅选择临时目录名称，不调用探测函数、不修改全局环境或应用源码。其余守卫保持原样，
实际 `mkstemp`、`mkdir` 和非白名单读取仍必须失败；没有新增允许写入或读取的路径。
修正后的真实完整导入结果仍待独立 source CI、评审及新运行验收，旧失败run不重发。

采用差异按实际使用边界核对：#287/#289 为 lock/staging 安装路径；#290/#299 为 health
正常调用的 artifact sync；#301/#306/#314 为 health 及日报 coverage/失败处理；#311
为日报 wrapper/consumer 结果摘要；#316 为 QPK pin。#307 的 watcher/strategy_watch
会被导入，但 health 直接调用 finding dispatch，不执行 watcher `run_watcher/main`；
日报 builder 仅复用 health 的 coverage helper。日报 consumer 还经 dual-review imports
加载 adapters 包及 CodexAdapter 定义，不能把这些依赖误归为仅 archive；常规 pipeline
未因此调用 adapter 执行方法。Gateway 服务与 monthly audit 入口不由这条启动链运行。

| 顺序 | 有限完成条件与执行边界 |
| --- | --- |
| 固定元数据 preflight | #318 和上述一次读回已完成静态阶段；保留未知 gate，不追加任意路径读取，也不把 metadata 当 interpreter/import 证明 |
| inactive 候选 venv | 独立 `stage-quant-runtime-inactive` 入口复用 setup 的显式 staging 分支，以精确 main/干净 checkout、原 27 包 hash lock 和独立获取的公共 QPK 286757 commit 建立固定新 venv；候选检查只执行标准库，核解释器隔离、锁版本及四项 QPK 文件 hash。源码/合成测试阶段不等于真实安装，执行后也不证明 application import 或服务采用 |
| immutable release 与离线导入 | 本地候选新增 `install-quant-release-inactive`，应用 source 固定上述 35ac，复用原 installer 绑定已验新 venv 与既有 data。目标已存在即拒绝；安装后逐项核完整源码树及两个精确 symlink，不能以 reused 或退出 0 代替验收。再以受守卫的新隔离进程导入实际 health/builder 懒加载依赖和完整 daily CLI。真实运行仍待单独阶段授权/验收 |
| 双服务采用/回退 | 先绑定 health/daily 各自的原源码根、解释器、QPK 和 timer 状态，再在不触 Gateway/共享 runner 的窗口分别切换并读回。恢复 timer 是可能触发业务的执行边界，不能当纯配置检查。当前没有现成 monitor switch/rollback mode；不得借用 Gateway `deploy` 或会立即运行 health 的完整 monitor deploy |
| 自然周期验收 | 独立确认新 source/module 采用、真实 account/stream 与保留连续段；短样本仍 unavailable/min10。回退源码根不能撤销既有数据、Issue、通知或送达状态，也不保证共享 QPK/venv 同时回退；保留状态，不删除重试。两个旧 failed 服务不构成恢复健康证明 |

PR #322 的候选只改既有 workflow、部署测试、工作流路由测试和本说明；installer、setup、应用源码和
lock 不改。安装入口沿原 protected runner/concurrency，main/workflow SHA 为执行控制版本，
干净 checkout 和被安装应用固定为 35ac，两者明确分开；不取最新 main 作为应用内容。
不接收自由路径/source 参数，不调用通用 Gateway job。固定 release、新 venv、既有 data
三者必须精确绑定；现存目标、安装非零、源码/文件模式/链接不符或并发 mv 造成的嵌套目录均
停止，未知目标和残留保留，不自动重用/覆盖/删除。仅 installer 子进程固定 umask 022，
使公开源码目录/文件模式确定；不改父进程或系统 umask。原 installer 的 reuse 比对会排除 data/.venv，
不能据此证明 runtime 绑定；新入口独立验证最终真实目录和两条链接。

导入阶段使用新 venv 的绝对 Python `-I -B` 和干净环境，业务代码导入前禁止外联、进程启动和
文件写入；open、listdir、scandir 共用受审代码/依赖及标准库读取边界，不枚举私有目录。显式载入 health/builder 的 QPK 懒加载依赖与
完整 daily CLI，不调用 main 或业务函数，不注入伪模块；核所有实际 AAB/QPK 模块来源，
拒绝旧共享镜像或其它目录。AAB/QPK 现存 pyc 读取被拒绝，标准 loader 从源码编译；
缓存保留，QPK 包根还必须位于新 venv 内。失败摘要仅含固定阶段/import_target 枚举、guard_attempts 计数与 false proof；
不输出异常正文、路径或目录名，不保存凭据/原始日志。
Python 导入守卫不是 OS 沙箱；候选导入成功也不证明 systemd 实际启动、shell 的 PYTHONPATH
选择或原生 history 验收。common_env 仍会优先共享 QPK src，daily consumer 则重设 PYTHONPATH；
后续采用必须把两个服务的 source、venv/PATH、共享 QPK 和两个 timer 原状态一起绑定。
旧 failed 配置仅供配置回退；恢复 timer 可能立即触发业务。此候选不实现 switch/rollback，
不调用 Telegram prestart、health/daily 本体、sync、模型、通知或服务/timer 操作。

`stage-quant-runtime-inactive` 只接受 main、该固定 mode、`acknowledge_interruption=false`
和空 `ssh_unban_ip`，使用原 `codex-vps-ops` environment/runner/concurrency。精确 workflow
SHA、checkout HEAD、当前 main 与干净工作树必须一致，并核 setup/common_env/lock/pin 固定字节。
它排除通用 Gateway job，没有自由 source/path 参数。候选目录固定为
`/home/ubuntu/quant-monitor-data03-286757-py312-v1`；任何已存在目录、文件或链接均拒绝，
失败残留保留为未验收状态，不能自动重用、覆盖或删除。公共 QPK 源仅取官方固定 URL
`https://github.com/QuantStrategyLab/QuantPlatformKit.git` 的
`28675796cabbe137a1fa3970b70d1aa98e952c88`，在 AAB checkout 外的 RUNNER_TEMP 中为本次
run 新建独立目录；只 init/fetch/cat-file 验证 commit，setup 继续 archive 精确 SHA，不 checkout。
公共获取不接收 GH_TOKEN 或新增凭据，并以新对象目录作为独立 HOME，避免读取旧 `.netrc`；
隔离 Git global/system config、credential helper、
URL rewrite、模板及 hooks，只允许 HTTPS 并保持 TLS 验证，保留原获准代理/CA变量且不输出值。
新目录失败、拒绝/403、超时、错误 object、平台/wheel/hash/版本或 pip check/gh status 失败
均停止并输出固定失败码，保留未验收残留，不换源/版本、不清理重试。这个 job 不再传入共享
QPK 镜像路径，不改其 HEAD、refs 或 objects；AAB exact-source/dirty gate 保持原样。

显式 staging 会清除 common_env 导出的 `PYTHONPATH`，平台 preflight、venv 创建与三项 pip
调用使用 Python `-I`；清除环境路径也保护 pip 的 build backend 子进程。默认 setup 模式
保持原 argv/行为。新增 startup/pip 回归只用临时合成镜像，证明路径隔离缺口及修复，
不表示生产存在这些测试文件或遭受攻击。入口以干净环境运行，仅传固定 HOME/PATH、已有
workflow token、必要配置及 runner 已有的代理/CA 变量，值不输出；官方 PyPI、原 lock/hash/
binary 限制和 TLS 验证保留，不增加凭据、不修改代理策略。安装阶段可访问官方包源；
后置检查以清空环境的候选 Python `-I -B` 执行标准库，核 27 项 distribution 版本、候选
prefix 下的 QPK distribution 和 drift_detector/health_dashboard/performance_monitor/
return_collector 四个固定文件 hash，不导入 QPK/AAB 业务代码，不声称全包或业务验证。
本入口不调用 health/daily、模型、通知、sync、服务或 timer 操作，也不安装 immutable release。
安装输出仅在内存中按完整固定短语映射为有界 failure enum，区分缺 QPK commit、平台不支持、
锁依赖安装、QPK 安装、pip check、gh 缺失或认证失败；其余为 `unknown`。不保存或透传原始
pip/代理/路径/凭据/异常正文。过滤器成功不能吞掉 setup 的非零退出状态；过滤器失败也拒绝成功，
失败没有候选成功摘要，残留继续保持未验收状态。

## Fixed read-only runtime path snapshot / 限定只读路径快照

The separate `VPS Codex Service Ops` mode `inspect-quant-paths` runs only
`inspect_runtime_paths.py` from an exact-main, clean checkout with a clean process
environment and ordinary runner privileges. The existing `inspect-quant-runtime`
mode is unchanged. This capability does not restart services or run pipelines.

The new mode filters the seven documented root variables through fixed path
classes, in the order `PassEnvironment` → `Environment` → `EnvironmentFiles` →
`UnsetEnvironment` → reviewed helper defaults → Telegram env assignments. It
never sources env files or returns their contents or hashes. It reports only
fixed provenance categories, override flags, selected public dependency SHA256s,
the current QPK pin, and bounded service invocation metadata before/after reads.
Only the known runtime checkout and exact 40-hex release roots can supply public
code reads; the legacy Projects roots are classified without reading them.

Unsupported syntax, unknown paths, permissions, startup controls, noncanonical
PATH, and symlink traversal fail to unknown. Implicit HOME is not inferred from a
username. Paths are lexical configuration categories; filesystem identity is
reported separately and links are never resolved. This is a current config
snapshot, not proof of a running process's environment, Python import resolution,
next invocation after the Telegram pre-start refresh, full-tree identity, or
business recovery. Do not change permissions to make unknown fields readable.

独立的 `inspect-quant-paths` 模式只在精确 main、干净源码和干净进程环境下，使用
runner 原权限投影七项路径、覆盖来源、限定公开依赖哈希、QPK pin 与前后调用元数据。
它不 source 环境文件，不输出或哈希凭证内容，不重启服务、不运行 pipeline。
未知路径、语法、权限、启动控制、非标准 PATH 和符号链接均保留未知；不会仅根据
用户名推断 HOME。路径类别是配置的字面分类，真实文件身份另报且不解析链接。
当前快照不证明进程环境、Python 实际导入、下次 pre-start 后环境、全树一致或业务恢复。

## Bounded current-invocation failure evidence / 限定退出证据

The manual-only `VPS Codex Service Ops` mode `inspect-monitor-failures` is a
separate protected `codex-vps-ops` job under the existing concurrency group. It
verifies exact current main, workflow SHA, and a clean checkout, then runs
`inspect_monitor_failures.py --inspect` with `env -i` and `/usr/bin/python3 -I -B`.
It has ordinary runner privileges, no provider/admin secrets, and no sudo or
permission expansion. Existing modes and their read sets are unchanged.

The fixed whitelist is `codex-quant.service`, `codex-daily-briefing.service`, and
their two timers. Each unit has two bounded `/usr/bin/systemctl --no-pager show`
reads, before/after its evidence read. The exact properties are `Id`, `LoadState`,
`ActiveState`, `SubState`, `UnitFileState`, `InvocationID`, and `Result`; services
add `MainPID`, `ExecMainCode`, `ExecMainStatus`, `ExecMainStartTimestampMonotonic`,
`ExecMainExitTimestampMonotonic`, `ActiveEnterTimestampMonotonic`, and
`InactiveEnterTimestampMonotonic`; timers add `LastTriggerUSec`,
`LastTriggerUSecMonotonic`, `NextElapseUSecRealtime`, and `NextElapseUSecMonotonic`.
Only each service's already-known nonzero 32-hex current `InvocationID` permits
one `/usr/bin/journalctl` query, jointly matching `_SYSTEMD_UNIT` and
`_SYSTEMD_INVOCATION_ID`. Every returned record must match both again. There is
no timer journal, historical invocation enumeration, or whole-host log read.

The metadata subprocesses alone set `TZ=UTC`; host timezone is unchanged. Timer
realtime values are normalized UTC dates when supported. The CLI formats
`USecMonotonic` as timespans, so the output separately retains a bounded
`elapsed_timespan` with `clock: monotonic`, using only official duration tokens.
`0` and `infinity` remain `unset` and `infinite` sentinels; unsupported values
remain unknown. These are elapsed monotonic clock readings, not time remaining
or UTC conversions. This preserves the quant timer's `OnBootSec=2min` /
`OnUnitActiveSec=30min` evidence even without a realtime next-elapse value.
CLI formatting references: [systemd v255 property printer](https://raw.githubusercontent.com/systemd/systemd/v255/src/shared/bus-print-properties.c)
and [timespan formatter](https://raw.githubusercontent.com/systemd/systemd/v255/src/basic/time-util.c).

Each command has a five-second deadline and streamed combined stdout/stderr
byte cap (16KiB for systemd, 64KiB per service journal). Journal queries request
at most 64 records. Reaching either cap is conservatively truncated and unknown,
including exactly 64 records. Timeout/cap termination affects only the newly
started metadata child. Missing units/IDs, denied visibility, stderr, malformed
or mismatched records, and changed snapshots cannot produce a diagnosis.

Journal MESSAGE is parsed only in transient memory. Output contains fixed
stage/error counters, visibility/matching/limit booleans, normalized unit/exit
summary and UTC timer times; it never retains log text, paths, URLs, accounts,
tokens, raw InvocationIDs, or exceptions. Import/permission/missing-file and
fixed consumer-stage evidence are observations, not proof of a root cause or
Python installation damage. Exit 2 alone cannot establish a monitor alert; a
matching health-cycle summary can establish reported alert evidence. No logs
cannot establish health. `pre_start_or_start` means the main start timestamp is
zero and the result failed, not identification of a particular ExecStartPre
command. No environment/configuration files, `/proc`, venv/PATH metadata,
account/task directories, pipelines, models, gateway, deployment, or notices are
read or invoked. A source/model mismatch is not classified as a live failure.

Offline verification uses only in-memory fake metadata runners with guards
against processes, sockets, and external callbacks installed before helper
import. No arguments perform host reads; only `--inspect` does. The built-in
`--fixture-test` also exercises the collector without metadata commands:

```bash
python3 -B -m unittest discover -s ops/quant-monitor/tests -p test_inspect_monitor_failures.py -v
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C /usr/bin/python3 -I -B ops/quant-monitor/scripts/inspect_monitor_failures.py --fixture-test
```

独立手动模式 `inspect-monitor-failures` 只读取上述四个固定 unit 的限定状态、
退出摘要和调度时间，以及两个 service 已知当前 InvocationID 对应的最多 64 条、
64KiB journal；不读历史调用或整机日志。前后快照须稳定，每条记录须同时匹配 unit
和 InvocationID。每条命令限时五秒，流式读集受字节上限约束；恰达上限也保留未知。
缺失、拒绝、截断、畸形、错配或并发变化均不能确定原因，不 sudo 补读。
只对 metadata 子进程设 `TZ=UTC`，不改主机时区。纯 monotonic timer 的调度值按
CLI 的限定 duration 词法另报 `clock: monotonic` 和带单位的 elapsed_timespan；
不转 UTC，不推算剩余时间。`0`/`infinity` 单独标为 unset/infinite，未知格式保留未知。
原日志、路径、URL、账号、凭据、异常和 InvocationID 不输出、不落盘，只保留固定
枚举、计数、布尔和规范时间。退出码 2 不单独证明告警或 Python 故障，日志为空不
证明健康；源码模型不匹配也不是生产故障。本入口不启动 pipeline、不调模型、不
触任务网关、账户、交易、通知、部署或维护。真实运行须另获本新增读集范围的授权，
不能复用先前已消费的 inspect 次数；候选源码和离线测试不证明生产恢复或允许部署。

### Daily-only record-limit samples / 仅日报的有界样本

The separate manual mode `inspect-daily-failure-sample` invokes only
`inspect_monitor_failures.py --inspect-daily-only`. It retains the same protected
environment, exact-main/workflow-SHA/clean-checkout gate and concurrency group.
Its host read set is exactly two existing-property systemctl snapshots of
`codex-daily-briefing.service`, around at most one journal query for that service's
already-known current InvocationID. It never reads quant, either timer, Gateway,
or another invocation. No-argument, combined, or arbitrary CLI options are
rejected. Existing modes, including `--inspect` and default `collect`, keep their
original read sets and outputs; sample classification is enabled only here.

Limits remain 64 records, 64KiB and five seconds with ordinary runner privileges.
If the command returned normally within the byte cap and exactly 64 complete
JSON records were read, every record must first match the fixed unit and current
InvocationID. Only after all records validate may the independent
`sampled_evidence` contain fixed stage/error counts. It has `complete: false` and
`absence_proven: false`; journal remains truncated and diagnosis remains unknown.
It describes observations in a bounded sample, not a root cause or full log
visibility. Zero counts never prove the absence of errors. More than 64 lines
are rejected without classification. Byte truncation, timeout, permission/stderr
issues, malformed/duplicate JSON, wrong unit/ID, unknown metadata or changed
before/after snapshots discard all sample counts. With 63 records or fewer,
the existing normal classification applies and sampled evidence is unavailable.
Raw MESSAGE, URLs, paths, credentials and exceptions are still never retained
or emitted. No extra reads, retries, permissions or business actions were added.

独立手动模式 `inspect-daily-failure-sample` 只以严格 `--inspect-daily-only` 参数
读取 codex-daily-briefing.service 的前后限定状态和当前 InvocationID 对应的一次
journal，不读 quant、timer、Gateway 或历史调用。旧模式读集和输出保持原样。
正常返回且未触字节上限、恰有 64 条时，先逐条校验完整 JSON、unit 和 InvocationID，
全样本通过才输出独立 sampled_evidence 固定阶段/类别计数。complete/absence_proven
均为 false，诊断仍 unknown；零计数不表示没有错误，也不证明完整可见、根因或恢复。
超过 64 行不分类；字节截断、超时、拒绝、stderr、畸形、错配、未知元数据或前后变化
都丢弃样本。63 条及以下沿用原分类。上限、原权限和无原文输出边界均不变。
这不能找回先前已丢弃的 journal 原文；真实调用需要本次限定范围的明确授权。

### Recorded daily invocation error filter / 固定旧日报调用的错误筛选

Manual mode `inspect-recorded-daily-errors` runs the strict no-value flag
`--inspect-recorded-daily-errors` in its own existing-environment/concurrency job.
The source binds only `codex-daily-briefing.service` and invocation
`ff223a65dbf049cc9e9280f49c7519d5`, recorded in both before/after InvocationID
fields of [the original inspect run](https://github.com/QuantStrategyLab/AIAuditBridge/actions/runs/37414305498)
(job 112109414839). This binding does not assert that a later bool-only sample
had the same ID. If the first existing-property systemctl snapshot cannot confirm
this exact current ID, stop after that one read. Otherwise issue one fixed
journal query and one after snapshot; changed state/ID discards the result.

The exact allowlisted journal argv adds a static `--grep` alternation for
`Traceback`, the 18 fixed exception classes in `RECORDED_DAILY_EXCEPTIONS`,
`report_dir_not_found`, `runtime digest rejected`, `domain_exit=` and
`runtime_exit=`, with `--case-sensitive=yes` and `--reverse`. It jointly matches
the fixed unit and ID, with the existing 64-record/64KiB/five-second reader and
ordinary privileges. [Official journalctl semantics](https://raw.githubusercontent.com/systemd/systemd/v255/man/journalctl.xml)
filter MESSAGE by regex; grep with lines implies reverse order. This may inspect
earlier entries within this one invocation. The 64-record cap limits returned
matches, not the number of records examined internally. The byte bound is on
returned stdout/stderr, not all internal journal scanning. No priority-only
filter, extra query, free ID/regex, other unit, or permission expansion is used.

All returned records must be strict JSON and match unit, ID and the reviewed
grep. Only anchored exception lines, the exact Traceback header, existing
report-directory/runtime-rejection markers, and complete domain/runtime exit
pairs with each code in 0..255 enter `filtered_evidence`. It contains only fixed
class/category/stage counts and legal exit-code pairs. Diagnosis remains unknown;
`complete` and `absence_proven` stay false. This is selective error evidence,
not full visibility, a first/root cause, Python damage, or business recovery.
Empty/unclassified results, exactly 64 or more returned lines, byte truncation,
timeout, stderr/permission failure, malformed/mismatched records, unknown state,
or snapshot changes end unknown without expanding words, IDs, history or retries.
No raw MESSAGE, traceback text, URL, path, account, token or raw ID is emitted or
saved. Previous modes and default collectors are unchanged. No pipeline, model,
Gateway, deployment, restart, account, transaction or notification is invoked.

独立模式和严格无值参数 inspect-recorded-daily-errors 只绑定原 run/job 前后共同记录
的固定旧日报 ID，不把后来的布尔样本当作同 ID 证明。前置快照无法确认即停止；
确认后仅一次固定 grep 查询和后快照。筛词、unit、ID、命令白名单均固定，不按 err
优先级替代错误文本，也不允许自由参数。64 条/64KiB 是返回读集上限，内部可在同一
调用内检查更早记录；不是“只扫描 64 条”。五秒及原权限不变。
只输出白名单类别/阶段计数和 0..255 的退出码，完整性和错误不存在证明均为 false，
诊断仍 unknown。空/无有效分类、截断、失败、错配或状态变化就结束，不扩词重试。
不留原日志或异常文本，不读其他调用，不执行业务或维护动作；旧模式行为不变。

## Daily result receipts / 日报结果摘要

The existing daily consumer now emits one bounded stderr line at its explicit
runtime refusal or domain/runtime return boundary, prefixed
`[briefing-result:v1]`. It projects only fixed `branch`, `stage`, `reason`,
`action`, `dispatch_failed` and exit-code values. Unknown reasons become
`unknown`; missing or partial dispatch evidence keeps `dispatch_failed=unknown`.
A false flag requires the existing dispatcher's complete, typed result fields;
it means the result reports no failure, not that a message was delivered.
No exception text, message body, path, object URI, date, target key, account
identity or credentials enter this new line. Existing stdout JSON is unchanged,
and the pipeline still discards runtime stdout.

The pipeline's `[briefing-pipeline-result:v1]` line separately records
`builder_exit`, `consumer_exit` (`not_run` if skipped) and the existing
`domain_exit`. The first nonzero domain result still wins: a builder failure is
not replaced by a later consumer result. With the runtime switch enabled, either
branch failing still makes the wrapper exit 1. A Telegram-routed domain result
still exits 2 even after a successful send; this is a routing outcome, not proof
of a Python crash or delivery failure. These receipts add no calls, retries,
notifications, ledger changes or new diagnostic workflow, and do not establish
production adoption or recovery.

日报消费者只在既有明确 runtime 拒绝或 domain/runtime 返回边界向 stderr 输出
一条有版本前缀的固定值摘要。未知原因和不完整送达证据保持 `unknown`，不把缺少证据
写成送达健康；`false` 须有既有 dispatcher 的完整、有类型结果字段支持，仅表示原结果
未报失败，不证明消息已送达。新日志不含异常正文、消息正文、路径、对象 URI、日期、目标键、账号或
凭据。pipeline 单独记录 builder 与 consumer 的退出码，未执行记为 `not_run`；
保留 domain 首个非零、runtime 启用时 wrapper 失败返回 1、Telegram 路由成功发送后
仍返回 2，以及原 stdout JSON/runtime stdout 丢弃规则。不新增调用、重试、通知、
状态写入或诊断 workflow；源码与本地合成验证不证明生产已采用或恢复。

## Agent 运行参考（由原项目指令迁入）

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
AIAUDIT_BRIDGE_SOURCE_SHA="$REVIEWED_MAIN_SHA" bash ops/quant-monitor/scripts/deploy_to_vps.sh
```

Set `REVIEWED_MAIN_SHA` to the exact reviewed 40-character commit. It must match fetched `origin/main`; the deploy script refuses an omitted or mismatched SHA.

## Codex 执行纪律

- 不要手填 token/chat id 到仓库
- 报警只走量化哨兵 bot
- 策略健康证据只进入可审计的 issue-only 优化队列，不自动改策略、参数、仓位或部署
- 策略劣化记录成功后不通知人；只对数据/运行风险和记录失败 fail-closed 通知
- Telegram 送达复用 `data/alert-state/health_cycle.json`，按事件和目标哈希记录 `pending` / `sent` / `failed` / `unknown`。发送前写不进去就停止，不退回无状态发送。明确成功才是 `sent`；服务明确拒绝是 `failed`，最多再试一次；超时、响应丢失或进程中断留下的 `pending` 都按 `unknown`，不盲重发。已成功目标不因其他目标失败而重发。状态不保存 token、正文或原始 chat id。`dry_run` / `send_dry_run` 不写这份状态。旧 fingerprint 且没有目标记录时，整次事件保持不重发，也不补写成各目标已送达。健康事件归属在首次 `pending` 时和送达记录一起写入，不表示已经成功；`fingerprint` 仍只在该事件全部目标 `sent` 后更新。健康恢复清 `health_sent_events` 里本周期每个健康事件的已确认 `sent`，不只清 fingerprint 指向的一条；`unknown` / `pending` / `failed`、日报送达和诊断尝试保留。诊断写入、恢复清理和送达更新共用同一文件锁完成读改写。这里不切换各平台自己的休市或订单心跳。
