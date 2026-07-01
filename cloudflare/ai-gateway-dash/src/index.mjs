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
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AiGateway · 登录</title>
<style>
  :root{--bg:#0a0e14;--surface:#131820;--border:#253040;--text:#c8d6e5;--text2:#6b7d95;--blue:#3b82f6;--purple:#8b5cf6;--radius:12px}
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);color:var(--text);display:flex;align-items:center;justify-content:center;min-height:100vh;-webkit-font-smoothing:antialiased}
  .box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:40px;width:400px;max-width:90vw;box-shadow:0 4px 24px rgba(0,0,0,.5);text-align:center}
  .logo{width:56px;height:56px;border-radius:14px;background:linear-gradient(135deg,var(--blue),var(--purple));display:flex;align-items:center;justify-content:center;font-size:28px;margin:0 auto 16px}
  h1{font-size:22px;font-weight:700;color:#fff;margin-bottom:4px}
  .sub{font-size:14px;color:var(--text2);margin-bottom:28px}
  .btn{
    display:inline-flex;align-items:center;gap:10px;
    padding:12px 28px;background:#24292f;border:1px solid #454b54;border-radius:8px;
    color:#fff;font-size:14px;font-weight:600;text-decoration:none;
    transition:all .2s;cursor:pointer;
  }
  .btn:hover{background:#2c333b;border-color:#6e7681}
  .btn svg{width:20px;height:20px;fill:#fff}
  .footer{margin-top:24px;font-size:12px;color:var(--text2)}
  .err{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);color:#ef4444;padding:10px;border-radius:8px;margin-bottom:16px;font-size:13px;display:none}
</style></head>
<body>
<div class="box">
  <div class="logo">⚡</div>
  <h1>AiGateway</h1>
  <div class="sub">QuantStrategyLab · 运维面板<br>GitHub 组织成员登录</div>
  <div class="err" id="err"></div>
  <a href="/login" class="btn">
    <svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>
    使用 GitHub 登录
  </a>
  <div class="footer">仅限 QuantStrategyLab 组织成员</div>
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
  :root {
    --bg: #0a0e14; --surface: #131820; --surface2: #1a212c; --border: #253040;
    --text: #c8d6e5; --text2: #6b7d95; --text3: #455368;
    --green: #10b981; --green-bg: rgba(16,185,129,0.10);
    --amber: #f59e0b; --amber-bg: rgba(245,158,11,0.10);
    --red: #ef4444; --red-bg: rgba(239,68,68,0.10);
    --blue: #3b82f6; --blue-bg: rgba(59,130,246,0.10);
    --purple: #8b5cf6;
    --radius: 12px; --shadow: 0 1px 3px rgba(0,0,0,.4), 0 0 0 1px rgba(255,255,255,.03);
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);padding:24px 32px;min-height:100vh;-webkit-font-smoothing:antialiased}
  .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
  .header-left{display:flex;align-items:center;gap:16px}
  .logo{width:40px;height:40px;border-radius:10px;background:linear-gradient(135deg,var(--blue),var(--purple));display:flex;align-items:center;justify-content:center;font-size:20px}
  .header h1{font-size:22px;font-weight:700;letter-spacing:-.3px;color:#fff}
  .user{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text2)}
  .user img{width:24px;height:24px;border-radius:50%}
  .logout{color:var(--text3);text-decoration:none;font-size:12px;margin-left:4px}
  .logout:hover{color:var(--text2)}
  .status-bar{display:flex;align-items:center;gap:20px;margin-bottom:24px;font-size:13px;color:var(--text2)}
  .status-pill{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600}
  .status-pill.ok{background:var(--green-bg);color:var(--green)} .status-pill.warn{background:var(--amber-bg);color:var(--amber)} .status-pill.err{background:var(--red-bg);color:var(--red)}
  .pulse{width:8px;height:8px;border-radius:50%;display:inline-block}
  .pulse.ok{background:var(--green);box-shadow:0 0 8px var(--green)} .pulse.warn{background:var(--amber);box-shadow:0 0 8px var(--amber)} .pulse.err{background:var(--red);box-shadow:0 0 8px var(--red)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-bottom:16px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;box-shadow:var(--shadow);transition:border-color .3s}
  .card:hover{border-color:var(--text3)}
  .card-header{display:flex;align-items:center;gap:8px;margin-bottom:16px}
  .card-header .icon{font-size:16px} .card-header h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--text2)}
  .stat-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:13px}
  .stat-row:last-child{border-bottom:none} .stat-label{color:var(--text2)} .stat-value{font-weight:600;font-variant-numeric:tabular-nums}
  .stat-value.ok{color:var(--green)} .stat-value.warn{color:var(--amber)} .stat-value.err{color:var(--red)} .stat-value.info{color:var(--blue)}
  .big-number{font-size:32px;font-weight:800;letter-spacing:-1px;color:#fff;line-height:1} .big-label{font-size:12px;color:var(--text2);margin-top:2px}
  .quota-item{margin-bottom:14px}.quota-item:last-child{margin-bottom:0}
  .quota-header{display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px}.quota-repo{color:var(--text);font-weight:500}.quota-amount{color:var(--text2);font-variant-numeric:tabular-nums}
  .bar-track{height:6px;border-radius:6px;background:var(--surface2);overflow:hidden}
  .bar-fill{height:100%;border-radius:6px;transition:width .6s cubic-bezier(.4,0,.2,1)}
  .bar-fill.ok{background:linear-gradient(90deg,var(--green),#34d399)} .bar-fill.warn{background:linear-gradient(90deg,var(--amber),#fbbf24)} .bar-fill.err{background:linear-gradient(90deg,var(--red),#f87171)}
  .badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;letter-spacing:.3px}
  .badge-ok{background:var(--green-bg);color:var(--green)} .badge-warn{background:var(--amber-bg);color:var(--amber)} .badge-err{background:var(--red-bg);color:var(--red)} .badge-info{background:var(--blue-bg);color:var(--blue)}
  table{width:100%;font-size:12px;border-collapse:collapse}
  thead th{color:var(--text3);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px;padding:0 0 8px;text-align:left}
  tbody td{padding:8px 0;border-bottom:1px solid rgba(255,255,255,.03);vertical-align:middle}
  tbody tr:last-child td{border-bottom:none} .mono{font-family:'JetBrains Mono','SF Mono',monospace;font-size:11px}
  .empty{text-align:center;padding:20px;color:var(--text3);font-size:13px}.empty .icon{font-size:28px;margin-bottom:6px;opacity:.5}
  @keyframes shimmer{0%{background-position:-200px 0}100%{background-position:calc(200px + 100%) 0}}
  .skeleton{background:linear-gradient(90deg,var(--surface2) 25%,var(--border) 50%,var(--surface2) 75%);background-size:200px 100%;animation:shimmer 1.5s infinite;border-radius:4px}
  .sk-row{height:14px;margin-bottom:8px}.sk-row:last-child{margin-bottom:0}
  .footer{text-align:right;font-size:11px;color:var(--text3);margin-top:8px}
  .toast{position:fixed;top:16px;right:16px;max-width:400px;background:var(--red-bg);border:1px solid var(--red);color:var(--red);padding:12px 16px;border-radius:var(--radius);font-size:13px;display:none;z-index:100;animation:slideIn .3s ease;box-shadow:0 4px 20px rgba(239,68,68,.15)}
  @keyframes slideIn{from{transform:translateX(20px);opacity:0}to{transform:translateX(0);opacity:1}}
  @media(max-width:768px){body{padding:16px}.grid{grid-template-columns:1fr}.big-number{font-size:24px}}
</style>
</head>
<body>
<div class="toast" id="toast"></div>
<div class="header">
  <div class="header-left">
    <div class="logo">⚡</div>
    <h1>AiGateway</h1>
  </div>
  <div class="user">
    <img id="avatar" src="" alt="">
    <span id="username"></span>
    <a href="/logout" class="logout">退出</a>
  </div>
</div>
<div class="status-bar">
  QuantStrategyLab · <span id="time"></span>
  <span class="status-pill ok" id="status-pill"><span class="pulse ok" id="pulse"></span><span id="status-text">连接中…</span></span>
</div>
<div class="grid">
  <div class="card"><div class="card-header"><span class="icon">📊</span><h2>服务健康</h2></div><div id="health"><div class="skeleton sk-row"></div><div class="skeleton sk-row"></div></div></div>
  <div class="card"><div class="card-header"><span class="icon">💰</span><h2>配额消耗</h2></div><div id="quota"><div class="skeleton sk-row"></div></div></div>
  <div class="card"><div class="card-header"><span class="icon">📈</span><h2>有效性 · 90 天</h2></div><div id="effectiveness"><div class="skeleton sk-row"></div></div></div>
  <div class="card"><div class="card-header"><span class="icon">🔍</span><h2>影子审计分歧</h2></div><div id="shadow"><div class="skeleton sk-row"></div></div></div>
  <div class="card" style="grid-column:1/-1"><div class="card-header"><span class="icon">⚡</span><h2>最近变更 · 7 天</h2></div><div id="changes"><div class="skeleton sk-row"></div></div></div>
</div>
<div class="footer">自动刷新 · 30s · 上次: <span id="last-refresh">—</span></div>
	<script>
	const API="/api";let refreshTimer;
	function fmtUSD(n){return "$"+(Number(n)||0).toFixed(2)}
	function fmtPct(n){return ((Number(n)||0)*100).toFixed(0)+"%"}
	function fmtNum(n){return (Number(n)||0).toLocaleString()}
	function clsStatus(st){return st==="healthy"?"ok":st==="degraded"?"warn":"err"}
	function clear(el){el.replaceChildren()}
	function el(tag, className, text){const n=document.createElement(tag);if(className)n.className=className;if(text!==undefined)n.textContent=String(text);return n}
	function append(parent){for(let i=1;i<arguments.length;i++)parent.appendChild(arguments[i]);return parent}
	function showToast(msg){const e=document.getElementById("toast");e.textContent=msg;e.style.display="block";setTimeout(()=>e.style.display="none",5000)}
	function setStatus(st){const c=clsStatus(st),p=document.getElementById("status-pill"),pl=document.getElementById("pulse"),t=document.getElementById("status-text");p.className="status-pill "+c;pl.className="pulse "+c;t.textContent=st}
	function empty(target, icon, text){const box=el("div","empty");append(box,el("div","icon",icon),document.createTextNode(text));clear(target);target.appendChild(box)}
	function statRow(label,value,valueClass){const row=el("div","stat-row");append(row,el("span","stat-label",label),el("span","stat-value "+(valueClass||""),value));return row}
	function badge(kind,text){return el("span","badge badge-"+kind,text)}
	async function fetchJSON(path){const r=await fetch(API+path);if(!r.ok)throw new Error(path+": "+r.status);return r.json()}
	async function refresh(){try{const[h,q,e,s,c]=await Promise.all([fetchJSON("/v1/ai/health"),fetchJSON("/v1/ai/quota"),fetchJSON("/v1/ai/changes/effectiveness?days=90"),fetchJSON("/v1/ai/feedback/shadow"),fetchJSON("/v1/ai/changes?days=7")]);renderHealth(h);renderQuota(q.quota||q);renderEffectiveness(e.report||e);renderShadow(s.disagreements||[]);renderChanges((c.changes||[]).slice(0,15));document.getElementById("last-refresh").textContent=new Date().toLocaleTimeString();document.getElementById("time").textContent=new Date().toLocaleString()}catch(e){showToast("API: "+e.message)}}
	function renderHealth(h){setStatus(h.status||"unknown");const target=document.getElementById("health");clear(target);const hours=el("div","big-number",Math.round((h.uptime_seconds||0)/3600));const unit=el("span","");unit.style.fontSize="16px";unit.style.fontWeight="400";unit.style.color="var(--text2)";unit.textContent=" h";hours.appendChild(unit);append(target,hours,el("div","big-label","运行时间"));const rows=el("div","");rows.style.marginTop="14px";if(!(h.endpoints||[]).length){append(rows,append(el("div","empty"),el("div","icon","📡"),document.createTextNode("暂无流量")))}else{for(const item of h.endpoints||[]){const c=item.error_rate>0.1?"err":item.error_rate>0.02?"warn":"ok";const path=String(item.path||"").replace("/v1/ai/","/");rows.appendChild(statRow(path,fmtNum(item.total)+" req · p95 "+fmtNum(item.p95_ms)+"ms · err "+fmtPct(item.error_rate),c))}}target.appendChild(rows)}
	function renderQuota(q){const target=document.getElementById("quota"),repos=q.repos||{};clear(target);if(!Object.keys(repos).length){empty(target,"💳","暂无用量");return}for(const name of Object.keys(repos)){const d=repos[name]||{},p=d.daily_budget?(Number(d.total_cost_usd)||0)/(Number(d.daily_budget)||1)*100:0,c=p>80?"err":p>50?"warn":"ok";const item=el("div","quota-item"),head=el("div","quota-header"),track=el("div","bar-track"),bar=el("div","bar-fill "+c);bar.style.width=Math.max(0,Math.min(p,100))+"%";append(head,el("span","quota-repo",String(name).split("/")[1]||name),el("span","quota-amount",fmtUSD(d.total_cost_usd)+" / "+fmtUSD(d.daily_budget)));append(track,bar);append(item,head,track);target.appendChild(item)}}
	function renderEffectiveness(e){const target=document.getElementById("effectiveness"),rate=Number(e.improvement_rate)||0;clear(target);const top=el("div","");top.style.display="flex";top.style.gap="24px";top.style.marginBottom="16px";const success=el("div",""),rateNode=el("div","big-number",fmtPct(rate));rateNode.style.color=rate>0.7?"var(--green)":"var(--amber)";append(success,rateNode,el("div","big-label","成功率"));const evaluated=append(el("div",""),el("div","big-number",e.evaluated||0),el("div","big-label","已评估"));append(top,success,evaluated);append(target,top,statRow("📈 改善",e.improved||0,"ok"),statRow("📉 退化",e.degraded||0,"err"),statRow("➖ 持平",e.neutral||0,""),statRow("⏳ 待评估",e.pending||0,"info"))}
	function renderShadow(items){const target=document.getElementById("shadow");clear(target);if(!items.length){empty(target,"✅","无活跃分歧");return}for(const d of items){const row=el("div","stat-row"),label=el("span","stat-label",d.plugin||""),value=el("span","stat-value err");append(value,badge("err",(d.disagreement_count||0)+"x"),document.createTextNode(" "+(d.ai_verdict||"")));append(row,label,value);target.appendChild(row)}}
	function renderChanges(items){const target=document.getElementById("changes");clear(target);if(!items.length){empty(target,"📋","窗口内无变更");return}const table=el("table",""),thead=el("thead",""),tr=el("tr","");for(const h of ["仓库","操作","效果","置信度","时间"])tr.appendChild(el("th","",h));thead.appendChild(tr);const tbody=el("tbody","");for(const c of items){const effect=c.effect||"pending",effectClass=effect==="improved"?"ok":effect==="degraded"?"err":"info",actionClass=c.action==="auto_merge"?"ok":c.action==="auto_pr"?"info":"warn",dt=c.created_at?new Date(c.created_at*1000).toLocaleDateString():"—",row=el("tr","");append(row,append(el("td",""),el("span","mono",String(c.repo||"").split("/")[1]||"")),append(el("td",""),badge(actionClass,c.action||"")),el("td","stat-value "+effectClass,effect),el("td","mono",fmtPct(c.confidence||0)),el("td","mono",dt));row.lastChild.style.color="var(--text3)";tbody.appendChild(row)}append(table,thead,tbody);target.appendChild(table)}
	function safeAvatarUrl(value){try{const u=new URL(value);return u.protocol==="https:"?u.toString():""}catch(e){return ""}}
	async function loadUser(){try{const r=await fetch("/api/user");if(r.ok){const u=await r.json();document.getElementById("username").textContent=u.login||"";document.getElementById("avatar").src=safeAvatarUrl(u.avatar_url)}}catch(e){}}
	document.getElementById("time").textContent=new Date().toLocaleString();loadUser();refresh();refreshTimer=setInterval(refresh,30000);
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

export function buildDashboardApiUrl(rawOrigin, pathname, search = "") {
  if (!rawOrigin || !rawOrigin.trim()) throw new Error("AI_GATEWAY_ORIGIN_URL not configured");
  const clean = allowedDashboardApiPath(pathname);
  if (!clean) throw new Error("dashboard API route is not allowed");
  const origin = new URL(rawOrigin.trim());
  if (origin.protocol !== "https:") throw new Error("AI_GATEWAY_ORIGIN_URL must use HTTPS");
  const basePath = withoutTrailingSlash(origin.pathname);
  origin.pathname = !basePath || basePath === "/" ? clean : basePath + clean;
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
