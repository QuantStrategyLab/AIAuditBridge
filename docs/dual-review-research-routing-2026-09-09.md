# 研究双审：Codex 主审与默认付费旁路收紧

本切片基于 `39e2d2b70f3cdb9a06c855dfaba4de66ed444f13`。下述结果为源码与离线集成验证，不包含部署、真实模型或 workflow 运行证据。此前日报和 Cursor 切片的发布不代表本次双审已采用。

## 本次行为

- `promotion`、`hit_rate`、`drift` 的实际 `run_dual_review_pipeline.py` 主审入口统一调用 Codex `research_stage=promotion_review / reasoning_effort=xhigh / mode=review_only`。型号由服务现有研究阶段策略和质量/额度准入选择，回写实际 provider/model/effort；即使全局研究 provider 配置为 Cursor，此条主审仍只批准 Codex。
- 新研究主审要求 GitHub Actions OIDC，配置静态服务 token 不能替代；缺身份、配置、能力或额度时返回 `review_unavailable`，失败内容固定脱敏。延期仅保留白名单 `retry_at`，未知为 null。旧的 `DUAL_REVIEW_GATE_ALLOW_SKIP` 不适用于研究配置失败；三种研究触发不会因此返回成功跳过。
- 低信心主审触发的默认 secondary 不再选择 gateway `analyze` 或直接 API。它只返回 GPT/Claude 两个明确 `executed=false / source=not_executed / verdict=review_unavailable` 槽位，没有第二、第三份模型意见。`DUAL_REVIEW_SECONDARY_MODE=dual_api` 不能绕过这个研究默认边界。
- 不改变比较算法：低信心有效主审加两份 unavailable 仍是 disagreement/退出码 2；全部 unavailable 为降级/退出码 3；无效主审仍阻断。保留既有高信心主审无需追加复审的行为，没有新增自动通过条件。
- 保留现有显式人工结果入口 `orchestrate_from_payload(..., secondary_review=...)`、`run_dual_review.py --secondary-review` 以及内部显式 reviewer 注入接口。人工提交结果仍按原规则比较；本次没有新增“已验收”标志、伪造票数或将两次 Codex 调用标成 GPT/Claude。
- `context` 只能提供证据上下文。trigger、strategy_profile/profile、primary_review、secondary_review 不可覆盖控制路径；冲突在模型调用前拒绝。主审、研究分流、比较和资金分支使用同一个规范 trigger。

`reconciliation_baseline` 的旧主审/API secondary 选择、默认 GPT/Claude 双槽路径的全部审阅者要求、candidate SHA 绑定、人工恢复批准与 `recovery_authority=escalate` 完整保留。历史人工接口仍允许单份 legacy secondary 参与双票比较，此既有例外未在本轮收紧，不能把所有历史接口都描述为强制三票。非研究直接 API 用途没有关闭，旧恢复路径的配置行为没有改变。本次结果依然只是审阅意见，不授予下单、资金恢复或候选实盘权限。

独立审查另确认并修复 SDK 的研究任务身份缺口：此前只有包含 Cursor 的订阅路由检查每次轮询的 job_id，纯 Codex 研究可能接受另一任务的成功结果。当前 `client/gateway_client.py` 对所有 `research_stage` 请求都验证准入 provider/stage/model/effort，并固定提交得到的 job_id 和路由；每次轮询（包括 running、failed）必须一致，显式指定型号/推理等级也必须在准入时匹配。普通非研究 legacy 请求不变。现有服务早已提供这些字段，没有更改服务 schema/能力版本或补造缺字段兼容；实际消费者必须安装或采用本次 SDK 才获得该保护。

## 实际调用与采用限制

CN/US 的 `evidence-gate.yml` 已调用各自 `gate_evidence_package.py`，后者调用 AAB `--from-evidence`。本轮只核对本地源码和 OIDC 声明，未验证生产服务 allowlist、真实证据路径或成功审阅。

CN/US 的 drift workflow 经 QPK reusable workflow 调用 `run_drift_dual_review.py`。当前 CN 研究工作区的 drift workflow 三处固定 QPK `c812ed70…`；该版本和本轮 QPK `d6c5dc43…` 的 reusable workflow 均固定 AAB `2351c987…`。因此仅发布这次 AAB、甚至仅更新 CN 的包依赖，都不证明工作流采用。其他消费者须按各自实际 ref 核对。命中率存在显式 payload/日报元数据入口，尚未确认独立定时主审生产者。没有扩大本轮为跨仓追 pin。

真实三份自动复审仍延期：需要获准身份、启用且费用范围明确的模型渠道，以及已核实的独立 GPT/Claude 具体型号、质量和执行证据。未满足前保持不可用，不为接通流程造票。偏离入口仍缺完整来源时钟/实验输入，未在本切片补造回测、shadow 或晋级证据。

调用者也必须正确传播失败：CN 的 `_run_promotion_dual_review` 原先仅在最大退出码 ≥2 时阻断，退出码 1 或信号终止会被漏过。同轮 [CN PR #254](https://github.com/QuantStrategyLab/CnEquityStrategies/pull/254) `af79f6a` 已单独修复，真实本地子进程回归通过；其他同类消费者须另行核对，不能视为已由本 AAB 切片修复。本切片的研究配置/能力/额度不可用沿退出码 3 报告。

既有 `--dry-run` 只限制通知分发，**不是零模型开关**，本切片未改变该含义。所有本次验证使用合成输入和 mock HTTP，未运行真实模型或通知。

## 验证

首批新增回归在旧源码实测 `12 failed, 2 passed`：阶段缺失 3 例、两种默认付费旁路 6 例、context 覆盖 3 例。后续又先复现 3 例“研究缺配置沿旧 ALLOW_SKIP 成功跳过”，再修复为 unavailable。

初批使用 `/usr/local/bin/python3`（Python 3.13.7、pytest 9.1.1），相关组 **107 passed + 47 subtests passed**；该结果发生在 SDK 身份补丁之前。随后 SDK 新回归先实际失败 15 项（含 14 个 subtests），再修复；两份既有 diagnosis HTTP fixture 补齐实际服务的准入/完成 metadata。

最终把当前 `client/` 与 `pyproject.toml` 的临时副本离线安装进新 venv，使用 `-I -B` 先从安装目录导入 SDK，并确认安装文件与当前源码字节一致。测试进程再将 AAB 内部 `client` 名称映射至已安装 `ai_gateway_client`，让既有两份 diagnosis 调用者及研究 SDK 测试实际执行新安装包；这是测试适配，不是生产兼容层。venv 共享主机的 pytest/setuptools，SDK 则重新独立安装；全程拦截真实 socket 网络连接。最终 **205 passed + 114 subtests passed**，安装导出检查未跳过。没有沿用旧 Cursor 切片的安装结论。

测试包括真实 CLI→当前 SDK 的合成 OIDC、health、排队、轮询，错 job/provider/model/stage/effort、非法输出、失败脱敏和延期，以及资金恢复和人工三票比较回归。初批命令见 `docs/validation/2026-09-09-dual-review-research/validation.json`；最终 SDK 增量结果与安装环境另见同目录 `sdk-identity-validation.json`、`installed-sdk.json` 和日志。此前 107+47 不能作为 SDK 新补丁的验证；这些离线验证也不证明真实金融意见正确或线上闭环成功。
