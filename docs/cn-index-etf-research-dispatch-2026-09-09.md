# CN 指数 ETF 的受控研究作业

本片从 AAB `dfccd5e21fa5e5ed0c3d226845ee988bdbab572a` 接续。源码验证、依赖采用、生产部署和真实研究周期分别验收；下列接线不代表已有真实数据或 shadow 成功。

## 实际入口和权限

已有 `strategy_optimization_watcher.yml` 的 CN 分支增加单一 VPS job。只有 main、`CN_INDEX_ETF_RESEARCH_ENABLED=true`、上游来源为 `QuantStrategyLab/CnEquitySnapshotPipelines` 且非 dry-run 时运行。它下载同一次 workflow 的原 watcher artifact，执行 `scripts.run_cn_index_etf_research`。现有来源 allowlist、来源 workflow 配置和排班不自动扩展；CN producer 未获配置或没有合法任务时不能执行。

作业使用 `/opt/codex-cn-index-etf-research/venv/bin/python`，固定并发组 `cn-index-etf-research-vps`，`cancel-in-progress=false`。QPK 的目录锁与票据位于 `/var/lib/codex-audit-bridge/cn-index-etf-research/research_promotion_tickets`；这只保证该 VPS、同一持久目录的串行执行，不声称跨主机互斥。

授权来自 root-owned `/etc/codex-audit-bridge-policy/cn-index-etf-research.json`。文件及父目录不得被普通用户或组改写；初始模板 `ops/codex-audit/cn-index-etf-research.json.example` 是 disabled 且所有实际输入留空。watcher 的 `strategy_diagnosis` task 只作触发证据，仍保持原 P3/no-order/size-zero 权限，不能替代这份独立配置。模板的示例费用是模拟执行假设，不是实盘预算。

模型调用只走实际 Actions OIDC 的既有 SDK `execute`，固定 Codex、`research_stage=optimization`、`review_only`，使用精确 CN 代码版本作为 `source_ref`。服务仍独立验证 OIDC 身份及允许的 workflow；脚本检查环境字段并不自行签发身份。静态 service token 被拒绝，无付费 API/Cursor fallback。SDK 固定每次提交及轮询的 job/provider/stage/model/effort。明确额度延期交给原票据保存 `retry_at`；恢复后实际 execute 前仍检查冻结窗口截止，错过时返回确定的 `optimization_needed=false / forward_window_start_elapsed` 并终止该次研究，零新 HTTP/试验。已完成 diagnosis 的 shadow/awaiting 尾部不会再调用这一模型闭包。未知结果不重新调用。

OIDC `repository` 保持 `QuantStrategyLab/AIAuditBridge`，请求 `source_repository` 为 `QuantStrategyLab/CnEquityStrategies`。既有服务实际验证 caller/direct-repository、workflow/ref allowlist，以及 source allowlist 和同组织边界，不能把 CN 源码声明改成 CN OIDC 身份。本轮部署负责人已只读核实生产默认 OIDC、AAB caller/direct、watcher workflow@main、main ref、CN source 和 public visibility 均匹配；没有发模型请求，这不是实际 Actions job 的身份验收。

## 输入与版本

配置必须明确三个实际安装版本：CN `code_revision`、QPK `qpk_revision`、AAB SDK `sdk_revision`，均为已批准的 40 位 Git commit。运行时核对已安装 distribution 的 `direct_url.json` VCS commit；不能用裸版本号、editable 临时源码或本地 wheel 路径冒充该部署验证。解释器及包由部署方安装，workflow 不安装依赖或修改代码。

`inputs.development/validation` 各自绑定已有本地许可数据包路径和已批准 manifest SHA-256。CN 原 `read_index_etf_input` 读取 `research_input_manifest.v1.json`，再用原 `preflight_index_etf_research_job` 验证开发、三折 WFA、锁定 OOS、费用及实际代码。返回的五字段 identity 必须等于 root policy 已冻结值；参数空间由既有 CN 模块限定为 12 个组合，模型不能改变。身份计算包括实际源码字节，不能只改环境变量或沿用旧 Git ref 来复用旧实验。

`drift.path` 引用真实生产 `DriftResult.to_dict()` 文件，绑定 profile/domain/source_revision，保留原 `as_of` 和有限 score，拒绝 future、suppressed、缺 baseline 或状态不一致。新研究仍由 QPK 原 7 个自然日时效门拒绝旧观测。task.created_at 不会变成观测日期，severity 不会生成 score。历史 task 和原观测仅可定位已经完成前置阶段的同一 shadow 票据；是否准许只读恢复由 QPK 持锁检查决定。

新票据首次保存、任何模型或回测之前，QPK 在同一目录锁内调用 `admit_one_new_experiment(ticket_dir, created_at)`。它先用同一个将写入票据的 UTC 时间确认仍严格早于已冻结首 session 的 09:25+08:00；错过这一点的新实验在模型、回测、当日票据保存前拒绝。已有票据不会重做新实验准入。然后只数原目录票据：当日已有一个、坏文件、未来/缺失时间或未知身份则拒绝。已有终态/unknown/pending 票据不再次准入，没有第二计数表或队列。试验与逐次参数记录由 CN 原函数存入独立实验目录。

## 真实 shadow 读取与候选

`shadow.forward_policy` 是完整 `ForwardObservationPolicy` 构造参数，候选、窗口、要求交易日数、基准和理由必须来自实际批准策略，不继承其他市场的默认期限。当前入口仅支持 XSHG 固定窗口、shadow-only；没有 paper、broker 或下单路径。`calendar_path/calendar_sha256` 绑定真实交易日历 JSON 日期数组；不能用工作日推算代替。

`shadow.observation_path` 由拥有观测责任的 producer 提供，顶层包含 `strategy_profile/domain/source_revision/research_identity/current_params/proposed_params/observations`。前六项必须匹配实际保存的 proposal、CN 版本及五字段身份。`observations` 是从窗口第一交易日至当前交易日的顺序数组；每项沿既有 paired adapter 字段：`forward_observation_receipt/baseline_id/observed_at/input_snapshot_sha256/candidate/baseline`。reader 使用 root policy 注入 `ForwardObservationPolicy`，逐项验证原 receipt 和 paired evidence，再把已验证前项传给后项。不会创建或补写 forward receipt。

`frozen_dependency_digests` 必须提供 `p2_config/p3_evidence/risk_policy/strategy_release/plugin_bundle` 的已批准真实根；每条 receipt 必须一致，p1 manifest 则绑定本次实际 input snapshot。baseline_id、当前/候选参数、代码和来源必须匹配；proposal 必须严格早于首个计入交易日的 09:25+08:00（含等于也拒绝，沿 CN next-open 的冻结时序）。每条观测的上海时区日期必须等于它声明的 session，时间不得早于 proposal 或该 session 的 15:00 收盘，亦不得晚于当前时间。候选产生过晚时，需由拥有配置责任者从真实日历选择下一合格窗口；reader 不改写已有 policy 日期。完整连续链必须覆盖真实 calendar 的全部要求交易日，单条合法 receipt 不能冒充已完成窗口。

forward policy、真实 calendar 摘要、固定来源摘要和 baseline 配置在模型前校验。缺 observation 文件或合法窗口尚未收满时返回 pending，按配置的 60–86400 秒后仅重读同一个 provider。QPK 复用已完成 AI/优化/回测，禁止重新调用初始 record callback。坏链或错身份明确失败；读取结果未知仍按 unknown 停车。完成时将完整 observation 交回 QPK 原 paired validator，之后才可能到 awaiting_human。旧观测跨 7 日只能继续这条已验证尾部，不能改写 as_of 创建新研究。

`console` 配置包含同一 HTTPS 站点的 `sync_url/pull_url` 与受限 `token_path`；token 由批准的本地文件消费，不进入环境、输出或工件。使用原 `make_console_research_promotion_sync/pull`：先 GET，只有确认不存在才 POST；未知提交后仅 GET 回收；人工接受仍只是意图，`live_authority_granted=false`。未配置 console 或输入时在模型前停车，零 POST。

## 发布与真实验收仍需的材料

部署方需一次采用最终通过审查并已发布的 CN/QPK/SDK commit，建立固定解释器与持久目录，保留原服务配置、凭据及 monitor 的两份本地文档改动。先保持变量及 policy disabled，验证安装来源、缺配置出口与零模型/零 POST，再配置真实许可输入、观测文件、原始 drift 和 console 只读/提交权限。

目前没有真实 CN 许可数据包、完整 forward producer 工件或生产周期成功证据。离线测试使用明确合成结构与拦截 HTTP，不得称作真实回测晋级。CLI 只输出固定状态、任务 ID、原观测日期/来源、研究 key、是否恢复/确认及延期时间；上传此摘要，不上传原始行情、许可证、完整票据、shadow legs 或底层异常。


## 本轮离线验证（2026-09-09）

生产与测试冻结为这六个文件：原 watcher workflow、required CI 的隔离验证步骤、新受控入口及专项测试、disabled 配置模板、本交接。没有改模型 HTTP 服务、SDK 协议、交易入口或第二队列。最终采用版本：

- CN：`2a0c5c9aafacfbe6519fb4029ca4ac18e4996a66`。
- QPK：`b5654244aa5d08bce2b4b4f931436268d57216df`。
- AAB SDK：`60bd64a2ae059a082614181eeb845b46df395523`。

这三个 Git commit 已实际非 editable 安装至 `/tmp/aab-cn-final-integration-20260909`。初次安装器复用全局同版本号旧 CN，真实 identity gate 拒绝；在隔离环境显式安装精确版本后，`direct_url.json` 和 import 位置均来自该 venv。没有使用 QPK/CN source overlay 代替安装结果。

验证命令为该 venv 的 `python -m pytest -q -p no:cacheprovider --tb=short tests/test_run_cn_index_etf_research.py tests/test_run_strategy_optimization_watcher.py tests/test_strategy_optimization_watcher_workflow.py`。实际以清空环境、`TZ=UTC`、禁自动插件/pyc，并拦截 socket connect 的 wrapper 运行，**97 passed，无 skip**。日志位于 `/tmp/aab-cn-dispatch-focused-20260909.log`。Ruff、actionlint、`git diff --check` 均通过。

关键回归先出现失败再修复：旧入口不存在；浅层编排未调用真实 runner；shadow 缺真实 reader；安装来源未拒绝混合 editable/VCS 元数据；future session 可被早于该交易日的时间标记完成；旧 session 可事后补时间，或首日收盘后才生成候选仍被计为 forward；已过冻结点的新实验未在 AI 前拒绝；shadow 配置错误在模型后才暴露；QRT reader 的静默 printer 不接受实际 `flush` 参数。最终真实 CN reader/preflight/数字 optimizer/QPK tickets 和已安装 SDK 均参与离线测试：合成平价数据运行 baseline + 12 组后正确 reject，同一票据不再调用模型，新观测仍受 UTC 每日一项限制。完整 shadow 两 receipt 用例和 QRT GET→POST未知→GET、再次只 GET 都通过各自实际 adapter；不将这些分段离线结果说成真实候选晋级。

新增 SDK→实际服务 auth/execute handler 的八个隔离用例：允许的 AAB 身份与 CN source 正常提交；错 repository/workflow/ref/direct/source、跨组织和静态 token 均零 job、零扣额。RSA/JWKS、额度和模型执行使用明确测试替身；实际签名、远端 job 和 forward producer 仍需部署后的独立证据。

额外运行未改动的 `test_verify_subscription_ai_consumer.py` 时出现 8 个环境依赖失败（组合 158 passed）：它导入本机全局旧 QSP `1f3a27b8fd83d71b583f4f5160a748e95fbefaa1`，触发旧 feedback/HTTP 行为。本轮未安装 QSP，也未修改该脚本或测试，两者与基线一致；不把这次扩展运行称为通过。原输出保留在 `/tmp/aab-cn-final-tests-20260909.log`，不扩大本片写集。


后续经独立审查补齐同一 shadow 时间因果边界和 CI 采用。`.github/workflows/ci.yml` 在现有 required `test` job 内增加独立 venv 步骤：安装 `cn-equity-strategies[research]` 的上述精确 commit，其已发布依赖绑定 QPK/SDK 两个精确版本；执行 `pip check` 后拦截真实 socket，运行整个 CN 专项。原主 suite 仅显式排除这一个文件，由同一个 required `test` job 的隔离 venv 承接；专项任何失败都会使该 required check 失败。没有新增可选 job、`needs`/skip 门或更改远端分支保护，既有 QSP 依赖与测试不变。专项已取消所有 `importorskip`；缺包、错 pin 或实际调用断裂都失败，不以 skip 代替覆盖。

该 CI 安装过程又在全新、不继承 system-site-packages 的 `/tmp/aab-cn-ci-rehearsal-20260909` 实际重演，只有 `pytest` 与固定 `CN[research]` 及其依赖。实际 Python 为 3.13.7（GitHub CI 配置 3.12）。`pip check` 通过；最后三文件组合 **103 passed，无 skip**，其中 CN 专项 77、原 watcher 26。最终三文件组合日志位于 `/tmp/aab-cn-ci-rehearsal-20260909.log`；前面的 97 项是前一冻结点结果。此前将隔离安装/运行移动到既有 required job 的结构回归实际先 RED 后该单例 1 passed。最终 103 项还覆盖后补的 quota-deferred 跨截止恢复：旧代码重复产生 5 个 HTTP 请求，修复后仅首次 429 准入的 3 个请求，零试验，原票据确定性终止。Ruff/actionlint/diffcheck 均通过。未在此环境安装 QSP，也不将前述 QSP 扩展检查的失败写成通过。


独立 reviewer 已完成全部源码及增量审查，无未关闭 P1/P2：原 97 项、时间边界定向验证及最后实际 caller/额度延期/required CI 的 5 项均通过；包含首 session 09:24:59 允许、等于 09:25 和收盘后冻结拒绝。评审不等于真实数据或线上模型验收，发布/部署仍由主任务统一执行。
