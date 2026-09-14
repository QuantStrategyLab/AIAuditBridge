# Russell 研究结果解释入口

`.github/workflows/russell_research_explanation.yml` 是一个仅允许 `main` 分支手动触发的研究辅助入口。它读取已成功完成的 `QuantStrategyLab/UsEquitySnapshotPipelines` run `34807588512` 的运行元数据和日志，只提取已验证的聚合结果字段，然后通过现有 `AiGatewayClient.execute` 使用 `research_summary`、`review_only`、`read-only`、Codex-only 路由生成中文 advisory。

归档使用现有 GitHub Actions artifact 机制，保存源 run/head、feature generation/hash、确定性结果和真实 Codex job/provider/model/ref，以及模型原文。结果明确标记为人工历史结果解释，不继承原研究任务的 AI 身份，也不表示自然漂移、晋级、上线或交易授权。工作流不下载价格、不重跑回测、不写 Issue/comment、不调用 broker。
