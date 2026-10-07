# 现有登录配置与助手接入

核对时间：2026-10-08。检查只读取源码、云端配置名称、部署记录和公开 HTTP 响应；未读取密钥值，未登录用户账户，未修改云端配置。

## 已有能力

`cloudflare/ai-gateway-dash` 已有 GitHub OAuth 登录入口，源码通过 GitHub 用户及组织接口限制 QuantStrategyLab 组织成员访问。登录后使用 HMAC 签名的看板 Cookie；配置中存在 `DASHBOARD_SESSION_KV` 绑定，源码支持注销时撤销会话。

线上 Worker `quantstrategylab-ai-gateway-dash` 的秘密配置名称包含 `GITHUB_OAUTH_CLIENT_ID`、`GITHUB_OAUTH_CLIENT_SECRET`、`DASHBOARD_SESSION_SECRET`、`AI_GATEWAY_ORIGIN_URL`、`DASHBOARD_API_TOKEN`。查到 10 条部署记录，最新时间为 2026-09-10T05:02:05.918113Z。

使用正常 TLS 校验、禁止跟随重定向的公开请求确认：

| 路径 | 结果 | 能说明什么 |
|---|---|---|
| `/` | 200，HTML | 看板入口可访问 |
| `/login` | 302，指向 github.com | GitHub 登录跳转已配置 |
| `/.well-known/oauth-authorization-server` | 404 | 当前路径没有授权服务器发现文档 |
| `/.well-known/oauth-protected-resource` | 404 | 当前路径没有 MCP 受保护资源发现文档 |

未完成真实登录，不能据此认定 OAuth 回调、GitHub 组织准入或新助手连接已经验收。云端部署存在也不能证明部署源码与本地基线完全相同。

GitHub 仓库配置名称显示 AIAuditBridge 仍使用 `CODEX_AUDIT_SERVICE_*` 和既有同步令牌；本次检查未发现仓库层面的 Auth0、Keycloak 或 Cloudflare Access 配置名称。AIGateway 仓库层面变量及秘密名称列表为空。这个范围不包含组织级秘密、其他项目或全部云端服务，不能推出账户内不存在其他身份服务。

## 对新架构的影响

可以复用现有 GitHub 身份体系和成员准入逻辑。看板 Cookie 和原服务静态令牌分别用于看板会话与旧服务读取，不能直接充当 Dot/Grok 的 OAuth 访问令牌。

原生助手接入还需要 OAuth 2.1 授权能力：授权服务器及资源发现、PKCE S256、明确的客户端注册机制、受众和范围受限的令牌、刷新与撤销，以及任务服务所需的签名/JWKS 验证。应先确定支持这些能力的授权组件，沿用 GitHub 作为上游登录来源，再配置稳定的助手连接身份与项目授权。当前通用服务只实现资源服务器的令牌验证，没有实现该授权服务器；示例配置中的身份提供方继续保留未启用占位符。

GitHub Actions 的 OIDC 是业务工作流提交任务的身份，已经与助手领取及回传任务的身份分开设计。QuantStrategyLab 组织成员资格只能作为登录准入条件，不能替代具体仓库、任务范围及 Dot/Grok 执行身份的授权。

本次结论不需要用户提供密码或密钥。新 OAuth 应用注册、授权服务部署、回调地址变更和生产配置切换尚未执行。

共享的登录配置清单、助手执行指令和连接验收顺序见 PersonalAIService 的 [Dot / Grok 连接准备](../../../AIGateway/docs/upgrade-v2/NATIVE-CONNECTION.zh-CN.md)。该链接按本地多仓库工作区解析，正式发布时需替换为批准发布仓库的文档地址。
