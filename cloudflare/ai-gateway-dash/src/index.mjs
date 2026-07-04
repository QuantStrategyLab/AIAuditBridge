/** AiGateway Dashboard — Cloudflare Worker.

  GitHub OAuth 2.0 login — only QuantStrategyLab org members can access.
  Setup: create OAuth App at https://github.com/settings/developers
    - Homepage: https://quantstrategylab-ai-gateway-dash.pigbibi.workers.dev
    - Callback: https://quantstrategylab-ai-gateway-dash.pigbibi.workers.dev/callback
    - Set secrets: GITHUB_OAUTH_CLIENT_ID, GITHUB_OAUTH_CLIENT_SECRET
 */

// ── Config ─────────────────────────────────────────────────────────────

const GITHUB_OAUTH_AUTHORIZE = "https://github.com/login/oauth/authorize";
const GITHUB_OAUTH_ACCESS_TOKEN = "https://github.com/login/oauth/access_token";
const GITHUB_API_USER = "https://api.github.com/user";
const GITHUB_API_ORGS = "https://api.github.com/user/orgs";
const REQUIRED_ORG = "QuantStrategyLab";
const COOKIE_NAME = "dash_session";
const STATE_COOKIE_NAME = "dash_oauth_state";
const COOKIE_MAX_AGE = 86400; // 24h
const SESSION_SECRET_LENGTH = 32;
const DASHBOARD_API_ROUTES = new Set([
  "/v1/ai/health",
  "/v1/ai/quota",
  "/v1/ai/changes",
  "/v1/ai/changes/effectiveness",
  "/v1/ai/feedback/shadow",
]);

// ── HTML templates ─────────────────────────────────────────────────────

const LOGIN_HTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AiGateway · QuantStrategyLab</title>
<style>
  :root{
    --bg:#050914;--panel:#0d1726;--panel2:#0f1c2f;--border:#203550;
    --text:#edf4ff;--muted:#a1b2c8;--muted2:#64748b;
    --blue:#3b82f6;--cyan:#22d3ee;--violet:#8b5cf6;--danger:#f87171;
    --shadow:0 32px 90px rgba(0,0,0,.44),0 0 0 1px rgba(96,165,250,.05);
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{min-height:100vh;padding:28px;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text);background:radial-gradient(circle at 18% 80%,rgba(59,130,246,.18),transparent 28%),radial-gradient(circle at 74% 18%,rgba(139,92,246,.16),transparent 24%),linear-gradient(145deg,#050914,#07111d 52%,#04070f);-webkit-font-smoothing:antialiased}
  body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(148,163,184,.028) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,184,.028) 1px,transparent 1px);background-size:46px 46px;mask-image:linear-gradient(to bottom,rgba(0,0,0,.72),transparent)}
  .page{position:relative;min-height:calc(100vh - 56px);display:grid;grid-template-rows:auto 1fr auto;max-width:1120px;margin:0 auto;border:1px solid rgba(148,163,184,.10);border-radius:28px;background:linear-gradient(180deg,rgba(7,15,28,.72),rgba(5,9,20,.86));box-shadow:var(--shadow);overflow:hidden}
  .page:after{content:"";position:absolute;left:-120px;bottom:-120px;width:440px;height:260px;background:radial-gradient(ellipse at center,rgba(34,211,238,.18),transparent 68%);filter:blur(10px);pointer-events:none}
  header{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:24px 28px;border-bottom:1px solid rgba(148,163,184,.09)}
  .brand{display:flex;align-items:center;gap:12px;font-weight:780;font-size:20px;letter-spacing:-.04em}.brand span{color:#8aa0bd;font-size:13px;font-weight:500;letter-spacing:0}.mark{width:34px;height:34px;border-radius:12px;display:grid;place-items:center;color:#60a5fa;background:linear-gradient(135deg,rgba(59,130,246,.22),rgba(139,92,246,.18));border:1px solid rgba(96,165,250,.34)}.mark svg{width:20px;height:20px}.top-login{color:#cbd5e1;text-decoration:none;font-size:13px;border:1px solid rgba(148,163,184,.18);border-radius:12px;padding:9px 13px;background:rgba(15,28,47,.62)}
  main{display:grid;place-items:center;padding:64px 28px 50px;text-align:center}.hero{width:min(680px,100%)}
  h1{font-size:clamp(48px,7vw,76px);line-height:.95;letter-spacing:-.075em;color:#fff;margin-bottom:16px}.org{font-size:clamp(28px,4vw,42px);font-weight:760;letter-spacing:-.055em;background:linear-gradient(90deg,#38bdf8,#8b5cf6);-webkit-background-clip:text;background-clip:text;color:transparent;margin-bottom:16px}.tagline{font-size:18px;color:#d7e2f1;margin-bottom:26px}.desc{max-width:590px;margin:0 auto 38px;color:var(--muted);font-size:15px;line-height:1.9}
  .login-title{font-size:28px;font-weight:760;letter-spacing:-.04em;margin-bottom:10px}.login-note{color:var(--muted);font-size:14px;margin-bottom:24px}.btn{display:inline-flex;align-items:center;justify-content:center;gap:11px;min-width:282px;padding:14px 22px;border-radius:13px;background:linear-gradient(135deg,var(--blue),var(--violet));color:#fff;text-decoration:none;font-size:16px;font-weight:760;box-shadow:0 18px 44px rgba(59,130,246,.24);transition:transform .18s ease,box-shadow .18s ease}.btn:hover{transform:translateY(-1px);box-shadow:0 22px 52px rgba(59,130,246,.32)}.btn svg{width:21px;height:21px;fill:currentColor}.limited{margin-top:14px;color:var(--muted2);font-size:13px}.err{display:none;max-width:560px;margin:0 auto 22px;padding:12px 14px;border-radius:13px;background:rgba(248,113,113,.11);border:1px solid rgba(248,113,113,.35);color:var(--danger);font-size:13px;text-align:left}
  .capabilities{width:min(560px,100%);margin:42px auto 0;padding:22px;border:1px solid rgba(148,163,184,.16);border-radius:18px;background:linear-gradient(180deg,rgba(15,28,47,.70),rgba(8,16,30,.64));text-align:left}.capabilities h2{font-size:16px;margin-bottom:14px;letter-spacing:-.02em}.capabilities ul{display:grid;gap:11px;list-style:none;color:var(--muted);font-size:14px}.capabilities li{display:flex;gap:10px;align-items:flex-start}.capabilities li:before{content:"";width:16px;height:16px;flex:0 0 16px;margin-top:2px;border-radius:50%;border:1px solid rgba(147,197,253,.55);background:radial-gradient(circle at center,rgba(59,130,246,.65) 0 3px,transparent 4px)}
  footer{position:relative;z-index:1;display:flex;justify-content:center;gap:12px;padding:24px 28px;color:var(--muted2);font-size:12px;border-top:1px solid rgba(148,163,184,.08)}
  @media(max-width:720px){body{padding:14px}.page{min-height:calc(100vh - 28px);border-radius:22px}header{padding:18px;align-items:flex-start}.brand{align-items:flex-start;flex-direction:column;gap:6px}.top-login{display:none}main{padding:46px 20px 36px}.btn{width:100%;min-width:0}.capabilities{margin-top:32px;padding:18px}footer{flex-direction:column;align-items:center}}
</style>
</head>
<body>
<div class="page">
  <header>
    <div class="brand"><div class="mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M12 2 21 7v10l-9 5-9-5V7l9-5Z" stroke="currentColor" stroke-width="1.8"/><path d="m8 15 3-8h5l-3 5h4l-7 6 2-5H8Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg></div>AiGateway <span>QuantStrategyLab</span></div>
    <a class="top-login" href="/login">组织成员登录</a>
  </header>
  <main>
    <section class="hero">
      <h1>AiGateway</h1>
      <div class="org">QuantStrategyLab</div>
      <div class="tagline">AI audit gateway operations</div>
      <p class="desc">AiGateway 是 QuantStrategyLab 内部的 AI 审计与网关边界，统一承载 API 使用、配额控制、变更效果评估与影子审计，让 AI 能力在组织边界内安全、可观测、可追溯地运行。</p>
      <div class="err" id="err"></div>
      <div class="login-title">组织成员登录</div>
      <div class="login-note">使用 GitHub 账号登录以查看内部运维数据。</div>
      <a href="/login" class="btn">
        <svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>
        使用 GitHub 登录
      </a>
      <div class="limited">仅限 QuantStrategyLab 组织成员访问</div>
      <div class="capabilities">
        <h2>登录后，您可以查看</h2>
        <ul>
          <li>服务整体健康状态与关键接口指标摘要</li>
          <li>各仓库 / 团队的用量与额度概览</li>
          <li>变更有效性评估（90 天）</li>
          <li>影子审计分歧统计与裁决结果</li>
          <li>最近变更记录（7 天）</li>
        </ul>
      </div>
    </section>
  </main>
  <footer><span>AiGateway</span><span>·</span><span>QuantStrategyLab</span><span>内部使用 · 安全审计边界</span></footer>
</div>
<script>
const p = new URLSearchParams(location.search);
if(p.get("error")){document.getElementById("err").style.display="block";document.getElementById("err").textContent=decodeURIComponent(p.get("error"))}
</script>
</body></html>`;

const DASHBOARD_HTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AiGateway · QuantStrategyLab</title>
<style>
  :root{
    --bg:#050914;--panel:#0d1726;--panel2:#101d31;--border:#213653;
    --text:#e8f0fb;--muted:#9bacc2;--muted2:#65758d;--muted3:#43536a;
    --green:#22c55e;--green-bg:rgba(34,197,94,.12);
    --amber:#f59e0b;--amber-bg:rgba(245,158,11,.12);
    --red:#f87171;--red-bg:rgba(248,113,113,.12);
    --blue:#3b82f6;--blue-bg:rgba(59,130,246,.13);
    --violet:#8b5cf6;--violet-bg:rgba(139,92,246,.14);
    --radius:18px;--shadow:0 20px 70px rgba(0,0,0,.30),0 0 0 1px rgba(96,165,250,.035);
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{min-height:100vh;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text);background:radial-gradient(circle at 18% 0%,rgba(59,130,246,.18),transparent 30%),radial-gradient(circle at 86% 12%,rgba(139,92,246,.13),transparent 24%),linear-gradient(145deg,#050914,#07111d 52%,#04070f);-webkit-font-smoothing:antialiased}
  body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(148,163,184,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,184,.025) 1px,transparent 1px);background-size:46px 46px;mask-image:linear-gradient(to bottom,rgba(0,0,0,.76),transparent)}
  .shell{position:relative;min-height:100vh;display:grid;grid-template-rows:auto 1fr auto}.topbar{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:18px 28px;border-bottom:1px solid rgba(148,163,184,.12);background:rgba(5,9,20,.58);backdrop-filter:blur(16px)}
  .brand{display:flex;align-items:center;gap:14px}.brand h1{font-size:22px;letter-spacing:-.05em;color:#fff}.brand span{color:#8ea0b8;font-size:13px}.mark{width:34px;height:34px;border-radius:12px;display:grid;place-items:center;color:#60a5fa;background:linear-gradient(135deg,rgba(59,130,246,.22),rgba(139,92,246,.18));border:1px solid rgba(96,165,250,.32)}.mark svg{width:20px;height:20px}
  .actions{display:flex;align-items:center;gap:14px;color:var(--muted);font-size:13px}.status-pill{display:inline-flex;align-items:center;gap:8px;padding:8px 13px;border-radius:999px;font-weight:760;border:1px solid transparent}.status-pill.ok{background:var(--green-bg);color:#b7f7ca;border-color:rgba(34,197,94,.28)}.status-pill.warn{background:var(--amber-bg);color:#fde68a;border-color:rgba(245,158,11,.28)}.status-pill.err{background:var(--red-bg);color:#fecaca;border-color:rgba(248,113,113,.28)}.pulse{width:8px;height:8px;border-radius:50%;display:inline-block}.pulse.ok{background:var(--green);box-shadow:0 0 14px var(--green)}.pulse.warn{background:var(--amber);box-shadow:0 0 14px var(--amber)}.pulse.err{background:var(--red);box-shadow:0 0 14px var(--red)}.user{display:flex;align-items:center;gap:8px;padding-left:14px;border-left:1px solid rgba(148,163,184,.13)}.user img{display:none;width:28px;height:28px;border-radius:50%;background:var(--panel2);border:1px solid rgba(148,163,184,.22)}.logout{display:inline-flex;align-items:center;padding:8px 11px;border:1px solid rgba(148,163,184,.17);border-radius:11px;color:#d7e2f1;text-decoration:none}.logout:hover{border-color:rgba(147,197,253,.38);color:#fff}
  main{width:min(1260px,100%);margin:0 auto;padding:22px 28px 24px}.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:14px}.card{position:relative;min-height:220px;background:linear-gradient(180deg,rgba(16,29,49,.88),rgba(9,18,32,.92));border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}.card:before{content:"";position:absolute;inset:0 0 auto;height:1px;background:linear-gradient(90deg,transparent,rgba(96,165,250,.45),transparent)}.card-body{position:relative;display:flex;flex-direction:column;min-height:inherit;height:100%;padding:20px}.card-body>.card-head+*{flex:1;min-height:0}.span-6{grid-column:span 6}.span-4{grid-column:span 4}.span-8{grid-column:span 8}.span-12{grid-column:span 12}.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:16px}.card h2{font-size:18px;color:#fff;letter-spacing:-.035em}.updated{font-size:12px;color:var(--muted2);white-space:nowrap}.loading{display:grid;place-items:center;height:100%;min-height:132px;color:var(--muted)}.spinner{width:40px;height:40px;border-radius:50%;border:4px solid rgba(59,130,246,.18);border-top-color:#60a5fa;animation:spin 1s linear infinite;margin:0 auto 14px}@keyframes spin{to{transform:rotate(360deg)}}.loading strong{display:block;text-align:center;margin-bottom:8px;color:#fff}.loading span{display:block;text-align:center;font-size:13px;color:var(--muted)}
  .stat-row{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;padding:10px 0;border-bottom:1px solid rgba(148,163,184,.08);font-size:13px}.stat-row:last-child{border-bottom:none}.stat-label{min-width:0;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.stat-value{max-width:68%;color:#e8f0fb;font-weight:740;font-variant-numeric:tabular-nums;text-align:right;line-height:1.45;overflow-wrap:anywhere}.stat-value.ok{color:#86efac}.stat-value.warn{color:#fde68a}.stat-value.err{color:#fecaca}.stat-value.info{color:#93c5fd}.big{font-size:42px;font-weight:820;letter-spacing:-.06em;line-height:1;color:#fff;font-variant-numeric:tabular-nums}.big.blue{color:#60a5fa}.big.violet{color:#a78bfa}.subtle{color:var(--muted2);font-size:12px;margin-top:12px;line-height:1.6}.metric-line{display:flex;align-items:flex-end;gap:12px;margin-bottom:18px}.delta{display:inline-flex;align-items:center;padding:5px 9px;border-radius:999px;font-size:12px;font-weight:760}.delta.ok{background:var(--green-bg);color:#86efac}.delta.warn{background:var(--amber-bg);color:#fde68a}.delta.err{background:var(--red-bg);color:#fecaca}.delta.info{background:var(--blue-bg);color:#bfdbfe}
  .quota-section{display:grid;gap:8px;padding-top:14px;margin-top:14px;border-top:1px solid rgba(148,163,184,.08)}.quota-section:first-child{padding-top:0;margin-top:0;border-top:none}.section-title{color:#c8d7ea;font-size:12px;font-weight:780;letter-spacing:.08em;text-transform:uppercase}.quota-list{display:grid;gap:13px}.quota-item{display:grid;grid-template-columns:minmax(160px,1fr) minmax(160px,260px) 82px;gap:16px;align-items:center}.quota-name{font-size:13px;font-weight:760;color:#e4edf9}.quota-meta{margin-top:3px;color:var(--muted2);font-size:12px;font-variant-numeric:tabular-nums}.bar{height:8px;border-radius:999px;background:rgba(148,163,184,.13);overflow:hidden}.bar-fill{height:100%;border-radius:999px;transition:width .7s cubic-bezier(.4,0,.2,1)}.bar-fill.ok{background:linear-gradient(90deg,#22c55e,#2dd4bf)}.bar-fill.warn{background:linear-gradient(90deg,#f59e0b,#f97316)}.bar-fill.err{background:linear-gradient(90deg,#f87171,#fb7185)}.bar-label{text-align:right;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}.badge{display:inline-flex;align-items:center;justify-content:center;padding:4px 9px;border-radius:8px;font-size:11px;font-weight:760;white-space:nowrap}.badge-ok{background:var(--green-bg);color:#86efac}.badge-warn{background:var(--amber-bg);color:#fde68a}.badge-err{background:var(--red-bg);color:#fecaca}.badge-info{background:var(--blue-bg);color:#bfdbfe}.badge-violet{background:var(--violet-bg);color:#ddd6fe}
  table{width:100%;border-collapse:collapse;font-size:12px}thead th{padding:0 10px 11px 0;text-align:left;color:var(--muted2);font-size:11px;font-weight:760;text-transform:uppercase;letter-spacing:.08em}tbody td{padding:11px 10px 11px 0;border-top:1px solid rgba(148,163,184,.08);vertical-align:middle;color:#d8e3f1}.mono{font-family:"SFMono-Regular","JetBrains Mono",ui-monospace,monospace;font-size:11px}.table-wrap{overflow:auto}.empty{display:grid;place-items:center;height:100%;min-height:132px;text-align:center;color:var(--muted2);font-size:13px}.empty strong{display:block;color:#d7e2f1;font-size:15px;margin-bottom:7px}.panel-error{display:grid;place-items:center;height:100%;min-height:132px;padding:16px;border:1px solid rgba(248,113,113,.22);border-radius:14px;background:rgba(248,113,113,.07);text-align:center;color:#fca5a5;font-size:13px}.panel-error strong{display:block;color:#fee2e2;font-size:15px;margin-bottom:7px}.panel-error span{overflow-wrap:anywhere}.toast{position:fixed;top:18px;right:18px;max-width:420px;background:rgba(127,29,29,.88);border:1px solid rgba(248,113,113,.42);color:#fecaca;padding:12px 15px;border-radius:14px;font-size:13px;display:none;z-index:100;box-shadow:0 18px 50px rgba(0,0,0,.35)}footer{display:flex;justify-content:space-between;gap:16px;padding:18px 28px;border-top:1px solid rgba(148,163,184,.10);color:var(--muted3);font-size:12px}
  @media(max-width:980px){.span-4,.span-6,.span-8{grid-column:span 12}.quota-item{grid-template-columns:1fr}.bar-label{text-align:left}.topbar,.actions{align-items:flex-start;flex-direction:column}.user{padding-left:0;border-left:none}}
  @media(max-width:640px){main{padding:16px}.topbar{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;gap:14px 18px;padding:16px}.brand{grid-column:1;grid-row:1;align-items:flex-start;flex-direction:column;gap:8px}.actions{display:contents}.actions>.status-pill{grid-column:2;grid-row:1;justify-self:end}.actions>span:nth-child(2){grid-column:1;grid-row:2;align-self:center;color:var(--muted2)}.actions>.user{grid-column:2;grid-row:2;justify-self:end;padding-left:0;border-left:none}.card-body{padding:17px}.stat-row{display:grid;grid-template-columns:1fr;gap:5px}.stat-label{white-space:normal}.stat-value{max-width:100%;text-align:left}.big{font-size:34px}footer{flex-direction:column}.card{min-height:190px}}
</style>
</head>
<body>
<div class="toast" id="toast"></div>
<div class="shell">
  <header class="topbar">
    <div class="brand"><div class="mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M12 2 21 7v10l-9 5-9-5V7l9-5Z" stroke="currentColor" stroke-width="1.8"/><path d="m8 15 3-8h5l-3 5h4l-7 6 2-5H8Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg></div><div><h1>AiGateway</h1><span>QuantStrategyLab</span></div></div>
    <div class="actions"><span class="status-pill ok" id="status-pill"><span class="pulse ok" id="pulse"></span><span id="status-text">连接中…</span></span><span>刷新: <span id="last-refresh">—</span></span><div class="user"><img id="avatar" src="" alt=""><span id="username"></span><a href="/logout" class="logout">退出</a></div></div>
  </header>
  <main>
    <section class="grid">
      <article class="card span-6"><div class="card-body"><div class="card-head"><h2>服务健康</h2><span class="updated" id="health-updated">更新: —</span></div><div id="health"></div></div></article>
      <article class="card span-6"><div class="card-body"><div class="card-head"><h2>用量与额度</h2><span class="updated" id="quota-updated">更新: —</span></div><div id="quota"></div></div></article>
      <article class="card span-4"><div class="card-body"><div class="card-head"><h2>有效性 · 90 天</h2><span class="updated" id="effectiveness-updated">更新: —</span></div><div id="effectiveness"></div></div></article>
      <article class="card span-4"><div class="card-body"><div class="card-head"><h2>影子审计分歧</h2><span class="updated" id="shadow-updated">更新: —</span></div><div id="shadow"></div></div></article>
      <article class="card span-4"><div class="card-body"><div class="card-head"><h2>访问范围</h2><span class="updated">内部</span></div><div class="empty"><div><strong>组织成员可见</strong><span>所有数据仅限 QuantStrategyLab 组织成员访问，严禁外泄。</span></div></div></div></article>
      <article class="card span-12"><div class="card-body"><div class="card-head"><h2>最近变更 · 7 天</h2><span class="updated" id="changes-updated">更新: —</span></div><div class="table-wrap" id="changes"></div></div></article>
    </section>
  </main>
  <footer><span>自动刷新 · 30s</span><span>AiGateway · QuantStrategyLab</span></footer>
</div>
<script>
const API="/api";let refreshTimer;
function fmtUSD(n){return "$"+(Number(n)||0).toFixed(2)}
function fmtPct(n){return ((Number(n)||0)*100).toFixed(0)+"%"}
function fmtNum(n){return (Number(n)||0).toLocaleString()}
function clsStatus(st){return st==="healthy"?"ok":st==="degraded"?"warn":"err"}
function statusText(st){return st==="healthy"?"健康":st==="degraded"?"部分异常":st==="unhealthy"?"异常":st||"unknown"}
function clear(el){el.replaceChildren()}
function el(tag,className,text){const n=document.createElement(tag);if(className)n.className=className;if(text!==undefined)n.textContent=String(text);return n}
function append(parent){for(let i=1;i<arguments.length;i++)parent.appendChild(arguments[i]);return parent}
function showToast(msg){const e=document.getElementById("toast");e.textContent=msg;e.style.display="block";setTimeout(()=>e.style.display="none",5000)}
function setStatus(st){const c=clsStatus(st),p=document.getElementById("status-pill"),pl=document.getElementById("pulse"),t=document.getElementById("status-text");p.className="status-pill "+c;pl.className="pulse "+c;t.textContent=statusText(st)}
function setUpdated(id){document.getElementById(id).textContent="更新: "+new Date().toLocaleTimeString()}
function empty(target,title,text){clear(target);target.appendChild(append(el("div","empty"),append(el("div",""),el("strong","",title),el("span","",text))))}
function publicErrorMessage(error){return String(error&&error.message?error.message:error||"加载失败").replace(/\s+/g," ").slice(0,180)}
function panelError(targetId,title,error){const target=document.getElementById(targetId);clear(target);target.appendChild(append(el("div","panel-error"),append(el("div",""),el("strong","",title),el("span","",publicErrorMessage(error)))))}
function statRow(label,value,valueClass){const row=el("div","stat-row");append(row,el("span","stat-label",label),el("span","stat-value "+(valueClass||""),value));return row}
function badge(kind,text){return el("span","badge badge-"+kind,text)}
function loading(title,text){return '<div class="loading"><div><div class="spinner"></div><strong>'+title+'</strong><span>'+text+'</span></div></div>'}
function progressClass(p){return p>80?"err":p>50?"warn":"ok"}
async function fetchJSON(path){const r=await fetch(API+path);let data=null;try{data=await r.json()}catch(e){}if(!r.ok||data&&data.status==="error"){const detail=data&&data.error?": "+data.error:"";throw new Error(path+": "+r.status+detail)}if(!data)throw new Error(path+": invalid JSON");return data}
async function refresh(){const requests=[["/v1/ai/health","health","服务健康加载失败",renderHealth],["/v1/ai/quota","quota","配额消耗加载失败",d=>renderQuota(d.quota||d)],["/v1/ai/changes/effectiveness?days=90","effectiveness","有效性报告加载失败",d=>renderEffectiveness(d.report||d)],["/v1/ai/feedback/shadow","shadow","影子审计分歧加载失败",d=>renderShadow(d.disagreements||[])],["/v1/ai/changes?days=7","changes","最近变更加载失败",d=>renderChanges((d.changes||[]).slice(0,15))]],results=await Promise.allSettled(requests.map(r=>fetchJSON(r[0]))),failures=[];results.forEach((result,i)=>{const item=requests[i];try{if(result.status==="fulfilled")item[3](result.value);else throw result.reason}catch(e){failures.push(publicErrorMessage(e));panelError(item[1],item[2],e)}});document.getElementById("last-refresh").textContent=new Date().toLocaleTimeString();if(failures.length){setStatus("error");showToast("API: "+failures.slice(0,2).join("；"))}}
function healthReasonText(r){const path=r.path||"endpoint";if(r.reason==="error_rate")return path+" 错误率 "+fmtPct(r.value||0)+" ≥ "+fmtPct(r.threshold||0);if(r.reason==="p95_latency_ms")return path+" P95 "+fmtNum(r.value||0)+" ms ≥ "+fmtNum(r.threshold||0)+" ms";return path+" "+(r.reason||"degraded")}
function renderHealth(h){setStatus(h.status||"unknown");setUpdated("health-updated");const target=document.getElementById("health"),endpoints=h.endpoints||[],reasons=h.degradation_reasons||[],statusEndpoints=endpoints.filter(i=>i.latency_affects_status!==false),backgroundEndpoints=endpoints.filter(i=>i.latency_affects_status===false),total=endpoints.reduce((s,i)=>s+(Number(i.total)||0),0),errors=endpoints.reduce((s,i)=>s+(Number(i.errors)||0),0),p95=Math.max(0,...statusEndpoints.map(i=>Number(i.p95_ms)||0)),backgroundP95=Math.max(0,...backgroundEndpoints.map(i=>Number(i.p95_ms)||0)),details=el("div","");append(details,statRow("接口数量",endpoints.length,"info"),statRow("总请求",fmtNum(total),""),statRow("错误",fmtNum(errors),errors?"err":"ok"),statRow("在线接口 P95",fmtNum(p95)+" ms",p95>30000?"err":p95>10000?"warn":"ok"));if(backgroundP95)append(details,statRow("后台任务 P95",fmtNum(backgroundP95)+" ms","info"));if(reasons.length){reasons.slice(0,3).forEach((reason,i)=>append(details,statRow(i?"降级原因 "+(i+1):"降级原因",healthReasonText(reason),clsStatus(reason.severity||h.status))));if(reasons.length>3)append(details,statRow("更多原因","+"+fmtNum(reasons.length-3),"warn"))}else if(h.last_error&&h.last_error.message){append(details,statRow("最近错误",String(h.last_error.message).slice(0,80),"warn"))}clear(target);append(target,append(el("div","metric-line"),el("div","big ",statusText(h.status||"unknown")),el("span","delta "+clsStatus(h.status||"unknown"),fmtNum(Math.round((h.uptime_seconds||0)/3600))+" h")),details)}
function quotaTotalRow(label,d){const row=el("div","stat-row"),used=Number(d&&d.total_cost_usd)||0;append(row,el("span","stat-label",label),el("span","stat-value info",fmtUSD(used)));return row}
function codexAccountRow(d){const row=el("div","stat-row"),r=d&&d.rate_limits||{},p=r.primary||{},s=r.secondary||{},credits=r.credits||{},parts=[];if(p.used_percent!==null&&p.used_percent!==undefined)parts.push((p.window_duration_mins?Math.round(p.window_duration_mins/60)+"h":"主窗口")+" "+p.used_percent+"%");if(s.used_percent!==null&&s.used_percent!==undefined)parts.push((s.window_duration_mins?Math.round(s.window_duration_mins/1440)+"d":"次窗口")+" "+s.used_percent+"%");if(r.plan_type)parts.push(String(r.plan_type));if(credits.balance!==null&&credits.balance!==undefined)parts.push("credits "+credits.balance);append(row,el("span","stat-label","Codex 账户"),el("span","stat-value "+(Number(p.used_percent)>80?"err":Number(p.used_percent)>50?"warn":"ok"),parts.join(" · ")||"实时可用"));return row}
function providerUsageBreakdown(u){const input=Number(u&&u.input_tokens)||0,output=Number(u&&u.output_tokens)||0,uncached=Number(u&&u.uncached_input_tokens)||0,cached=Number(u&&u.input_cached_tokens)||0,parts=[];if(input||output||uncached)parts.push("in "+fmtNum(input||uncached)+" / out "+fmtNum(output));if(cached)parts.push("cached "+fmtNum(cached));return parts.join(" · ")}
function providerAccountRow(label,d,usageKey){const row=el("div","stat-row"),c=d&&d.costs||{},u=d&&d[usageKey]||{},days=Number(d&&d.window_days)||7,currency=String(c.currency||"usd").toUpperCase(),cost=Number(c.total_cost)||0,textTokens=(Number(u.input_tokens)||0)+(Number(u.output_tokens)||0),audioTokens=(Number(u.input_audio_tokens)||0)+(Number(u.output_audio_tokens)||0),tokens=Number(u.total_tokens)||(textTokens+audioTokens),cached=Number(u.input_cached_tokens)||0,requests=Number(u.num_model_requests)||0,breakdown=providerUsageBreakdown(u),parts=[days+"d"];if(c.total_cost!==null&&c.total_cost!==undefined)parts.push(currency==="USD"?fmtUSD(cost):cost.toFixed(2)+" "+currency);if(requests)parts.push(fmtNum(requests)+" req");if(tokens)parts.push(fmtNum(tokens)+" tokens");if(breakdown)parts.push(breakdown);if(audioTokens)parts.push(fmtNum(audioTokens)+" audio");if(cached&&!breakdown)parts.push(fmtNum(cached)+" cached");if(d&&d.usage_surface)parts.push(String(d.usage_surface));if(d&&d.filtered_project_count)parts.push(fmtNum(d.filtered_project_count)+" project");if(d&&d.filtered_api_key_count)parts.push(fmtNum(d.filtered_api_key_count)+" key");if(d&&d.filtered_workspace_count)parts.push(fmtNum(d.filtered_workspace_count)+" workspace");append(row,el("span","stat-label",label),el("span","stat-value info",parts.join(" · ")||"实时可用"));return row}
function providerCostRow(label,d,days){if(!d||d.total_cost===null||d.total_cost===undefined)return statRow(label,(Number(days)||7)+"d 成本暂不可用 · Usage API 已连接","warn");const row=el("div","stat-row"),currency=String(d.currency||"usd").toUpperCase(),cost=Number(d.total_cost)||0,parts=[(Number(days)||7)+"d "+(currency==="USD"?fmtUSD(cost):cost.toFixed(2)+" "+currency)];append(row,el("span","stat-label",label),el("span","stat-value info",parts.join(" · ")));return row}
function quotaHasUsage(d){return Boolean(Number(d&&d.total_cost_usd)||Number(d&&d.calls)||Number(d&&d.tokens_input)||Number(d&&d.tokens_output)||(d&&d.calls_incomplete))}
function quotaUsageRow(label,d,extra){const row=el("div","stat-row"),calls=Number(d&&d.calls)||0,used=Number(d&&d.total_cost_usd)||0,tokens=(Number(d&&d.tokens_input)||0)+(Number(d&&d.tokens_output)||0),callText=d&&d.calls_incomplete?(calls?"≥ "+fmtNum(calls):"未知次数"):fmtNum(calls),detail=fmtUSD(used)+" · "+callText+" 次"+(tokens?" · "+fmtNum(tokens)+" tokens":"")+(extra?" · "+extra:"");append(row,el("span","stat-label",label),el("span","stat-value info",detail));return row}
function appendRepoQuotaRows(list,repos){for(const name of Object.keys(repos)){const d=repos[name]||{},used=Number(d.total_cost_usd)||0,budget=Number(d.daily_budget)||0,p=budget?used/budget*100:0,c=progressClass(p),item=el("div","quota-item"),meta=el("div",""),bar=el("div","bar"),fill=el("div","bar-fill "+c);fill.style.width=Math.max(0,Math.min(p,100))+"%";append(meta,el("div","quota-name",String(name).split("/").pop()||name),el("div","quota-meta",fmtUSD(used)+" / "+fmtUSD(budget)+" repo 日预算"));append(bar,fill);append(item,meta,bar,el("div","bar-label",Math.round(p)+"%"));list.appendChild(item)}}
function quotaSection(title,node){return append(el("section","quota-section"),el("div","section-title",title),node)}
function renderQuota(q){setUpdated("quota-updated");const target=document.getElementById("quota"),repos=q.repos||{},repoCount=Object.keys(repos).length,summary=q.summary||null,account=summary&&summary.codex_account&&summary.codex_account.status==="available"?summary.codex_account:null,openai=summary&&summary.openai_account&&summary.openai_account.status==="available"?summary.openai_account:null,anthropic=summary&&summary.anthropic_account&&summary.anthropic_account.status==="available"?summary.anthropic_account:null,hasSummaryUsage=summary&&(account||openai||anthropic||quotaHasUsage(summary.combined)||quotaHasUsage(summary.api_key)||quotaHasUsage(summary.codex)||quotaHasUsage(summary.legacy_unknown));clear(target);if(summary&&(repoCount||hasSummaryUsage)){if(openai||anthropic||account){const provider=el("div","");if(openai)append(provider,providerAccountRow("GPT API 账户",openai,"completions"),providerCostRow("GPT 组织成本",openai.organization_costs,openai.window_days));if(anthropic)append(provider,providerAccountRow("Claude API 账户",anthropic,"messages"),providerCostRow("Claude 成本",anthropic.costs,anthropic.window_days));if(account)append(provider,codexAccountRow(account));target.appendChild(quotaSection("实时账户用量",provider))}const internal=el("div","");append(internal,quotaTotalRow("内部合计估算",summary.combined||{}),quotaUsageRow("本服务 API Key 估算",summary.api_key||{}),quotaUsageRow("本服务 Codex 估算",summary.codex||{}));if(quotaHasUsage(summary.legacy_unknown))append(internal,quotaUsageRow("历史未拆分",summary.legacy_unknown||{},"未归属"));target.appendChild(quotaSection("内部成本估算",internal));if(repoCount){const list=el("div","quota-list");appendRepoQuotaRows(list,repos);target.appendChild(quotaSection("仓库日预算",list))}target.appendChild(el("div","subtle","实时账户用量来自 GPT/Claude Admin Usage 与本机 Codex rate-limit 快照；成本暂不可用表示对应 Cost API 未返回金额，不代表没有用量。内部成本估算仅统计本服务记录的 API Key/Codex 调用，不等同于云厂商账单。"));return}if(!repoCount){empty(target,"暂无用量","当前窗口内没有记录到配额消耗。API Key 与 Codex 均无用量记录。");return}const list=el("div","quota-list");appendRepoQuotaRows(list,repos);target.appendChild(list)}
function renderEffectiveness(e){setUpdated("effectiveness-updated");const target=document.getElementById("effectiveness"),evaluated=Number(e.evaluated)||0,pending=Number(e.pending)||0,total=Number(e.total_changes)||evaluated+pending,rate=Number(e.improvement_rate)||0;clear(target);if(!total){empty(target,"暂无有效性样本","最近 90 天没有登记可评估的变更；有变更完成并回填评估后，这里会显示改善率。");return}if(!evaluated){append(target,append(el("div","metric-line"),el("div","big blue","待评估"),el("span","delta info",fmtNum(total)+" 条")),append(el("div",""),statRow("已登记",total,"info"),statRow("待评估",pending,"warn"),statRow("改善率","评估后生成","info")));return}append(target,append(el("div","metric-line"),el("div","big blue",fmtPct(rate)),el("span","delta "+(rate>0.7?"ok":rate>0.4?"warn":"info"),"改善率")),append(el("div",""),statRow("已评估",evaluated,"info"),statRow("改善",e.improved||0,"ok"),statRow("退化",e.degraded||0,"err"),statRow("持平",e.neutral||0,""),statRow("待评估",pending,"info")))}
function renderShadow(items){setUpdated("shadow-updated");const target=document.getElementById("shadow"),total=items.reduce((sum,item)=>sum+(Number(item.disagreement_count)||0),0);clear(target);if(!items.length){empty(target,"无活跃分歧","AI 影子审计与确定性路由当前没有待复核分歧；有异常累积时会在这里提示。");return}append(target,append(el("div","metric-line"),el("div","big violet",fmtNum(total)),el("span","delta warn","待复核")));for(const d of items.slice(0,5)){const row=el("div","stat-row"),value=el("span","stat-value warn");append(value,badge("warn",(d.disagreement_count||0)+"x"),document.createTextNode(" "+(d.ai_verdict||"")));append(row,el("span","stat-label",d.plugin||d.repo||"shadow"),value);target.appendChild(row)}}
function effectText(effect){return effect==="improved"?"改善":effect==="degraded"?"退化":effect==="neutral"?"持平":effect==="pending"?"待评估":effect||"待评估"}
function actionText(action){return action==="auto_merge"?"自动合并":action==="auto_pr"?"自动 PR":action==="manual"?"人工处理":action||"变更"}
function renderChanges(items){setUpdated("changes-updated");const target=document.getElementById("changes");clear(target);if(!items.length){empty(target,"暂无变更记录","最近 7 天没有登记的 AI 变更；后续通过反馈接口登记后会在这里形成报告。");return}const table=el("table",""),thead=el("thead",""),tr=el("tr","");for(const h of ["仓库","操作","效果","置信度","时间"])tr.appendChild(el("th","",h));thead.appendChild(tr);const tbody=el("tbody","");for(const c of items){const effect=c.effect||"pending",effectClass=effect==="improved"?"ok":effect==="degraded"?"err":"info",actionClass=c.action==="auto_merge"?"ok":c.action==="auto_pr"?"info":c.action?"violet":"warn",dt=c.created_at?new Date(c.created_at*1000).toLocaleDateString():"—",row=el("tr",""),repoName=String(c.repo||"").split("/").pop()||"—",repoCell=el("td","mono");if(c.external_url){const a=el("a","",repoName);a.href=c.external_url;a.target="_blank";a.rel="noreferrer";a.style.color="#93c5fd";a.style.textDecoration="none";repoCell.appendChild(a)}else repoCell.textContent=repoName;append(row,repoCell,append(el("td",""),badge(actionClass,actionText(c.action))),el("td","stat-value "+effectClass,effectText(effect)),el("td","mono",fmtPct(c.confidence||0)),el("td","mono",dt));tbody.appendChild(row)}append(table,thead,tbody);target.appendChild(table)}
function safeAvatarUrl(value){try{const u=new URL(value);return u.protocol==="https:"?u.toString():""}catch(e){return ""}}
async function loadUser(){try{const r=await fetch("/api/user");if(r.ok){const u=await r.json(),img=document.getElementById("avatar"),avatar=safeAvatarUrl(u.avatar_url);document.getElementById("username").textContent=u.login||"";if(avatar){img.src=avatar;img.style.display="block"}else{img.removeAttribute("src");img.style.display="none"}}}catch(e){}}
document.getElementById("health").innerHTML=loading("加载中…","正在获取服务健康数据");document.getElementById("quota").innerHTML=loading("加载中…","正在获取配额消耗数据");document.getElementById("effectiveness").innerHTML=loading("加载中…","正在获取有效性报告");document.getElementById("shadow").innerHTML=loading("加载中…","正在获取影子审计分歧数据");document.getElementById("changes").innerHTML=loading("加载中…","正在获取最近变更数据");loadUser();refresh();refreshTimer=setInterval(refresh,30000);
</script>
</body></html>`;

// ── Helpers ────────────────────────────────────────────────────────────

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function html(body, status = 200) {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function redirect(url, headers = {}) {
  return new Response(null, { status: 302, headers: { Location: url, ...headers } });
}

function getCookie(request, name) {
  const cookie = request.headers.get("Cookie") || "";
  const match = cookie.match(new RegExp(name + "=([^;]+)"));
  return match ? match[1] : "";
}

function cookieValue(name, value, maxAge, { httpOnly = true } = {}) {
  const flags = ["Path=/", "Secure", "SameSite=Lax", "Max-Age=" + maxAge];
  if (httpOnly) flags.push("HttpOnly");
  return name + "=" + value + "; " + flags.join("; ");
}

function clearCookieValue(name) {
  return name + "=; Path=/; Secure; SameSite=Lax; HttpOnly; Max-Age=0";
}

async function githubAPI(path, token) {
  const resp = await fetch("https://api.github.com" + path, {
    headers: { Authorization: "Bearer " + token, "User-Agent": "AiGatewayDashboard", Accept: "application/vnd.github+json" },
  });
  if (!resp.ok) throw new Error("GitHub API " + resp.status);
  return resp.json();
}

function generateSessionToken() {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  let result = "";
  const bytes = new Uint8Array(SESSION_SECRET_LENGTH);
  crypto.getRandomValues(bytes);
  for (let i = 0; i < SESSION_SECRET_LENGTH; i++) result += chars[bytes[i] % chars.length];
  return result;
}

// ── Session store (in-memory, resets on Worker deploy) ────────────────

const sessions = new Map(); // sessionToken → { login, avatar_url, orgs, expires }

function createSession(user) {
  const token = generateSessionToken();
  sessions.set(token, {
    login: user.login,
    avatar_url: user.avatar_url,
    expires: Date.now() + COOKIE_MAX_AGE * 1000,
  });
  // Clean expired sessions
  for (const [k, v] of sessions) {
    if (v.expires < Date.now()) sessions.delete(k);
  }
  return token;
}

function getSession(token) {
  const s = sessions.get(token);
  if (!s || s.expires < Date.now()) {
    sessions.delete(token);
    return null;
  }
  return s;
}

// ── API proxy ──────────────────────────────────────────────────────────

function withoutTrailingSlash(pathname) {
  return pathname.replace(/\/+$/, "");
}

function allowedDashboardApiPath(pathname) {
  const clean = withoutTrailingSlash(pathname);
  return DASHBOARD_API_ROUTES.has(clean) ? clean : "";
}

function shouldIgnoreLegacyEndpointBase(basePath, pathname) {
  return basePath === "/v1/codex-audit" && pathname.startsWith("/v1/ai/");
}

export function buildDashboardApiUrl(rawOrigin, pathname, search = "") {
  if (!rawOrigin || !rawOrigin.trim()) throw new Error("AI_GATEWAY_ORIGIN_URL not configured");
  const clean = allowedDashboardApiPath(pathname);
  if (!clean) throw new Error("dashboard API route is not allowed");
  const origin = new URL(rawOrigin.trim());
  if (origin.protocol !== "https:") throw new Error("AI_GATEWAY_ORIGIN_URL must use HTTPS");
  const basePath = withoutTrailingSlash(origin.pathname);
  origin.pathname = !basePath || basePath === "/" || shouldIgnoreLegacyEndpointBase(basePath, clean) ? clean : basePath + clean;
  origin.search = search;
  origin.hash = "";
  return origin.toString();
}

async function proxyAPI(path, search, env) {
  const token = (env.DASHBOARD_API_TOKEN || "").trim();
  const url = buildDashboardApiUrl(env.AI_GATEWAY_ORIGIN_URL || "", path, search);
  const resp = await fetch(url, {
    headers: { Authorization: token ? "Bearer " + token : "", Accept: "application/json", "User-Agent": "AiGatewayDashboard/1.0" },
  });
  const body = await resp.text();
  if (resp.status === 401 || resp.status === 403) {
    return json(
      { status: "error", error: "后端认证失败：请同步 Dashboard API Token 与服务端静态访问 token" },
      502,
    );
  }
  return new Response(body, {
    status: resp.status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

// ── Main ───────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    // ── OAuth callback ─────────────────────────────────────────────
    if (path === "/callback") {
      const code = url.searchParams.get("code");
      const state = url.searchParams.get("state") || "";
      const expectedState = getCookie(request, STATE_COOKIE_NAME);
      if (!code) return redirect("/?error=" + encodeURIComponent("缺少授权码"));
      if (!state || !expectedState || state !== expectedState) {
        return redirect("/?error=" + encodeURIComponent("OAuth state 校验失败"));
      }

      try {
        // Exchange code for access token
        const tokenResp = await fetch(GITHUB_OAUTH_ACCESS_TOKEN, {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({
            client_id: env.GITHUB_OAUTH_CLIENT_ID,
            client_secret: env.GITHUB_OAUTH_CLIENT_SECRET,
            code,
          }),
        });
        const tokenData = await tokenResp.json();
        if (tokenData.error) throw new Error(tokenData.error_description || tokenData.error);

        const accessToken = tokenData.access_token;

        // Get user info + org membership
        const [user, orgs] = await Promise.all([
          githubAPI("/user", accessToken),
          githubAPI("/user/orgs", accessToken),
        ]);

        const isMember = orgs.some(o => o.login === REQUIRED_ORG);
        if (!isMember) {
          return redirect("/?error=" + encodeURIComponent("仅限 " + REQUIRED_ORG + " 组织成员访问"));
        }

        // Create session
        const sessionToken = createSession(user);
        const headers = new Headers({ Location: "/" });
        headers.append("Set-Cookie", cookieValue(COOKIE_NAME, sessionToken, COOKIE_MAX_AGE));
        headers.append("Set-Cookie", clearCookieValue(STATE_COOKIE_NAME));
        return new Response(null, {
          status: 302,
          headers,
        });
      } catch (e) {
        return redirect("/?error=" + encodeURIComponent("登录失败: " + e.message));
      }
    }

    // ── Login redirect ─────────────────────────────────────────────
    if (path === "/login") {
      const clientId = env.GITHUB_OAUTH_CLIENT_ID;
      if (!clientId) return html("<h1>OAuth 未配置</h1><p>请设置 GITHUB_OAUTH_CLIENT_ID 和 GITHUB_OAUTH_CLIENT_SECRET 环境变量</p>", 500);
      const state = generateSessionToken();
      const authUrl = GITHUB_OAUTH_AUTHORIZE
        + "?client_id=" + encodeURIComponent(clientId)
        + "&redirect_uri=" + encodeURIComponent(url.origin + "/callback")
        + "&scope=read:org"
        + "&state=" + encodeURIComponent(state);
      return redirect(authUrl, { "Set-Cookie": cookieValue(STATE_COOKIE_NAME, state, 600) });
    }

    // ── Logout ─────────────────────────────────────────────────────
    if (path === "/logout") {
      const token = getCookie(request, COOKIE_NAME);
      if (token) sessions.delete(token);
      return new Response(null, {
        status: 302,
        headers: {
          Location: "/",
          "Set-Cookie": clearCookieValue(COOKIE_NAME),
        },
      });
    }

    // ── User info (for dashboard header) ───────────────────────────
    if (path === "/api/user") {
      const token = getCookie(request, COOKIE_NAME);
      const session = getSession(token);
      if (!session) return json({ error: "unauthorized" }, 401);
      return json({ login: session.login, avatar_url: session.avatar_url });
    }

    // ── API proxy (requires session) ───────────────────────────────
    if (path.startsWith("/api/")) {
      const token = getCookie(request, COOKIE_NAME);
      const session = getSession(token);
      if (!session) return json({ status: "error", error: "unauthorized" }, 401);

      const apiPath = path.slice(4);
      try {
        return await proxyAPI(apiPath, url.search, env);
      } catch (e) {
        return json({ status: "error", error: e.message }, 502);
      }
    }

    // ── Dashboard HTML (requires session) ──────────────────────────
    if (path === "/" || path === "") {
      const token = getCookie(request, COOKIE_NAME);
      const session = getSession(token);
      if (!session) return html(LOGIN_HTML);
      return html(DASHBOARD_HTML);
    }

    return new Response("Not found", { status: 404 });
  },
};
