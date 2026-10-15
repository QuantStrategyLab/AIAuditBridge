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
export const REQUIRED_ORG = "QuantStrategyLab";
const COOKIE_NAME = "dash_session";
const STATE_COOKIE_NAME = "dash_oauth_state";
const COOKIE_MAX_AGE = 86400; // 24h
const SESSION_SECRET_LENGTH = 32;
const DIAGNOSIS_JOB_ID_PATTERN = /^[A-Za-z0-9_-]{32}$/;
const DIAGNOSIS_TASKS = new Map([
  ["operational_data_diagnosis", "operational"],
  ["historical_operational_diagnosis_rehearsal", "historical_rehearsal"],
]);
const DIAGNOSIS_SOURCE_REPOSITORY = "QuantStrategyLab/QuantAIService";
const DIAGNOSIS_MAX_RESPONSE_BYTES = 1_000_000;
const DASHBOARD_API_ROUTES = new Set([
  "/v1/ai/health",
  "/v1/ai/org-health",
  "/v1/ai/quota",
  "/v1/ai/changes",
  "/v1/ai/changes/effectiveness",
  "/v1/ai/feedback/shadow",
]);

export function codexRemainingPercent(raw) {
  if (!raw || typeof raw !== "object") return null;
  if (raw.remaining_percent !== null && raw.remaining_percent !== undefined) {
    const explicit = Number(raw.remaining_percent);
    if (Number.isFinite(explicit)) return Math.max(0, Math.min(100, Math.round(explicit)));
  }
  const used = Number(raw.used_percent);
  if (!Number.isFinite(used)) return null;
  return Math.max(0, Math.min(100, Math.round(100 - used)));
}

export function codexWindowLabel(raw) {
  const mins = Number(raw && raw.window_duration_mins) || 0;
  if (!mins) return "窗口";
  if (mins % 1440 === 0) return Math.round(mins / 1440) + "d";
  if (mins % 60 === 0) return Math.round(mins / 60) + "h";
  return mins + "m";
}

export function codexWindowDisplay(raw) {
  const remaining = codexRemainingPercent(raw);
  if (remaining === null) return "";
  return codexWindowLabel(raw) + " 剩余 " + remaining + "%";
}

export function codexRemainingClass(raw) {
  const remaining = codexRemainingPercent(raw);
  if (remaining === null) return "ok";
  return remaining < 20 ? "err" : remaining < 50 ? "warn" : "ok";
}

export function requiresHumanAudit(change) {
  if (!change || typeof change !== "object") return false;
  if (typeof change.human_review_required === "boolean") {
    return change.human_review_required;
  }
  const state = String(change.state || "");
  if ([
    "human_review_required",
    "human_review_pr_opened",
    "human_review_waiting_for_ci",
    "human_review_auto_merge_requested",
  ].includes(state)) return true;
  const action = String(change.action || "");
  const risk = String(change.risk || "");
  const effect = String(change.effect || "");
  return (
    action === "escalate"
    || action === "manual"
    || risk === "critical"
    || risk === "high"
    || effect === "degraded"
    || Boolean(change.rollback_issue_url)
  );
}

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
      <div class="tagline">AI 用量、变更与效果，一屏看清</div>
      <p class="desc">AiGateway 汇总 QuantStrategyLab 内部的 AI 使用情况：哪些服务在跑、花了多少额度、每次改动效果如何、哪里需要你过一眼，一个页面看全。</p>
      <div class="err" id="err"></div>
      <div class="login-title">组织成员登录</div>
      <div class="login-note">用 GitHub 账号登录，查看内部的 AI 使用情况。</div>
      <a href="/login" class="btn">
        <svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>
        使用 GitHub 登录
      </a>
      <div class="limited">仅限 QuantStrategyLab 组织成员访问</div>
      <div class="capabilities">
        <h2>登录后，您可以查看</h2>
        <ul>
          <li>各服务是否正常，以及接口延迟、错误率</li>
          <li>各仓库 / 团队的用量和额度</li>
          <li>改动效果评估（90 天）</li>
          <li>AI 判断与预期不一致的地方</li>
          <li>最近 7 天的变更记录</li>
        </ul>
      </div>
    </section>
  </main>
  <footer><span>AiGateway</span><span>·</span><span>QuantStrategyLab</span><span>仅限组织内部使用</span></footer>
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
  :root{color-scheme:dark;--bg:#0b1117;--panel:#141b24;--panel2:#1b2430;--border:#27313e;--text:#e8ecf2;--muted:#a0acba;--muted2:#8b98a8;--muted3:#7a889a;--green:#6ed4a6;--green-bg:#17342a;--amber:#f4b844;--amber-bg:#30250e;--red:#f87171;--red-bg:#382024;--blue:#80b4ff;--blue-bg:#1e304a;--violet:#b4a3ef;--violet-bg:#302a42;--radius:12px}
  *{margin:0;padding:0;box-sizing:border-box}body{min-height:100vh;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--text);background:var(--bg);-webkit-font-smoothing:antialiased}button,a,summary{touch-action:manipulation}button,a{font:inherit}button,summary{cursor:pointer}button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--blue);outline-offset:3px}
  .shell{min-height:100vh;display:grid;grid-template-rows:auto 1fr auto}.topbar{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:16px 28px;border-bottom:1px solid var(--border)}.brand{display:flex;align-items:center;gap:10px}.brand h1{font-size:18px}.brand span{color:var(--muted);font-size:12px}.brand>div:last-child{display:flex;align-items:baseline;gap:10px}.mark{width:24px;color:var(--blue)}.mark svg{width:24px;height:24px}.actions{display:flex;flex-wrap:wrap;align-items:center;gap:12px;color:var(--muted);font-size:12px}.status-pill,.delta,.badge{display:inline-flex;align-items:center;gap:6px;border-radius:6px;padding:4px 8px;font-size:12px}.ok,.badge-ok{color:var(--green)}.warn,.badge-warn{color:var(--amber)}.err,.badge-err{color:var(--red)}.info,.badge-info{color:var(--blue)}.badge-violet{color:var(--violet)}.delta.ok,.status-pill.ok,.badge-ok{background:var(--green-bg)}.delta.warn,.status-pill.warn,.badge-warn{background:var(--amber-bg)}.delta.err,.status-pill.err,.badge-err{background:var(--red-bg)}.delta.info,.badge-info{background:var(--blue-bg)}.pulse{width:6px;height:6px;border-radius:50%;background:currentColor}.user{display:flex;align-items:center;gap:8px}.user img{display:none;width:24px;height:24px;border-radius:50%}.logout{color:var(--muted);text-decoration:none;padding:6px 8px;border:1px solid var(--border);border-radius:6px}
  main{width:min(1200px,100%);margin:0 auto;padding:28px 28px 40px}.summary-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:24px}.card{min-width:0;border:1px solid var(--border);border-radius:var(--radius);background:var(--panel)}.card-body{padding:20px}.card-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:16px}.card h2,.audit-heading{font-size:15px}.summary-card .card-body{padding:16px}.summary-card h2{font-size:12px;font-weight:500;color:var(--muted)}.summary-card .card-head{margin-bottom:12px}.updated,.subtle,.diagnosis-meta{color:var(--muted2);font-size:12px;line-height:1.6}.updated{overflow-wrap:anywhere}.big{font-size:28px;font-weight:650;font-family:ui-monospace,"SFMono-Regular",monospace;font-variant-numeric:tabular-nums;line-height:1.2}.big.blue{color:var(--blue)}.big.violet{color:var(--violet)}.metric-line{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;margin-bottom:8px}.summary-card .empty,.summary-card .panel-error{min-height:40px;font-size:12px}.summary-card .empty strong{font-size:15px}.summary-card details{margin-top:8px}.summary-card summary{font-size:12px;color:var(--muted)}
  .attention-card{margin-bottom:24px;border-color:#5b461f;background:#18150d}.attention-card h2{color:#f2d594}.attention-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;align-items:start;padding:14px 0;border-top:1px solid #40331a}.attention-title{font-size:14px;overflow-wrap:anywhere}.attention-meta{font-size:12px;color:#b7a67f;margin-top:4px;overflow-wrap:anywhere}.attention-view{display:inline-flex;align-items:center;justify-content:center;min-height:32px;padding:6px 12px;border:0;border-radius:6px;background:var(--amber);color:#281c06;text-decoration:none;font-size:12px;font-weight:600}.attention-detail{grid-column:2/-1;white-space:pre-wrap;font-size:12px;line-height:1.6;overflow-wrap:anywhere}.attention-detail[hidden]{display:none}.source-status{font-size:12px;color:var(--muted);margin-bottom:8px}.quota-card{margin-bottom:24px}.audit-heading{color:var(--muted);margin:0 0 12px}.audit-records{display:grid;gap:8px}.audit-panel{border:1px solid var(--border);border-radius:10px;background:var(--panel);min-width:0}.audit-panel>summary{display:flex;flex-wrap:wrap;align-items:center;justify-content:flex-start;gap:8px;padding:14px 16px;font-size:14px;font-weight:550}.audit-panel>summary:before{content:'›';color:var(--muted);margin-right:2px}.audit-panel>summary>.updated{margin-left:auto}.audit-panel[open]>summary:before{content:'⌄'}.audit-panel>.card-body{padding:4px 16px 16px}.stat-row{display:flex;justify-content:space-between;gap:14px;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px}.stat-row:last-child{border-bottom:0}.stat-label{min-width:0;color:var(--muted);overflow-wrap:anywhere}.stat-value{max-width:68%;text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.subtle{margin-top:12px}.quota-section{display:grid;gap:10px;padding-top:14px;margin-top:14px;border-top:1px solid var(--border)}.quota-section:first-child{padding:0;margin:0;border:0}.section-title{font-size:12px;color:var(--muted)}.quota-list{display:grid;gap:14px}.quota-item{display:grid;grid-template-columns:minmax(140px,1fr) minmax(0,3fr) auto;gap:16px;align-items:center}.quota-name{font-size:13px;overflow-wrap:anywhere}.quota-meta,.bar-label{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}.bar{height:8px;border-radius:4px;background:#27313c;overflow:hidden}.bar-fill{height:100%;border-radius:4px;background:var(--blue)}.bar-fill.warn{background:var(--amber)}.bar-fill.err{background:var(--red)}
  .empty,.loading,.panel-error{padding:16px 0;font-size:13px;color:var(--muted);overflow-wrap:anywhere}.empty strong,.loading strong,.panel-error strong{display:block;margin-bottom:6px;color:var(--text)}.panel-error{color:var(--red)}.panel-error strong{color:var(--red)}.loading span{font-size:12px}.diagnosis-item{padding:12px 0;border-bottom:1px solid var(--border)}.diagnosis-line{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px}.diagnosis-button{padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--blue-bg);color:var(--blue)}.diagnosis-output{margin-top:10px;padding:12px;background:var(--bg);border-radius:6px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 ui-monospace,monospace}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:10px;text-align:left;border-bottom:1px solid var(--border)}th{color:var(--muted);font-weight:500}.mono{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums}.toast{position:fixed;top:16px;right:16px;max-width:calc(100vw - 32px);padding:12px 16px;background:var(--red-bg);color:var(--red);border:1px solid var(--red);border-radius:8px;display:none;z-index:10;font-size:13px}footer{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;padding:16px 28px;border-top:1px solid var(--border);color:var(--muted3);font-size:12px}
  @media(max-width:900px){.summary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.topbar{align-items:flex-start;flex-direction:column}.actions{width:100%}}@media(max-width:540px){main{padding:20px 16px}.topbar,footer{padding:16px}.summary-grid{grid-template-columns:minmax(0,1fr)}.card-body{padding:16px}.brand>div:last-child{flex-wrap:wrap}.quota-item{grid-template-columns:minmax(0,1fr)}.stat-row{flex-direction:column;gap:4px}.stat-value{max-width:100%;text-align:left}.attention-row{gap:8px}.attention-title{font-size:13px}}
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
    <section class="summary-grid" aria-label="健康概览">
      <article class="card summary-card"><div class="card-body"><div class="card-head"><h2>服务健康</h2></div><div id="health-summary"></div><span class="updated" id="health-updated">更新: —</span><details><summary>服务明细</summary><div id="health"></div></details></div></article>
      <article class="card summary-card"><div class="card-body"><div class="card-head"><h2>组织健康</h2></div><div id="org-health-summary"></div><span class="updated" id="org-health-updated">更新: —</span><details><summary>组织明细</summary><div id="org-health"></div></details></div></article>
      <article class="card summary-card"><div class="card-body"><div class="card-head"><h2>自动操作 · 7 天</h2></div><div id="autonomy-summary"></div><span class="updated" id="autonomy-updated">更新: —</span></div></article>
      <article class="card summary-card"><div class="card-body"><div class="card-head"><h2>改动效果 · 90 天</h2></div><div id="effectiveness-summary"></div><span class="updated" id="effectiveness-updated">更新: —</span></div></article>
    </section>
    <article class="card attention-card"><div class="card-body"><div class="card-head"><h2>需要你处理</h2><span class="updated">待人工处理 + 判断分歧</span></div><div class="source-status"><span id="human-audit-updated">人工队列未读到</span> · <span id="shadow-updated">分歧未读到</span></div><div id="attention"></div></div></article>
    <article class="card quota-card"><div class="card-body"><div class="card-head"><h2>用量与额度</h2><span class="updated" id="quota-updated">更新: —</span></div><div id="quota"></div></div></article>
    <h2 class="audit-heading">审计与记录</h2>
    <section class="audit-records">
      <details class="audit-panel"><summary>访问范围<span class="updated">谁能访问哪些组织</span></summary><div class="card-body"><p>仅 QuantStrategyLab 组织成员可见。请勿外传。</p></div></details>
      <details class="audit-panel"><summary>诊断记录<span class="updated" id="diagnoses-updated">更新: —</span></summary><div class="card-body" id="diagnoses"></div></details>
      <details class="audit-panel"><summary>最近变更 · 7 天<span class="updated" id="changes-updated">更新: —</span></summary><div class="card-body table-wrap" id="changes"></div></details>
      <details class="audit-panel"><summary>改动效果明细 · 90 天<span class="updated">按改动展开</span></summary><div class="card-body" id="effectiveness"></div></details>
      <details class="audit-panel"><summary>自动操作明细 · 7 天<span class="updated">按操作展开</span></summary><div class="card-body" id="autonomy"></div></details>
    </section>
  </main>
  <footer><span>自动刷新 · 30s</span><span>AiGateway · QuantStrategyLab</span></footer>
</div>
<script>
const API="/api";let refreshTimer;
function fmtUSD(n){return "$"+(Number(n)||0).toFixed(2)}
function fmtPct(n){return ((Number(n)||0)*100).toFixed(0)+"%"}
function fmtNum(n){return (Number(n)||0).toLocaleString()}
function clsStatus(st){return st==="healthy"?"ok":st==="degraded"||st==="unknown"?"warn":"err"}
function statusText(st){return st==="healthy"?"健康":st==="degraded"?"部分异常":st==="unknown"?"未知":st==="unhealthy"?"异常":st||"unknown"}
function clear(el){el.replaceChildren()}
function el(tag,className,text){const n=document.createElement(tag);if(className)n.className=className;if(text!==undefined)n.textContent=String(text);return n}
function append(parent){for(let i=1;i<arguments.length;i++)parent.appendChild(arguments[i]);return parent}
function showToast(msg){const e=document.getElementById("toast");e.textContent=msg;e.style.display="block";setTimeout(()=>e.style.display="none",5000)}
function setStatus(st){const c=clsStatus(st),p=document.getElementById("status-pill"),pl=document.getElementById("pulse"),t=document.getElementById("status-text");p.className="status-pill "+c;pl.className="pulse "+c;t.textContent=statusText(st)}
function setUpdated(id){document.getElementById(id).textContent="更新: "+new Date().toLocaleTimeString()}
function empty(target,title,text){clear(target);target.appendChild(append(el("div","empty"),append(el("div",""),el("strong","",title),el("span","",text))))}
function publicErrorMessage(error){return String(error&&error.message?error.message:error||"加载失败").replace(/\s+/g," ").slice(0,180)}
function panelError(targetId,title,error){if(targetId==="shadow"||targetId==="human-audit"){const key=targetId==="shadow"?"shadow":"human";attentionSources[key]={items:[],error:publicErrorMessage(error)};renderAttention();return;}const target=document.getElementById(targetId);clear(target);target.appendChild(append(el("div","panel-error"),append(el("div",""),el("strong","",title),el("span","",publicErrorMessage(error)))))}
function statRow(label,value,valueClass){const row=el("div","stat-row");append(row,el("span","stat-label",label),el("span","stat-value "+(valueClass||""),value));return row}
function badge(kind,text){return el("span","badge badge-"+kind,text)}
function loading(title,text){return '<div class="loading"><div><div class="spinner"></div><strong>'+title+'</strong><span>'+text+'</span></div></div>'}
function progressClass(p){return p>80?"err":p>50?"warn":"ok"}
async function fetchJSON(path){const r=await fetch(API+path);let data=null;try{data=await r.json()}catch(e){}if(!r.ok||data&&data.status==="error"){if(path==="/v1/ai/org-health"&&r.status===503&&data&&data.status==="unavailable")return data;const detail=data&&data.error?": "+data.error:"";throw new Error(path+": "+r.status+detail)}if(!data)throw new Error(path+": invalid JSON");return data}
async function refresh(){const requests=[["/v1/ai/health","health","服务健康加载失败",renderHealth],["/v1/ai/org-health","org-health","组织健康加载失败",d=>{renderOrgHealth(d);renderOrgSummary(d)}],["/v1/ai/quota","quota","配额消耗加载失败",d=>renderQuota(d.quota||d)],["/v1/ai/changes/effectiveness?days=90","effectiveness","改动效果报告加载失败",d=>renderEffectiveness(d.report||d)],["/v1/ai/feedback/shadow","shadow","判断分歧加载失败",d=>renderShadow(d.disagreements||[])],["/v1/ai/changes?days=7","changes","最近变更加载失败",d=>renderChangePanels(d.changes||[])],["/diagnoses","diagnoses","数据故障诊断加载失败",renderDiagnoses]],results=await Promise.allSettled(requests.map(r=>fetchJSON(r[0]))),failures=[];results.forEach((result,i)=>{const item=requests[i];try{if(result.status==="fulfilled"){item[3](result.value);if(item[1]==="health"||item[1]==="effectiveness")copyMetric(item[1]);}else throw result.reason}catch(e){failures.push(publicErrorMessage(e));if(item[1]==="changes"){panelError("autonomy","自动操作加载失败",e);panelError("human-audit","待处理队列加载失败",e)}panelError(item[1],item[2],e);if(["health","org-health","effectiveness"].includes(item[1]))panelError(item[1]+"-summary",item[2],e);if(item[1]==="changes")panelError("autonomy-summary","自动操作加载失败",e)}});document.getElementById("last-refresh").textContent=new Date().toLocaleTimeString();if(failures.length){setStatus("error");showToast("API: "+failures.slice(0,2).join("；"))}}
const attentionSources={human:{items:null,error:null},shadow:{items:null,error:null}};
function copyMetric(sourceId){const source=document.getElementById(sourceId),target=document.getElementById(sourceId+"-summary");clear(target);const metric=source.querySelector(".metric-line")||source.firstElementChild;if(metric)target.appendChild(metric.cloneNode(true));}
function renderAttention(){const target=document.getElementById("attention");clear(target);for(const key of ["human","shadow"]){const state=attentionSources[key],label=key==="human"?"待人工":"分歧";if(state.error){append(target,el("p","panel-error",label+"加载失败；"+state.error));continue;}if(state.items===null){append(target,el("p","loading",label+"加载中…"));continue;}for(const item of state.items){const row=el("div","attention-row"),body=el("div",""),title=key==="human"?actionText(item.action):item.plugin||item.repo||"判断分歧",repo=item.repo||item.source_repo||"来源未读到",time=item.created_at?new Date(item.created_at*1000).toLocaleString():"时间未读到",detail=el("div","attention-detail");detail.hidden=true;append(body,el("div","attention-title",title),el("div","attention-meta",repo+" · "+time));detail.textContent=key==="human"?"风险："+riskText(item.risk)+" · 效果："+effectText(item.effect):"AI 判断："+(item.ai_verdict||"未读到")+" · 预期判断："+(item.expected_verdict||"未读到")+" · 分歧次数："+(item.disagreement_count??"未读到");const url=safeExternalUrl(item.external_url),view=el(url?"a":"button","attention-view","查看");if(url){view.href=url;view.target="_blank";view.rel="noreferrer";}else{view.type="button";view.setAttribute("aria-expanded","false");view.addEventListener("click",()=>{detail.hidden=!detail.hidden;view.setAttribute("aria-expanded",String(!detail.hidden));});}append(row,badge("warn",label),body,view,detail);target.appendChild(row);}}if([attentionSources.human,attentionSources.shadow].every(s=>Array.isArray(s.items)&&!s.items.length&&!s.error))empty(target,"暂无待处理","本次读回的人工队列和判断分歧均为空。");}
function renderShadow(items){if(!Array.isArray(items))throw new Error("分歧列表格式无效");setUpdated("shadow-updated");attentionSources.shadow={items,error:null};renderAttention();}
function renderHumanAudit(items){setUpdated("human-audit-updated");attentionSources.human={items:items.filter(requiresHumanAudit),error:null};renderAttention();}
function renderOrgSummary(h){const target=document.getElementById("org-health-summary"),s=h.summary||{},keys=["total_repositories","unhealthy_repositories","unknown_repositories","degraded_repositories"],values=keys.map(k=>s[k]);clear(target);if(h.provider?.status==="unavailable"||!values.every(v=>Number.isSafeInteger(v)&&v>=0)||values[0]<values[1]+values[2]+values[3]){append(target,el("div","big","未读到"),el("p","subtle","组织正常 / 总数未知"));return;}append(target,el("div","big",fmtNum(values[0]-values[1]-values[2]-values[3])+" / "+fmtNum(values[0])),el("p","subtle","组织正常 / 总数"));}
function renderAutonomySummary(items){const target=document.getElementById("autonomy-summary"),automatic=items.filter(c=>["auto_merge","auto_pr","auto_notify"].includes(c.action));clear(target);append(target,el("div","big",fmtNum(automatic.length)),el("p","subtle","已登记自动操作；失败次数未读到"));}
function healthReasonText(r){const path=r.path||"endpoint";if(r.reason==="error_rate")return path+" 错误率 "+fmtPct(r.value||0)+" ≥ "+fmtPct(r.threshold||0);if(r.reason==="p95_latency_ms")return path+" P95 "+fmtNum(r.value||0)+" ms ≥ "+fmtNum(r.threshold||0)+" ms";return path+" "+(r.reason||"degraded")}
function renderHealth(h){setStatus(h.status||"unknown");setUpdated("health-updated");const target=document.getElementById("health"),endpoints=h.endpoints||[],reasons=h.degradation_reasons||[],statusEndpoints=endpoints.filter(i=>i.latency_profile!=="background_job"),backgroundEndpoints=endpoints.filter(i=>i.latency_profile==="background_job"),total=endpoints.reduce((s,i)=>s+(Number(i.total)||0),0),errors=endpoints.reduce((s,i)=>s+(Number(i.errors)||0),0),p95=Math.max(0,...statusEndpoints.map(i=>Number(i.p95_ms)||0)),backgroundP95=Math.max(0,...backgroundEndpoints.map(i=>Number(i.p95_ms)||0)),details=el("div","");append(details,statRow("接口数量",endpoints.length,"info"),statRow("总请求",fmtNum(total),""),statRow("错误",fmtNum(errors),errors?"err":"ok"),statRow("在线接口 P95",fmtNum(p95)+" ms",p95>30000?"err":p95>10000?"warn":"ok"));if(backgroundP95)append(details,statRow("后台任务 P95",fmtNum(backgroundP95)+" ms","info"));if(reasons.length){reasons.slice(0,3).forEach((reason,i)=>append(details,statRow(i?"降级原因 "+(i+1):"降级原因",healthReasonText(reason),clsStatus(reason.severity||h.status))));if(reasons.length>3)append(details,statRow("更多原因","+"+fmtNum(reasons.length-3),"warn"))}else if(h.last_error&&h.last_error.message){append(details,statRow("最近错误",String(h.last_error.message).slice(0,80),"warn"))}clear(target);append(target,append(el("div","metric-line"),el("div","big ",statusText(h.status||"unknown")),el("span","delta "+clsStatus(h.status||"unknown"),fmtNum(Math.round((h.uptime_seconds||0)/3600))+" h")),details)}
function orgReasonText(reason){return reason==="latest_run_in_progress"?"最新 workflow 运行中":reason==="no_workflow_runs"?"暂无 workflow run":reason==="github_api_error"?"GitHub API 读取失败":String(reason||"需要关注").replaceAll("_"," ")}
function renderOrgHealth(h){setUpdated("org-health-updated");const target=document.getElementById("org-health"),provider=h.provider||{},summary=h.summary||{},repos=h.repositories||[];clear(target);if(provider.status==="unavailable"){empty(target,"组织健康未连接","配置 CODEX_AUDIT_SERVICE_GITHUB_TOKEN 后，会显示组织仓库 Actions 健康。",);return}const unhealthy=Number(summary.unhealthy_repositories)||0,unknown=Number(summary.unknown_repositories)||0,degraded=Number(summary.degraded_repositories)||0,failed=Number(summary.failed_workflow_runs)||0,running=Number(summary.in_progress_workflow_runs)||0,total=Number(summary.total_repositories)||repos.length,problemRepos=repos.filter(r=>r.status!=="healthy").slice(0,5),statusClass=unhealthy?"err":degraded||unknown?"warn":"ok";append(target,append(el("div","metric-line"),el("div","big ",unhealthy?"异常":degraded?"关注":unknown?"未知":"健康"),el("span","delta "+statusClass,fmtNum(total)+" repos")));const details=el("div","");append(details,statRow("异常仓库",unhealthy,unhealthy?"err":"ok"),statRow("未知仓库",unknown,unknown?"warn":"ok"),statRow("需关注仓库",degraded,degraded?"warn":"ok"),statRow("失败 workflow",failed,failed?"err":"ok"),statRow("运行中",running,running?"info":"ok"));append(target,details);if(problemRepos.length){const list=el("div","");for(const r of problemRepos){const latest=r.latest_run||{},problem=r.problem_run||{},repoName=String(r.repo||"").split("/").pop()||"—",label=(problem.name||latest.name||orgReasonText((r.reasons||[])[0])),runUrl=problem.url||latest.url;const row=el("div","stat-row"),value=el("span","stat-value "+clsStatus(r.status));if(runUrl){const a=el("a","",label);a.href=runUrl;a.target="_blank";a.rel="noreferrer";a.style.color="inherit";a.style.textDecoration="none";value.appendChild(a)}else value.textContent=label;append(row,el("span","stat-label",repoName),value);list.appendChild(row)}target.appendChild(quotaSection("需要处理",list))}}

function quotaTotalRow(label,d){const row=el("div","stat-row"),used=Number(d&&d.total_cost_usd)||0;append(row,el("span","stat-label",label),el("span","stat-value info",fmtUSD(used)));return row}
${codexRemainingPercent.toString()}
${codexWindowLabel.toString()}
${codexWindowDisplay.toString()}
${codexRemainingClass.toString()}
function codexAccountRow(d){const row=el("div","stat-row"),r=d&&d.rate_limits||{},p=r.primary||{},s=r.secondary||{},credits=r.credits||{},parts=[],primary=codexWindowDisplay(p),secondary=codexWindowDisplay(s);if(primary)parts.push(primary);if(secondary)parts.push(secondary);if(r.plan_type)parts.push(String(r.plan_type));if(credits.balance!==null&&credits.balance!==undefined)parts.push("credits "+credits.balance);append(row,el("span","stat-label","Codex 账户"),el("span","stat-value "+codexRemainingClass(p),parts.join(" · ")||"实时可用"));return row}
function providerUsageBreakdown(u){const input=Number(u&&u.input_tokens)||0,output=Number(u&&u.output_tokens)||0,uncached=Number(u&&u.uncached_input_tokens)||0,cached=Number(u&&u.input_cached_tokens)||0,parts=[];if(input||output||uncached)parts.push("in "+fmtNum(input||uncached)+" / out "+fmtNum(output));if(cached)parts.push("cached "+fmtNum(cached));return parts.join(" · ")}
function providerAccountRow(label,d,usageKey){const row=el("div","stat-row"),c=d&&d.costs||{},u=d&&d[usageKey]||{},days=Number(d&&d.window_days)||7,currency=String(c.currency||"usd").toUpperCase(),cost=Number(c.total_cost)||0,textTokens=(Number(u.input_tokens)||0)+(Number(u.output_tokens)||0),audioTokens=(Number(u.input_audio_tokens)||0)+(Number(u.output_audio_tokens)||0),tokens=Number(u.total_tokens)||(textTokens+audioTokens),cached=Number(u.input_cached_tokens)||0,requests=Number(u.num_model_requests)||0,breakdown=providerUsageBreakdown(u),parts=[days+"d"];if(c.total_cost!==null&&c.total_cost!==undefined)parts.push(currency==="USD"?fmtUSD(cost):cost.toFixed(2)+" "+currency);if(requests)parts.push(fmtNum(requests)+" req");if(tokens)parts.push(fmtNum(tokens)+" tokens");if(breakdown)parts.push(breakdown);if(audioTokens)parts.push(fmtNum(audioTokens)+" audio");if(cached&&!breakdown)parts.push(fmtNum(cached)+" cached");if(d&&d.usage_surface)parts.push(String(d.usage_surface));if(d&&d.filtered_project_count)parts.push(fmtNum(d.filtered_project_count)+" project");if(d&&d.filtered_api_key_count)parts.push(fmtNum(d.filtered_api_key_count)+" key");if(d&&d.filtered_workspace_count)parts.push(fmtNum(d.filtered_workspace_count)+" workspace");append(row,el("span","stat-label",label),el("span","stat-value info",parts.join(" · ")||"实时可用"));return row}
function providerCostRow(label,d,days){if(!d||d.total_cost===null||d.total_cost===undefined)return statRow(label,(Number(days)||7)+"d 成本暂不可用 · Usage API 已连接","warn");const row=el("div","stat-row"),currency=String(d.currency||"usd").toUpperCase(),cost=Number(d.total_cost)||0,parts=[(Number(days)||7)+"d "+(currency==="USD"?fmtUSD(cost):cost.toFixed(2)+" "+currency)];append(row,el("span","stat-label",label),el("span","stat-value info",parts.join(" · ")));return row}
function quotaHasUsage(d){return Boolean(Number(d&&d.total_cost_usd)||Number(d&&d.calls)||Number(d&&d.tokens_input)||Number(d&&d.tokens_output)||(d&&d.calls_incomplete))}
function quotaUsageRow(label,d,extra){const row=el("div","stat-row"),calls=Number(d&&d.calls)||0,used=Number(d&&d.total_cost_usd)||0,tokens=(Number(d&&d.tokens_input)||0)+(Number(d&&d.tokens_output)||0),callText=d&&d.calls_incomplete?(calls?"≥ "+fmtNum(calls):"未知次数"):fmtNum(calls),detail=fmtUSD(used)+" · "+callText+" 次"+(tokens?" · "+fmtNum(tokens)+" tokens":"")+(extra?" · "+extra:"");append(row,el("span","stat-label",label),el("span","stat-value info",detail));return row}
function appendRepoQuotaRows(list,repos){for(const name of Object.keys(repos)){const d=repos[name]||{},used=Number(d.total_cost_usd)||0,budget=Number(d.daily_budget)||0,p=budget?used/budget*100:0,c=progressClass(p),item=el("div","quota-item"),meta=el("div",""),bar=el("div","bar"),fill=el("div","bar-fill "+c);fill.style.width=Math.max(0,Math.min(p,100))+"%";append(meta,el("div","quota-name",String(name).split("/").pop()||name),el("div","quota-meta",fmtUSD(used)+" / "+fmtUSD(budget)+" repo 日预算"));append(bar,fill);append(item,meta,bar,el("div","bar-label",Math.round(p)+"%"));list.appendChild(item)}}
function quotaSection(title,node){return append(el("section","quota-section"),el("div","section-title",title),node)}
function renderQuota(q){setUpdated("quota-updated");const target=document.getElementById("quota"),repos=q.repos||{},repoCount=Object.keys(repos).length,summary=q.summary||null,account=summary&&summary.codex_account&&summary.codex_account.status==="available"?summary.codex_account:null,openai=summary&&summary.openai_account&&summary.openai_account.status==="available"?summary.openai_account:null,anthropic=summary&&summary.anthropic_account&&summary.anthropic_account.status==="available"?summary.anthropic_account:null,hasSummaryUsage=summary&&(account||openai||anthropic||quotaHasUsage(summary.combined)||quotaHasUsage(summary.api_key)||quotaHasUsage(summary.codex)||quotaHasUsage(summary.legacy_unknown));clear(target);if(summary&&(repoCount||hasSummaryUsage)){if(openai||anthropic||account){const provider=el("div","");if(openai)append(provider,providerAccountRow("GPT API 账户",openai,"completions"),providerCostRow("GPT 组织成本",openai.organization_costs,openai.window_days));if(anthropic)append(provider,providerAccountRow("Claude API 账户",anthropic,"messages"),providerCostRow("Claude 成本",anthropic.costs,anthropic.window_days));if(account)append(provider,codexAccountRow(account));target.appendChild(quotaSection("实时账户用量",provider))}const internal=el("div","");append(internal,quotaTotalRow("内部合计估算",summary.combined||{}),quotaUsageRow("本服务 API Key 估算",summary.api_key||{}),quotaUsageRow("本服务 Codex 估算",summary.codex||{}));if(quotaHasUsage(summary.legacy_unknown))append(internal,quotaUsageRow("历史未拆分",summary.legacy_unknown||{},"未归属"));target.appendChild(quotaSection("内部成本估算",internal));if(repoCount){const list=el("div","quota-list");appendRepoQuotaRows(list,repos);target.appendChild(quotaSection("仓库日预算",list))}target.appendChild(el("div","subtle","实时账户用量来自 GPT/Claude Admin Usage 与本机 Codex rate-limit 快照；成本暂不可用表示对应 Cost API 未返回金额，不代表没有用量。内部成本估算仅统计本服务记录的 API Key/Codex 调用，不等同于云厂商账单。"));return}if(!repoCount){empty(target,"暂无用量","当前窗口内没有记录到配额消耗。API Key 与 Codex 均无用量记录。");return}const list=el("div","quota-list");appendRepoQuotaRows(list,repos);target.appendChild(list)}
function renderEffectiveness(e){setUpdated("effectiveness-updated");const target=document.getElementById("effectiveness"),evaluated=Number(e.evaluated)||0,pending=Number(e.pending)||0,total=Number(e.total_changes)||evaluated+pending,rate=Number(e.improvement_rate)||0;clear(target);if(!total){empty(target,"暂无评估样本","最近 90 天没有登记可评估的变更；有变更完成并回填评估后，这里会显示改善率。");return}if(!evaluated){append(target,append(el("div","metric-line"),el("div","big blue","待评估"),el("span","delta info",fmtNum(total)+" 条")),append(el("div",""),statRow("已登记",total,"info"),statRow("待评估",pending,"warn"),statRow("改善率","评估后生成","info")));return}append(target,append(el("div","metric-line"),el("div","big blue",fmtPct(rate)),el("span","delta "+(rate>0.7?"ok":rate>0.4?"warn":"info"),"改善率")),append(el("div",""),statRow("已评估",evaluated,"info"),statRow("改善",e.improved||0,"ok"),statRow("退化",e.degraded||0,"err"),statRow("持平",e.neutral||0,""),statRow("待评估",pending,"info")))}

function diagnosisStatusText(status){return status==="reviewed"||status==="succeeded"?"诊断完成":status==="queued"?"等待诊断":status==="running"?"诊断中":status==="failed"||status==="blocked"?"诊断未完成":"状态未知"}
function renderDiagnosisItems(target,items,emptyTitle,emptyText){if(!items.length){empty(target,emptyTitle,emptyText);return}for(const item of items.slice(0,20)){const box=el("div","diagnosis-item"),line=el("div","diagnosis-line"),meta=el("div",""),output=el("pre","diagnosis-output","诊断完成仅提供建议，不授予修改、发布或交易权限。"),button=el("button","diagnosis-button","查看结果");output.hidden=true;button.type="button";append(meta,el("div","",diagnosisStatusText(item.status)),el("div","diagnosis-meta",item.updated_at?new Date(Number(item.updated_at)*1000).toLocaleString():"时间未知"));button.addEventListener("click",async()=>{output.hidden=false;output.textContent="正在读取诊断结果…";try{const detail=await fetchJSON("/diagnoses/"+encodeURIComponent(item.job_id)),diagnosis=detail.diagnosis||{};output.textContent=diagnosis.output||diagnosis.message||"诊断结果不可用"}catch(e){output.textContent="诊断结果暂不可用"}});append(line,meta,button);append(box,line,output);target.appendChild(box)}}
function renderDiagnoses(data){setUpdated("diagnoses-updated");const target=document.getElementById("diagnoses"),items=Array.isArray(data.diagnoses)?data.diagnoses:[],operational=items.filter(i=>i.kind==="operational"),rehearsals=items.filter(i=>i.kind==="historical_rehearsal"),operationalSection=el("section",""),rehearsalSection=el("section","");clear(target);append(operationalSection,el("h3","","数据故障诊断"));renderDiagnosisItems(operationalSection,operational,"暂无数据故障诊断","尚无生产数据故障诊断记录。");append(rehearsalSection,el("h3","","历史故障诊断演练"));renderDiagnosisItems(rehearsalSection,rehearsals,"暂无历史演练","尚未运行明确标记的历史故障诊断演练。");append(target,operationalSection,rehearsalSection)}
function effectText(effect){return effect==="improved"?"改善":effect==="degraded"?"退化":effect==="neutral"?"持平":effect==="pending"?"待评估":effect||"待评估"}
function actionText(action){return action==="auto_merge"?"自动合并":action==="auto_pr"?"自动 PR":action==="auto_notify"?"自动通知":action==="escalate"?"升级人工":action==="manual"?"人工处理":action||"变更"}
function riskText(risk){return risk==="critical"?"关键":risk==="high"?"高":risk==="medium"?"中":risk==="low"?"低":risk||"未知"}
function riskClass(risk){return risk==="critical"||risk==="high"?"err":risk==="medium"?"warn":risk==="low"?"ok":"info"}
${requiresHumanAudit.toString()}
function renderChangePanels(items){renderAutonomy(items);renderAutonomySummary(items);renderHumanAudit(items);renderChanges(items.slice(0,15))}
function renderAutonomy(items){setUpdated("autonomy-updated");const target=document.getElementById("autonomy"),autoMerge=items.filter(c=>c.action==="auto_merge").length,autoPr=items.filter(c=>c.action==="auto_pr").length,manual=items.filter(requiresHumanAudit).length,highRisk=items.filter(c=>["critical","high"].includes(String(c.risk||""))).length,pending=items.filter(c=>(c.effect||"pending")==="pending").length;clear(target);if(!items.length){empty(target,"暂无自动操作","最近 7 天没有 AI 自动处理的记录。");return}append(target,append(el("div","metric-line"),el("div","big blue",fmtNum(items.length)),el("span","delta info","已登记")),append(el("div",""),statRow("自动合并",autoMerge,autoMerge?"ok":"info"),statRow("自动 PR",autoPr,"info"),statRow("需人工处理",manual,manual?"warn":"ok"),statRow("高风险/关键",highRisk,highRisk?"err":"ok"),statRow("待效果评估",pending,pending?"warn":"ok")))}

function renderChanges(items){setUpdated("changes-updated");const target=document.getElementById("changes");clear(target);if(!items.length){empty(target,"暂无变更记录","最近 7 天没有登记的 AI 变更。");return}const table=el("table",""),thead=el("thead",""),tr=el("tr","");for(const h of ["仓库","操作","风险","效果","置信度","时间"])tr.appendChild(el("th","",h));thead.appendChild(tr);const tbody=el("tbody","");for(const c of items){const effect=c.effect||"pending",effectClass=effect==="improved"?"ok":effect==="degraded"?"err":"info",actionClass=c.action==="auto_merge"?"ok":c.action==="auto_pr"?"info":c.action?"violet":"warn",dt=c.created_at?new Date(c.created_at*1000).toLocaleDateString():"—",row=el("tr",""),repoName=String(c.repo||"").split("/").pop()||"—",repoCell=el("td","mono"),url=safeExternalUrl(c.external_url);if(url){const a=el("a","",repoName);a.href=url;a.target="_blank";a.rel="noreferrer";a.style.color="#93c5fd";a.style.textDecoration="none";repoCell.appendChild(a)}else repoCell.textContent=repoName;append(row,repoCell,append(el("td",""),badge(actionClass,actionText(c.action))),append(el("td",""),badge(riskClass(c.risk),riskText(c.risk))),el("td","stat-value "+effectClass,effectText(effect)),el("td","mono",fmtPct(c.confidence||0)),el("td","mono",dt));tbody.appendChild(row)}append(table,thead,tbody);target.appendChild(table)}
function safeExternalUrl(value){try{const u=new URL(value);return u.protocol==="https:"?u.toString():""}catch(e){return ""}}
function safeAvatarUrl(value){try{const u=new URL(value);return u.protocol==="https:"?u.toString():""}catch(e){return ""}}
async function loadUser(){try{const r=await fetch("/api/user");if(r.ok){const u=await r.json(),img=document.getElementById("avatar"),avatar=safeAvatarUrl(u.avatar_url);document.getElementById("username").textContent=u.login||"";if(avatar){img.src=avatar;img.style.display="block"}else{img.removeAttribute("src");img.style.display="none"}}}catch(e){}}
document.getElementById("health").innerHTML=loading("加载中…","正在获取服务健康数据");document.getElementById("org-health").innerHTML=loading("加载中…","正在获取组织健康数据");document.getElementById("quota").innerHTML=loading("加载中…","正在获取配额消耗数据");document.getElementById("effectiveness").innerHTML=loading("加载中…","正在获取改动效果报告");renderAttention();document.getElementById("autonomy").innerHTML=loading("加载中…","正在获取自动操作数据");document.getElementById("diagnoses").innerHTML=loading("加载中…","正在获取数据故障诊断");document.getElementById("changes").innerHTML=loading("加载中…","正在获取最近变更数据");loadUser();refresh();refreshTimer=setInterval(refresh,30000);
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

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function base64UrlEncode(value) {
  const bytes = typeof value === "string" ? encoder.encode(value) : new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function base64UrlDecode(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return decoder.decode(bytes);
}

function constantTimeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function sessionSecret(env) {
  return String(env.DASHBOARD_SESSION_SECRET || "").trim();
}

async function hmacSha256(secret, data) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(data));
  return base64UrlEncode(signature);
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

function sessionKV(env) {
  const kv = env && env.DASHBOARD_SESSION_KV;
  return kv && typeof kv.get === "function" && typeof kv.put === "function" && typeof kv.delete === "function" ? kv : null;
}

function sessionKVKey(jti) {
  return "dash_session:" + jti;
}

async function parseSignedSession(token, env) {
  const secret = sessionSecret(env);
  if (!secret) return null;
  const [payload, signature] = String(token || "").split(".");
  if (!payload || !signature) return null;
  const expected = await hmacSha256(secret, payload);
  if (!constantTimeEqual(signature, expected)) return null;
  const session = JSON.parse(base64UrlDecode(payload));
  if (!session || Number(session.exp) < Date.now()) return null;
  return session;
}

async function createSession(user, env) {
  const secret = sessionSecret(env);
  if (!secret) throw new Error("Dashboard session secret not configured");
  const kv = sessionKV(env);
  const session = {
    login: user.login,
    avatar_url: user.avatar_url,
    exp: Date.now() + COOKIE_MAX_AGE * 1000,
  };
  if (kv) {
    session.jti = generateSessionToken();
    await kv.put(sessionKVKey(session.jti), JSON.stringify({ login: session.login, exp: session.exp }), { expirationTtl: COOKIE_MAX_AGE });
  }
  const payload = base64UrlEncode(JSON.stringify(session));
  return payload + "." + await hmacSha256(secret, payload);
}

async function getSession(token, env) {
  try {
    const session = await parseSignedSession(token, env);
    if (!session) return null;
    const kv = sessionKV(env);
    const jti = String(session.jti || "");
    if (jti) {
      if (!kv) return null;
      const active = await kv.get(sessionKVKey(jti));
      if (!active) return null;
    } else if (kv) {
      return null;
    }
    return { login: String(session.login || ""), avatar_url: String(session.avatar_url || ""), jti };
  } catch (e) {
    return null;
  }
}

async function revokeSession(token, env) {
  const kv = sessionKV(env);
  if (!kv) return;
  const session = await parseSignedSession(token, env);
  if (session && session.jti) await kv.delete(sessionKVKey(String(session.jti)));
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

function diagnosisUpstreamUrl(env, jobId = "") {
  const rawOrigin = String(env.AI_GATEWAY_ORIGIN_URL || "").trim();
  if (!rawOrigin) throw new Error("origin_unavailable");
  const origin = new URL(rawOrigin);
  if (origin.protocol !== "https:") throw new Error("origin_unavailable");
  const basePath = withoutTrailingSlash(origin.pathname);
  const path = "/v1/ai/automation/runs" + (jobId ? "/" + jobId : "");
  origin.pathname = !basePath || basePath === "/" || shouldIgnoreLegacyEndpointBase(basePath, path)
    ? path
    : basePath + path;
  origin.search = jobId ? "" : "?limit=100";
  origin.hash = "";
  return origin.toString();
}

async function fetchDiagnosisUpstream(env, jobId = "") {
  const token = String(env.DASHBOARD_API_TOKEN || "").trim();
  const response = await fetch(diagnosisUpstreamUrl(env, jobId), {
    method: "GET",
    redirect: "manual",
    signal: AbortSignal.timeout(5000),
    headers: {
      Authorization: token ? "Bearer " + token : "",
      Accept: "application/json",
      "User-Agent": "AiGatewayDashboard/1.0",
    },
  });
  if (response.status >= 300 && response.status < 400) throw new Error("upstream_redirect_rejected");
  const declaredLength = Number(response.headers.get("content-length") || 0);
  if (declaredLength > DIAGNOSIS_MAX_RESPONSE_BYTES) throw new Error("upstream_response_too_large");
  const chunks = [];
  let size = 0;
  const reader = response.body?.getReader();
  if (!reader) throw new Error("upstream_response_invalid");
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > DIAGNOSIS_MAX_RESPONSE_BYTES) {
      await reader.cancel();
      throw new Error("upstream_response_too_large");
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let payload = null;
  try {
    payload = JSON.parse(new TextDecoder().decode(bytes));
  } catch (e) {
    throw new Error("upstream_response_invalid");
  }
  if (response.status === 404) return { missing: true, payload: null };
  if (!response.ok || !payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("upstream_unavailable");
  }
  return { missing: false, payload };
}

function isOperationalDiagnosisRun(run, expectedId = "") {
  if (!run || typeof run !== "object" || Array.isArray(run)) return false;
  const metadata = run.metadata;
  const runId = String(run.run_id || "");
  return DIAGNOSIS_JOB_ID_PATTERN.test(runId)
    && (!expectedId || runId === expectedId)
    && DIAGNOSIS_TASKS.has(run.task_name)
    && metadata && typeof metadata === "object" && !Array.isArray(metadata)
    && metadata.origin === "service_job"
    && metadata.source_repository === DIAGNOSIS_SOURCE_REPOSITORY
    && metadata.mode === "review_only";
}

function diagnosisKind(run) {
  return DIAGNOSIS_TASKS.get(run.task_name) || "unknown";
}

function diagnosisListItem(run) {
  const status = String(run.metadata?.diagnosis_status || "unknown");
  return {
    job_id: String(run.run_id),
    status: ["queued", "running", "succeeded", "failed"].includes(status) ? status : "unknown",
    updated_at: Number.isFinite(Number(run.updated_at)) ? Number(run.updated_at) : 0,
    kind: diagnosisKind(run),
  };
}

async function listDiagnoses(env) {
  const upstream = await fetchDiagnosisUpstream(env);
  const runs = upstream.payload?.ledger?.runs;
  if (!Array.isArray(runs)) throw new Error("upstream_response_invalid");
  const diagnoses = runs
    .filter(run => isOperationalDiagnosisRun(run))
    .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0))
    .slice(0, 20)
    .map(diagnosisListItem);
  return json({ status: "ok", diagnoses });
}

async function diagnosisDetail(env, jobId) {
  const upstream = await fetchDiagnosisUpstream(env, jobId);
  if (upstream.missing) {
    return json({ status: "ok", diagnosis: { job_id: jobId, status: "missing", message: "诊断记录已过期或不存在。", advisory: true } });
  }
  const run = upstream.payload?.run;
  if (!isOperationalDiagnosisRun(run, jobId)) return json({ status: "error", error: "diagnosis_not_found" }, 404);
  const metadata = run.metadata;
  const rawStatus = String(metadata.diagnosis_status || "unknown");
  const status = ["queued", "running", "succeeded", "failed"].includes(rawStatus) ? rawStatus : "unknown";
  const diagnosis = {
    job_id: jobId,
    status,
    updated_at: Number.isFinite(Number(run.updated_at)) ? Number(run.updated_at) : 0,
    advisory: true,
    kind: diagnosisKind(run),
  };
  if (status === "succeeded" && typeof metadata.diagnosis_summary === "string" && metadata.diagnosis_summary.trim()) {
    diagnosis.output = metadata.diagnosis_summary.slice(0, 500);
  } else if (status === "queued" || status === "running") {
    diagnosis.message = "诊断尚未完成。";
  } else if (status === "failed" || status === "blocked") {
    diagnosis.message = "诊断未完成，未提供结果摘要。";
  } else {
    diagnosis.message = "诊断摘要尚未提供。";
  }
  return json({ status: "ok", diagnosis });
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
        const sessionToken = await createSession(user, env);
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
      try {
        await revokeSession(getCookie(request, COOKIE_NAME), env);
      } catch (e) {
        return json({ error: "session_revocation_failed" }, 503);
      }
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
      const session = await getSession(token, env);
      if (!session) return json({ error: "unauthorized" }, 401);
      return json({ login: session.login, avatar_url: session.avatar_url });
    }

    // ── Bounded diagnosis read API (requires session) ─────────────
    if (path === "/api/diagnoses" || path.startsWith("/api/diagnoses/")) {
      const token = getCookie(request, COOKIE_NAME);
      const session = await getSession(token, env);
      if (!session) return json({ status: "error", error: "unauthorized" }, 401);
      if (request.method !== "GET") {
        return new Response(null, { status: 405, headers: { Allow: "GET" } });
      }
      try {
        if (path === "/api/diagnoses") return await listDiagnoses(env);
        const jobId = path.slice("/api/diagnoses/".length);
        if (!DIAGNOSIS_JOB_ID_PATTERN.test(jobId)) {
          return json({ status: "error", error: "diagnosis_not_found" }, 404);
        }
        return await diagnosisDetail(env, jobId);
      } catch (e) {
        return json({ status: "error", error: "诊断数据暂不可用" }, 502);
      }
    }

    // ── API proxy (requires session) ───────────────────────────────
    if (path.startsWith("/api/")) {
      const token = getCookie(request, COOKIE_NAME);
      const session = await getSession(token, env);
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
      const session = await getSession(token, env);
      if (!session) return html(LOGIN_HTML);
      return html(DASHBOARD_HTML);
    }

    return new Response("Not found", { status: 404 });
  },
};
