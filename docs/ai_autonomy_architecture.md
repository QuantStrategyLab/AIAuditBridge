# QuantStrategyLab AI Autonomy Architecture Draft

> 目标：评估当前 AIAuditBridge / Codex review-gate / monthly audit / dashboard-org-health / auto-merge / QSL 管理链路，是否足够支撑“策略智能监控优化、月度报告审计、健康状态检测、AI 自动修复、自动合并 PR”的无人值守目标。

## 结论

当前架构已经足以支撑**受控自动化**，但**还不足以直接支撑完全无人值守**。

它已经把下面几件事做对了：

- 把 Codex / OpenAI / Anthropic 的调用边界收进 AIAuditBridge，而不是散落在各个源仓库里。
- 用 OIDC、repo allowlist、workflow/ref allowlist、payload 校验、路径白名单、quota、health、review gate、guarded auto-merge 把高风险动作隔离开。
- 把“能自动做”的范围收敛到了低风险文档、测试、月度报告类修复，以及有明确 policy 约束的 PR 自动合并。

但它仍然缺少几个让“无人值守”真正成立的条件：

- 健康检测还偏服务级，不是策略级和组织级闭环。
- 月报审计与策略优化没有统一的持久化记忆和评价指标。
- 自动修复可以产出补丁，但高风险变更仍必须人工审计。
- 自动合并依赖源仓库自己的 CI / merge guard，AIAuditBridge 不能直接替代最终合并决策。
- QSL / 版本管理 / internal dependency 管理仍应保持在它们自己的治理链路里，不应该被桥接层吞掉。

所以更准确的目标不是“完全无人值守”，而是：

> **低风险自动执行 + 中风险建议式执行 + 高风险人工审计 + 所有结果可追溯可回滚。**

---

## 1. 当前架构盘点

### 1.1 入口与职责

AIAuditBridge 是 QuantStrategyLab 的 AI 审计控制面，负责：

- 接收源仓库的月度审计 / PR review 请求；
- 通过 GitHub Actions OIDC 认证来源；
- 克隆源仓库并构造上下文；
- 调用 Codex service，必要时回退到 OpenAI / Anthropic API；
- 应用受控 patch、创建 PR、评论 issue、打标签、触发 guarded auto-merge。

### 1.2 已存在的主要构件

#### Workflow 层

- `codex_audit.yml`
  - 处理月度审计 / review_and_fix。
  - 支持 `provider=auto|api|anthropic|codex|openai`。
  - 使用 `CODEX_AUDIT_SERVICE_URL` 指向服务端。
  - 支持 guarded auto-merge。

- `codex_pr_review.yml`
  - 处理 PR review。
  - 支持 Codex service + 直接 API fallback。
  - 通过中央 Contract Oscillation Guard 保存受限的 blocking finding 历史并仲裁契约冲突。
  - 上传诊断 artifact。

- `codex_review_gate.yml`
  - 只执行确定性的 secret / path / metadata 静态门禁。
  - 使用受信任 base 代码检查 PR diff；API 读取失败时 fail closed。

- `codex_review_advisory.yml`
  - 事件驱动报告 current-head Codex GitHub App review。
  - 使用独立非 required check，不覆盖静态门禁结果，也不轮询。

- `monthly-orchestrator.yml`
  - 生成月度审计 issue。
  - 验证目标仓库必须是 snapshot repositories。
  - 强调月审 issue 由源仓库自己 dispatch AIAuditBridge。

- `vps_codex_service_ops.yml`
  - 管理 VPS 上的 Codex audit service。
  - 说明服务端和 GitHub 端已经拆开。

#### 服务层

- `service/ai_gateway_service.py`
  - 统一 HTTP 服务。
  - OIDC 验证、限流、输入校验、job 管理、health/quota 接口。

- `service/adapters/codex_adapter.py`
  - 只负责在 VPS 上跑 `codex exec`。

- `service/adapters/llm_adapter.py`
  - 负责 OpenAI / Anthropic API 调用。

- `service/autonomy.py`
  - 负责风险等级 + 置信度 -> 动作建议。

- `service/health.py`
  - 负责健康状态、延迟、错误率、降级判断。

- `service/quota.py`
  - 负责每 repo quota、成本估算、账单快照。

- `service/feedback.py`
  - 负责反馈记录、效果评估、shadow disagreement。

#### 桥接脚本层

- `scripts/run_monthly_codex_audit.py`
  - 月审主流程。
  - 包括 repo/task 校验、service patch contract、path guard、PR 创建、label 管理、auto-merge 请求、stale label cleanup。

- `scripts/run_codex_pr_review.py`
  - PR review 主流程。
  - service 失败时可按条件回退到 API review。

- `scripts/gate_codex_app_review.py`
  - 以静态 gate 的形式保护合并；不处理 AI review verdict。

- `scripts/report_codex_app_review.py`
  - 只报告 current-head connector review 的 advisory 状态。

### 1.3 已经具备的自动化能力

- 来源认证：OIDC / allowlist。
- 资源控制：rate limit / quota。
- 风险控制：path guard / policy / label gating。
- 结果可追踪：issue comment、PR body、artifact、step summary。
- 容错：Codex service 失败后可走 API fallback。
- 变更应用：支持 patch response -> 本地应用 -> PR。
- 合并保护：guarded auto-merge 不是直接绕过 GitHub protection。

---

## 2. 能自动处理 vs 必须人工审计的边界

### 2.1 可以自动处理的范围

适合自动执行的条件是：

1. 变更范围低风险；
2. 变更目标明确；
3. 有稳定 policy / gate；
4. 失败后能安全回滚或重试；
5. 不影响资金、策略核心逻辑、权限边界、版本治理。

当前可自动处理的典型场景：

- docs / tests / README 类变更；
- 月度报告生成脚本、低风险辅助脚本；
- 明确的 packaging / lint / warning 修复；
- 低风险月度审计修复 PR；
- Codex review / API review 的评论生成；
- guarded auto-merge 条件满足时的受控打标与交给源仓 CI 合并。

### 2.2 必须人工审计的范围

这些场景不应交给无人值守直接合并：

- 策略核心逻辑、交易逻辑、信号生成逻辑；
- 影响仓库基础架构或权限边界的修改；
- `.github/codex_auto_merge_policy.json`、workflow、label policy、gate policy 变更；
- 删除、重命名、复制文件，特别是跨目录调整；
- 涉及 secrets、credentials、keys、token 的变更；
- 任何 QSL 版本管理、internal dependency matrix、qslctl 相关治理文件；
- 低置信度模型输出但影响面大；
- 运行时 health 仅“看起来健康”但没有真实业务指标支撑的场景；
- 任何需要跨仓确认的变更，例如消费者仓库与治理仓库之间的契约变动。

### 2.3 灰区：可以自动建议，但不应自动完成

这些适合“自动做前半段，人工做最后确认”：

- 月度 audit 结果总结；
- 策略健康报告的初稿；
- 变化解释、风险分析、建议动作；
- 需要看历史效果再决定是否采纳的优化建议；
- 复杂 PR review 的评论，但不自动 merge。

---

## 3. 关键缺口和优先级

### P0：必须补的缺口

#### 3.1 策略级与组织级指标缺失

现在 health/quota 主要是服务层健康，不足以回答：

- 哪个策略长期退化？
- 哪类月审修复真正降低了人工介入？
- 哪些 repo 反复触发高风险变更？
- 自动修复是否真的提高了通过率？

缺少统一的、可持久化的 KPI / 回放数据。

#### 3.2 人工审计边界虽然存在，但缺少统一执行面

目前边界分散在：

- policy JSON；
- workflow；
- script 内的 path guard；
- review gate。

问题是这些规则是“分散一致”，还不是“单点治理”。一旦某处漏改，就会出现策略漂移。

#### 3.3 自动合并的最终决定仍依赖外部 source CI

这是对的，但也说明 AIAuditBridge 本身还不能闭环完成“无人值守”。

它能请求 auto-merge，不能替代：

- 源仓库 CI；
- branch protection；
- merge queue / required checks；
- 失败后的 retrigger 逻辑。

#### 3.3.1 Contract Oscillation Guard

Contract Oscillation Guard 是 `AIAuditBridge` 的中央 PR review gate 语义，不是要求每个消费者仓库新增一套 branch rule。消费者仍使用原有 required check、branch protection 和 merge queue；guard 不提供 label、admin 或人工确认绕过。

trusted review comment 只保存最近固定轮数、固定字节上限且脱敏后的 blocking finding 摘要，包括 head SHA、file、category、severity、description 和 suggestion。历史只能由已验证的 review bot comment 恢复；legacy comment 没有 history marker 时保持兼容，但既有 blocker 会被迁移为 `invalid_history` 并继续 fail closed，不能因一次 clean review 自动清除。畸形或超限 history 同样 fail closed。

若 `overflow` / `invalid_history` 状态中没有可供仲裁的 trusted prior finding，系统不得用空上下文自动 `clear`。此时需要人工确认 source-of-truth 后修复或删除损坏的 trusted bot state，再重新运行普通 required review check；这只恢复可审计状态，不直接放行 merge，也不绕过 branch protection。

当同一 file/category/severity 的前后 finding 可能要求相反行为时，独立仲裁必须同时读取上一轮 finding、当前 finding 和累计 PR diff，并优先以公共接口、schema、tests、docs 等 source-of-truth 判断：

- source-of-truth 足以证明当前 finding 为 false positive 时，仲裁可 `clear`；
- 当前 finding 有明确契约依据时保持 `block`；
- 证据不足、结果 ambiguous 或仲裁失败时继续 blocked。

一旦确认或无法排除 contract conflict，结构化结果固定为 `contract_conflict=true`、`auto_fix_allowed=false`、`next_action=contract_arbitration`，禁止自动 remediation 继续反向修改代码。系统只要求一次人工契约确认；确认应落到公共接口、schema、tests 或 docs 的明确变更后，再由普通 review/check 链路重新验证，而不是绕过 gate。

`verdict=clear` 表示 source-of-truth 已证明当前 finding 为 false positive，因此 required review check 可以通过；即使历史上检测到 `contract_conflict=true`，仍保持 `auto_fix_allowed=false`，防止执行线程继续改代码。这是对错误 finding 的独立仲裁结论，不是绕过 branch protection。`block`、`ambiguous` 或仲裁失败才必须继续 blocked。

已 `cleared` 的 finding key 是历史匹配边界，不得继续回溯并复活更旧的同 key blocker；未被 clear 的多个 current finding key 则必须从最近历史轮分别聚合后统一交给仲裁，不能只取第一个命中的 round。

### P1：强烈建议补的缺口

#### 3.4 缺少统一的任务状态机

月度审计、PR review、修复、重试、回退、人工升级，这些状态现在是靠脚本和 GitHub 流程串起来的。

建议显式建模：

- `queued`
- `running`
- `reviewed`
- `patch_applied`
- `pr_opened`
- `waiting_for_ci`
- `auto_merge_requested`
- `human_review_required`
- `human_review_pr_opened`
- `human_review_waiting_for_ci`
- `human_review_auto_merge_requested`
- `merged`
- `failed`
- `blocked`

#### 3.5 反馈回路还不够强

已有 `feedback.py`，但还没形成：

- 哪类问题最常复发；
- 哪个 provider / model 组合最稳定；
- 哪类变更最适合直接走 API fallback；
- 哪些 low-risk 规则需要升级或收紧。
- 有效性报告属于反馈回路指标，只能帮助判断历史改动是否改善，不应单独作为自动合并或策略放行依据。

#### 3.6 Dashboard / org-health 还缺“决策联动”

健康面板如果只是展示状态，不足以支持无人值守。

它至少还需要：

- 健康异常自动降级到 review_only；
- quota 低时自动降级模型；
- 失败模式自动切换 provider 或暂停自动修复；
- 对连续失败仓库自动升级人工审计。

### P2：可后置优化

#### 3.7 更细粒度的复杂度路由

现在有 low / medium / high 的复杂度路由，但还可以更精细地接入：

- repo 历史稳定性；
- 变更类型；
- 最近失败率；
- 真实审批时延。

这类优化有价值，但不是无人值守的先决条件。

#### 3.8 Strategy Optimization Watcher 的 issue-only 安全边界

对于 Strategy Optimization Watcher，首批实现只走 issue-only 提案流，不直接触碰策略执行面。

推荐流程是：

1. deterministic trigger 触发 watcher；
2. 生成 evidence bundle；
3. 只创建 optimization issue / task proposal；
4. 经过 authority / registry gate 校验后再决定是否进入下一步；
5. 后续如需执行，再由人工或 CI gate 接管。

当前首批实现的安全边界是：

- 只创建 optimization issue/task；
- 不自动改策略；
- 不调 live 参数；
- 不联网检索；
- 不自动 merge / deploy。

这样可以把策略优化先收敛为可审计、可回放的建议流，再逐步扩展到受控执行面。

---

## 4. 可落地的阶段性改造计划

### Phase 1：先把“可无人值守的部分”界定清楚

目标：让系统明确知道什么能自动做，什么必须升级人工。

建议动作：

- 把自动化边界写成统一 policy 文档，并和代码测试绑定；
- 把风险分类、label policy、workflow gate 的规则集中到一个共享配置入口；
- 明确低风险 auto-merge 的文件集合和禁区；
- 给每类任务加上标准状态输出和 step summary。

交付物：

- 统一的自治政策说明；
- 可读的风险分级规则；
- 每个任务的状态机输出。

### Phase 2：把反馈回路做实

目标：让系统不是“做完就走”，而是“做完能学”。

建议动作：

- 把每次月审 / PR review / auto-fix 的结果持久化；
- 记录：问题类型、provider、模型、风险级别、是否需要人工、是否 merge 成功、是否复发；
- 在 dashboard 上展示：
  - 自动处理成功率；
  - 人工升级率；
  - 重试成功率；
  - 最近 30 天回退次数；
  - 高风险变更占比。

交付物：

- 持久化反馈表；
- 可查询的 org-health 指标；
- 月报审计的历史对比。

### Phase 3：让健康检测影响决策

目标：把健康状态从“展示”变成“调度输入”。

建议动作：

- health degraded 时自动切换 review_only；
- quota 紧张时优先低成本模型或延后任务；
- 连续失败时强制人工审计；
- 针对不同 repo 设置不同自治等级。

交付物：

- 健康驱动的执行降级策略；
- repo 级别自治阈值。

### Phase 4：扩大自动修复，但只扩大低风险面

目标：提升无人值守覆盖率，但不放松安全门。

建议动作：

- 扩展 docs/tests/report helper 的自动修复能力；
- 对 packaging/lint 类修复增加更强的自动测试和回归测试；
- 对月审生成的 PR 增加标准化的 PR body / comment 模板；
- 维持高风险路径默认人工审计。

交付物：

- 更高的低风险自动修复成功率；
- 更稳定的 guarded auto-merge 命中率。

### Phase 5：再考虑组织级无人值守

只有在下面条件都满足后，才建议把“无人值守”从局部扩到组织级：

- 反馈数据稳定；
- 高风险边界清晰；
- 失败降级机制可自动生效；
- 关键仓库的 merge gate 行为稳定；
- 月审结果和人工审计结果长期一致。

---

## 5. 对“无人值守”目标的判断

### 可以放心推进的部分

- 月度审计 issue 的生成与调度；
- 低风险 review / 修复；
- 受控 auto-merge 请求；
- 服务健康与 quota 监控；
- 失败后 fallback 和 retry。

### 不能直接无人值守的部分

- 策略核心修改；
- 任何治理 / policy / workflow 变更；
- QSL 版本管理；
- 最终 merge 决策；
- 需要解释原因或承担业务风险的变更。

### 总体判断

当前架构更像是：

- **自动化执行层已经有了**；
- **自治决策层还不完整**；
- **组织级闭环还差一层数据和治理**。

所以现在的最佳目标不是“完全无人值守”，而是：

> **把 60%~80% 的低风险审计与修复自动化，把高风险部分稳定地拦在人工审计门前。**

---

## 6. 建议的下一步

1. 先把这份架构草案定稿到仓库文档里。
2. 再补一份“自治边界表”，把自动 / 人工 / 禁止 三类动作列清楚。
3. 然后补指标持久化和 dashboard 视图。
4. 最后再决定是否扩大 auto-merge 覆盖面。
