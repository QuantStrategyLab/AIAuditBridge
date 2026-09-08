# 监测同步与偏离时效修复交接（2026-09-08）

对应全局审计 F01/F02、S0/S1。下文记录源码及本地验收时的结果；验收时尚未执行 Git 发布、部署、重启、通知、真实模型调用或交易。后续 Git 交付状态以对应 PR 为准；本地验证不能证明现网采用或监测恢复。

## F01：源码镜像被 editable 安装生成物弄脏

VPS 只读取证确认：QPK 专用镜像只有两个已跟踪文件被改动：`src/quant_platform_kit.egg-info/PKG-INFO`、`SOURCES.txt`。当前唯一 QPK 已安装包的 `direct_url.json` 声明 editable=true，且指向该镜像。源码 `setup_vps_runtime.sh` 的 `pip install -e` 会生成这些元数据，与 checkout 拒绝覆盖的故障链一致。

修改 AAB：

- `ops/quant-monitor/scripts/sync_strategy_repos.sh`：仅允许这两份未暂存的生成物进入恢复分支；先准备干净替代目录，再完整保留旧镜像为固定的 `.preserved-before-metadata-refresh` 目录。替换失败恢复原目录。未知、暂存改动不允许带入更新后的源码。已有保留目录或同步锁时拒绝继续，不覆盖旧材料，不无限积累备份。
- `ops/quant-monitor/scripts/setup_vps_runtime.sh`：先检查镜像干净，再从临时 Git archive 安装；元数据生成留在临时构建目录。运行仍通过既有 PYTHONPATH 消费同步镜像。
- `tests/test_quant_monitor_sync_strategy_repos.py`：使用本地临时 Git 仓复现；无网络仓库、真实安装或远端写入。覆盖完整旧镜像/未跟踪文件保留、重复运行、未知变更拒绝、安装隔离与替换失败回滚。
- `ops/quant-monitor/README.md`：说明两次目录重命名是可恢复切换，不是同时原子替换。强制终止后须核对锁与保留目录，不能自动删除它们。

RED：未修改脚本时，两项真实 Git 回归失败，分别证明 checkout 被生成物阻断、未知非冲突变更可被带入更新后的源码。GREEN：5 项同步/安装测试通过，既有 48 项监测测试通过。

## F02：旧记录不再成为当前研究准入

修改 QPK 的具体调用链：

| 文件（均相对于 `src/quant_platform_kit`） | 作用 |
| --- | --- |
| `strategy_lifecycle/contracts.py` | DriftResult、StrategyPerformanceSnapshot 的 `as_of` 可为 None；原日期正确的记录保持原语义。DriftResult 保留 source_revision。缺日期健康评分使用 None 而非零分/假日期。 |
| `strategy_lifecycle/performance_store.py` | 缺失/非法日期保留为 None，不再替换成今天；保留有效分数/状态用于风险诊断。缺日期记录禁止写为新观察。 |
| `strategy_lifecycle/production_drift_health_probe.py` | 保留观察日期、评估时钟、有效期、来源版本、基准 artifact/参数身份；过期、未来、未知时效或不匹配来源返回 unavailable/actionable=false。 |
| `risk/production_drift_new_risk.py` | 仅保留 review/critical 的严格 risk_status；不能用该字段把 unavailable 变 healthy/watch，也不能用它覆盖现有 critical 为更宽松状态。 |
| `strategy_lifecycle/promotion_actionable_runner.py` | 在研究调用前拒绝不可用观察；构造周期输入时用观察本身的日期、source_revision 和基准身份。 |
| `strategy_lifecycle/codex_integration.py` | 缺日期/过期观察在 Issue、Codex 调用之前停止；自动研究要求同目标、同日、版本明确的 snapshot。 |
| `strategy_lifecycle/drift_detector.py` | 新计算保留 source_revision；缺日期输入不产生新的漂移结果，旧风险记录保留。 |
| `strategy_lifecycle/drift_alerts.py` | 缺日期旧记录不产生新的策略优化告警。 |
| `strategy_lifecycle/performance_export.py` | 缺日期数据不导出为可信研究证据。 |
| `strategy_lifecycle/strategy_health_score.py`、`health_dashboard.py` | 缺日期策略显示 unavailable、日期和分数为 null；同批其他策略继续正常评分及输出。 |

新增测试 `tests/test_production_drift_freshness.py`。同时更新既有 probe、actionable runner 与 Codex integration 测试的观察来源及显式测试时钟，保留原 HITL/模型额度改动。

公共语义：

- `probe_production_drift_health*` 增加 `evaluation_date` 与 `max_age_days`；默认当前 UTC 日期、7 个自然日，与已有监测 artifact 的 168 小时预算相衔接。这是按日期计算的保守默认，**不是精确 168 小时或交易日历验证**；节假日/策略专用频率须由实际 consumer 显式配置。
- `as_of` 始终代表来源观察日期。from-store 的 `as_of` 只核验期望日期，不能改写旧记录；历史回放只能显式传入 `evaluation_date`。CLI 对应 `--evaluation-date`、`--max-age-days`。
- `as_of=null` 表示不知道观察日期，没有 `date.min` 或“今天”哨兵。未知时效不是真实当日结果。
- 最新 DriftResult 与 snapshot 必须同目标、同日期；snapshot 的 source_revision 必须非空，已有 drift source_revision 不能与其冲突。基准 artifact 身份和观察来源版本分开。该检查消费既有 store 信任边界，不把一个版本字符串当成独立的真实性证明。
- 研究 `status=unavailable/actionable=false` 与已知 `risk_status=review/critical` 可同时成立：前者停止新研究，后者延续已有 NEW_RISK 禁令。缺记录仍维持既有可选风险轴语义，不擅自改成全平台停单。
- **probe 与 risk mapper 必须随同一个已验证 QPK 安装包发布和采用；不能只复制新 probe 给旧 risk mapper。** 旧 mapper 会丢弃 unavailable，可能错误解除该条禁令。

日期消费者审计：QPK 内已逐项检查 store key/序列化、detector、alert、Codex Issue/模型入口、actionable runner、performance export、health score/dashboard。performance_monitor 产生的日期来自已验证返回序列，写入仍经过 store 日期校验。其他已配置平台的 production_drift_health_observe 只透传 probe 摘要；搜到的其他 `.as_of` 为账户/策略 snapshot 合同，不是本次 lifecycle 类型，未扩改。

消费者采用后需要验证：LongBridge/IBKR/Schwab/Firstrade 的观察脚本保留新增字段；目标停用状态仍由实际 runtime-target 配置判断，不因旧 critical 恢复目标。QRT/监测前端应呈现 unavailable 与原风险禁令的区别；不能通过字段丢弃变成 healthy。没有全组织追 pin 或平台配置改动。

## 实际验证与材料

本地工作区的日志和精确解释器、cwd、命令位于 `docs/validation/2026-09-08-monitor-drift/`（机器相关诊断材料未纳入 Git；远端验证以 PR 的实际 CI 为准）：

- `qpk-final.json` / `.log`：Python 3.13.7，16 个相关测试文件，**219 passed，14 subtests passed**。包括风险禁令、历史重放、缺时间、来源不匹配、完整 HITL/paired shadow，以及 date.max/极大有效期的受控拒绝。
- `aab-monitor.json` / `.log`：48 项既有监测测试。
- `aab-sync.json` / `.log`：5 项真实临时 Git/安装回归。
- `aab-codex-integration.json` / `.log`：临时 venv 中实际离线安装当前 SDK 后，**107 passed，57 subtests passed**。安装使用 `--no-index --no-deps --no-build-isolation`，SDK 来源仅当前 client/pyproject；`-I` 子进程验证安装包的真实导出，未靠源码 PYTHONPATH 绕过。
- `sdk-install.log`：该安装的原始结果。
- `red-and-remote-observations.json`：记录最初 RED 的工具输出标识及两次脱敏 VPS 只读结果。
- `aab-sdk-environment-failure.log`：初次通用解释器未安装 SDK 的环境失败，未记为业务回归。
- `aab-*-environment-failure.*`：尝试剥除全部环境变量时，既有 common_env 缺 HOME 导致的测试环境失败。最终验证保留真实 HOME 原值；没有改写 HOME，也没有引入凭据环境变量。

另执行了两份 shell 的语法检查、改动 Python 的 ruff 和 git diff --check，均通过。未执行全组织测试或真实回测/模型/券商调用。

## 真实恢复前最小验收

1. 独立审查通过后按实际 Git/部署授权交付。先确定 AAB 服务/SDK/QPK 的具体发布与采用版本；保留原本尚未提交的其他 HITL/UI 改动，不能混淆“本地通过”和“部署采用”。
2. 只读核对专用镜像仍仅有允许的两个未暂存生成物，检查固定保留目录和同步锁、可用空间；存在未知改动或并发 writer 时仅停止这条同步操作。
3. 在部署授权内采用安装隔离修复及同步脚本；确保新导入路径与已安装版本符合预期，旧镜像完整保留，不强制覆盖。bootstrap 的源码/依赖安装有独立网络副作用，不能用它冒充只读检查。
4. 等待下一次本来计划的监测周期；读回服务结果、mirror clean、最新完整 artifact 和控制台时间。不要直接运行 health_check 来“验证”，它会同步并可能写 Issue/通知；如需主动运行须明确授权其副作用。
5. 用真实存储的受限元数据核验观察日期、来源版本和状态；过期/未知不启动研究，已知严格风险仍存在，其他健康策略仍可显示。随后才使用合法新鲜输入验证一个真实研究例子。

本次没有修复其他独立 F03 输入任务、Gateway/router observer、云权限、实盘配置或费用策略；这些不因本次源码验证而自动解锁。
