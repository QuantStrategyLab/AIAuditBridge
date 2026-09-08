# 日报身份接线与模型目录部署修复

本片从已合并的 `60bd64a2ae059a082614181eeb845b46df395523` 接续。以下是本地源码与离线验证结果；没有部署、真实摘要、通知或真实模型调用证据。前一版日报文档描述的是当时的缺口，本片补上实际 workflow 调用入口；策略实验和跨 run 恢复另行处理。

## 日报摘要

既有 `codex_audit.yml` 新增独立 `daily-summary` job，复用已有 `self-hosted / codex-vps` runner 与已获准 workflow 身份。每天 22:45 UTC 读取 VPS 22:30 UTC 定时器已经生成的当日日报；不会运行 builder、监测、优化或通知。只有仓库变量 `AI_DAILY_SUMMARY_ENABLED=true` 且运行 ref 为受保护的 main 才执行。也可显式 workflow_dispatch `daily_summary=true`，同样需要该变量；默认关闭。月度审计和 synthetic 检查与日报 job 互斥；月度入口仍要求实际 `ISSUE_NUMBER`。

运行 job 自己领取 GitHub OIDC。VPS timer 不获得或保管该 token，也不以静态 dashboard token 代替。日报代码使用该 workflow checkout 自带的 `client` SDK，无需依赖 VPS 旧安装包。请求固定 `research_summary / review_only` 与 Codex 渠道；不自动启用 Cursor 或付费 API fallback，模型、推理等级、额度仍由既有研究准入决定。SDK 保留排队身份与每次轮询的 job/provider/stage/model/effort 绑定。

实际入口为 `python3 -m scripts.consume_daily_briefing --report-dir <当日日报目录> --day YYYY-MM-DD --summary-only`。该模式拒绝 `--dispatch` 和 `--dual-review`，只输出 `day` 与 `ai_summary`。模型输入沿用已有领域/源文件、行状态计数及两类时钟白名单：报告生成不超过 36 小时、策略观测不超过 7 个自然日，缺失领域明确列出；不输出策略名称、指标明细或底层错误。输出不含原 findings 或本地路径。缺报告、非法输入、缺 OIDC、执行不可用或额度延期均输出明确状态并 exit 3；成功和摘要 dry-run 为 exit 0，dry-run 零认证/模型。

源报告在同一 VPS 文件系统被读取；GitHub artifact 仅保存 `daily-ai-summary.json`，保留 7 天，不上传原始日报。模型输出仍是 advisory，不是金融事实已验证或策略健康/晋级通过。原 `daily_briefing_pipeline.sh --dispatch` 的规则告警和退出语义未改。

源码采用后，仍需主任务在真实 runner 核验固定日报路径、读权限和来源时间，再按已授权费用范围启用变量并核验一次实际 OIDC job 与摘要 artifact。定时触发、健康检查和本地 mock 都不替代这一业务证据。当前服务只有同 run 的活动去重；在跨 run 研究恢复补丁完成前，不要用手动反复重跑处理已启动但结果未知的摘要。

## 模型目录部署

旧 `deploy_model_catalog_sync.yml` 与脚本先 checkout/pull 长期开发副本，受其中两处 monitor 文档改动阻挡。新 workflow 从自己的 exact `github.sha` checkout 执行部署脚本，`MODEL_CATALOG_REVISION` 必须为该 checkout 的完整 HEAD；不 fetch、checkout、reset 或 pull 长期副本。

脚本用 `git archive` 只取 `service/`、`scripts/sync_model_catalog.py`、默认目录种子和本服务 systemd 模板，安装到 `/opt/codex-model-catalog/releases/<SHA>`。本地未提交和未跟踪内容不参与安装；已有同 SHA 内容一致则复用，内容不同拒绝覆盖。安装在同文件系统临时目录完成后再移动，保留旧 release。systemd 中的代码路径被渲染为这个固定目录，禁止运行时写 bytecode，避免下一次幂等检查被生成文件干扰。

已有 `/etc/codex-audit-bridge/model-catalog.env` 原样保留，不读取/输出其值，不改其目录权限。仅首次缺失时才从既有批准 secrets 建立文件；没有新凭据来源。原目录 JSON、月首 06:00 UTC 的 API 目录 timer、独立每日 Cursor CLI roster timer 及 gateway 配置不改。API 目录发现不执行模型。本片未改 monitor 的部署/同步入口，不覆盖其用户文档。

部署脚本应从实际已发布 checkout 运行；手动使用时须显式传入对应 `MODEL_CATALOG_REVISION`，不能用移动 ref `main`。主任务部署后需核验 systemd 的固定 release 路径、既有凭据/用户文档保留、原 timer 频率及一次目录刷新结果。当前本地 fixture 未运行真实 systemctl、模型目录 HTTP、VPS 写入或 Git 发布。

## 离线验证

解释器 `/usr/local/bin/python3`。日报新 CLI 在旧代码上 3 个用例失败，workflow 缺入口 1 个失败，非法规则字段暴露原文另复现 1 个失败；模型部署旧逻辑 3 个失败。最小修复后定向组通过：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 /usr/local/bin/python3 -m pytest -q -p no:cacheprovider tests/test_daily_briefing_summary.py tests/test_consume_daily_briefing.py tests/test_deploy_model_catalog_sync_workflow.py
```

实际结果：52 passed。包含 CLI→当前 SDK→合成 OIDC/health/submit/poll 完整路径、原告警兼容、纯本地 Git clone/archive 部署 fixture、脏文档/凭据保留、重复部署和被修改 release 的拒绝。Ruff、actionlint、shellcheck、bash 语法与 diff whitespace 检查用于本片相应文件；没有将 mock 作为真实业务成功。
