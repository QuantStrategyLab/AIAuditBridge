/** AiGateway Dashboard — Cloudflare Worker that serves an operations dashboard.

  Uses a static token (DASHBOARD_API_TOKEN) to call the AiGateway API.
  Serves HTML at / and proxies API calls at /api/*.

  Deploy alongside the codex-audit-proxy Worker — they share the same VPS origin.
 */

// ── HTML template ──────────────────────────────────────────────────────

const HTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AiGateway · QuantStrategyLab</title>
<style>
  :root {
    --bg: #0a0e14;
    --surface: #131820;
    --surface2: #1a212c;
    --border: #253040;
    --text: #c8d6e5;
    --text2: #6b7d95;
    --text3: #455368;
    --green: #10b981;
    --green-bg: rgba(16,185,129,0.10);
    --amber: #f59e0b;
    --amber-bg: rgba(245,158,11,0.10);
    --red: #ef4444;
    --red-bg: rgba(239,68,68,0.10);
    --blue: #3b82f6;
    --blue-bg: rgba(59,130,246,0.10);
    --purple: #8b5cf6;
    --radius: 12px;
    --shadow: 0 1px 3px rgba(0,0,0,.4), 0 0 0 1px rgba(255,255,255,.03);
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{
    font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:var(--bg);
    color:var(--text);
    padding:24px 32px;
    min-height:100vh;
    -webkit-font-smoothing:antialiased;
  }
  /* Header */
  .header{display:flex;align-items:center;gap:16px;margin-bottom:6px}
  .header .logo{
    width:40px;height:40px;border-radius:10px;
    background:linear-gradient(135deg,var(--blue),var(--purple));
    display:flex;align-items:center;justify-content:center;font-size:20px;
  }
  .header h1{font-size:22px;font-weight:700;letter-spacing:-.3px;color:#fff}
  .status-bar{
    display:flex;align-items:center;gap:20px;margin-bottom:24px;
    font-size:13px;color:var(--text2);
  }
  .status-pill{
    display:inline-flex;align-items:center;gap:6px;
    padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;
  }
  .status-pill.ok{background:var(--green-bg);color:var(--green)}
  .status-pill.warn{background:var(--amber-bg);color:var(--amber)}
  .status-pill.err{background:var(--red-bg);color:var(--red)}
  .pulse{width:8px;height:8px;border-radius:50%;display:inline-block}
  .pulse.ok{background:var(--green);box-shadow:0 0 8px var(--green)}
  .pulse.warn{background:var(--amber);box-shadow:0 0 8px var(--amber)}
  .pulse.err{background:var(--red);box-shadow:0 0 8px var(--red)}

  /* Grid */
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-bottom:16px}

  /* Card */
  .card{
    background:var(--surface);border:1px solid var(--border);
    border-radius:var(--radius);padding:20px;
    box-shadow:var(--shadow);
    transition:border-color .3s;
  }
  .card:hover{border-color:var(--text3)}
  .card-header{display:flex;align-items:center;gap:8px;margin-bottom:16px}
  .card-header .icon{font-size:16px;line-height:1}
  .card-header h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--text2)}

  /* Stats */
  .stat-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.04)}
  .stat-row:last-child{border-bottom:none}
  .stat-label{font-size:13px;color:var(--text2)}
  .stat-value{font-size:13px;font-weight:600;font-variant-numeric:tabular-nums}
  .stat-value.ok{color:var(--green)} .stat-value.warn{color:var(--amber)} .stat-value.err{color:var(--red)} .stat-value.info{color:var(--blue)}

  /* Big number */
  .big-number{font-size:32px;font-weight:800;letter-spacing:-1px;color:#fff;line-height:1}
  .big-label{font-size:12px;color:var(--text2);margin-top:2px}

  /* Progress bar */
  .quota-item{margin-bottom:14px}
  .quota-item:last-child{margin-bottom:0}
  .quota-header{display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px}
  .quota-repo{color:var(--text);font-weight:500}
  .quota-amount{color:var(--text2);font-variant-numeric:tabular-nums}
  .bar-track{height:6px;border-radius:6px;background:var(--surface2);overflow:hidden}
  .bar-fill{height:100%;border-radius:6px;transition:width .6s cubic-bezier(.4,0,.2,1)}
  .bar-fill.ok{background:linear-gradient(90deg,var(--green),#34d399)}
  .bar-fill.warn{background:linear-gradient(90deg,var(--amber),#fbbf24)}
  .bar-fill.err{background:linear-gradient(90deg,var(--red),#f87171)}

  /* Badge */
  .badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;letter-spacing:.3px}
  .badge-ok{background:var(--green-bg);color:var(--green)}
  .badge-warn{background:var(--amber-bg);color:var(--amber)}
  .badge-err{background:var(--red-bg);color:var(--red)}
  .badge-info{background:var(--blue-bg);color:var(--blue)}

  /* Table */
  table{width:100%;font-size:12px;border-collapse:collapse}
  thead th{color:var(--text3);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px;padding:0 0 8px;text-align:left}
  tbody td{padding:8px 0;border-bottom:1px solid rgba(255,255,255,.03);vertical-align:middle}
  tbody tr:last-child td{border-bottom:none}
  .mono{font-family:'JetBrains Mono','SF Mono',monospace;font-size:11px}

  /* Empty */
  .empty{text-align:center;padding:20px;color:var(--text3);font-size:13px}
  .empty .icon{font-size:28px;margin-bottom:6px;opacity:.5}

  /* Skeleton */
  @keyframes shimmer{0%{background-position:-200px 0}100%{background-position:calc(200px + 100%) 0}}
  .skeleton{background:linear-gradient(90deg,var(--surface2) 25%,var(--border) 50%,var(--surface2) 75%);background-size:200px 100%;animation:shimmer 1.5s infinite;border-radius:4px}
  .sk-row{height:14px;margin-bottom:8px}
  .sk-row:last-child{margin-bottom:0}

  /* Footer */
  .footer{text-align:right;font-size:11px;color:var(--text3);margin-top:8px}

  /* Error toast */
  .toast{
    position:fixed;top:16px;right:16px;max-width:400px;
    background:var(--red-bg);border:1px solid var(--red);color:var(--red);
    padding:12px 16px;border-radius:var(--radius);font-size:13px;
    display:none;z-index:100;animation:slideIn .3s ease;
    box-shadow:0 4px 20px rgba(239,68,68,.15);
  }
  @keyframes slideIn{from{transform:translateX(20px);opacity:0}to{transform:translateX(0);opacity:1}}

  /* Responsive */
  @media(max-width:768px){body{padding:16px}.grid{grid-template-columns:1fr}.big-number{font-size:24px}}
</style>
</head>
<body>

<div class="toast" id="toast"></div>

<!-- Header -->
<div class="header">
  <div class="logo">⚡</div>
  <div>
    <h1>AiGateway</h1>
  </div>
</div>
<div class="status-bar">
  QuantStrategyLab · <span id="time"></span>
  <span class="status-pill ok" id="status-pill"><span class="pulse ok" id="pulse"></span><span id="status-text">连接中…</span></span>
</div>

<!-- Grid -->
<div class="grid">
  <!-- Health -->
  <div class="card">
    <div class="card-header"><span class="icon">📊</span><h2>服务健康</h2></div>
    <div id="health"><div class="skeleton sk-row"></div><div class="skeleton sk-row"></div><div class="skeleton sk-row"></div></div>
  </div>

  <!-- Quota -->
  <div class="card">
    <div class="card-header"><span class="icon">💰</span><h2>配额消耗</h2></div>
    <div id="quota"><div class="skeleton sk-row"></div><div class="skeleton sk-row"></div></div>
  </div>

  <!-- Effectiveness -->
  <div class="card">
    <div class="card-header"><span class="icon">📈</span><h2>有效性 · 90 天</h2></div>
    <div id="effectiveness"><div class="skeleton sk-row"></div></div>
  </div>

  <!-- Shadow -->
  <div class="card">
    <div class="card-header"><span class="icon">🔍</span><h2>影子审计分歧</h2></div>
    <div id="shadow"><div class="skeleton sk-row"></div></div>
  </div>

  <!-- Changes -->
  <div class="card" style="grid-column:1/-1">
    <div class="card-header"><span class="icon">⚡</span><h2>最近变更 · 7 天</h2></div>
    <div id="changes"><div class="skeleton sk-row"></div></div>
  </div>
</div>

<div class="footer">自动刷新 · 30s · 上次: <span id="last-refresh">—</span></div>

<script>
const API = "/api";
let refreshTimer;

function fmtUSD(n){return"$"+(n||0).toFixed(2)}
function fmtPct(n){return((n||0)*100).toFixed(0)+"%"}
function fmtNum(n){return(n||0).toLocaleString()}
function t(tag,attrs,children){const el=document.createElement(tag);if(attrs)Object.entries(attrs).forEach(([k,v])=>{if(k==="cls")el.className=v;else if(k==="txt")el.textContent=v;else el.setAttribute(k,v)});if(children)el.innerHTML=children;return el}

function showToast(msg){const e=document.getElementById("toast");e.textContent=msg;e.style.display="block";setTimeout(()=>e.style.display="none",5000)}

function setStatus(st){
  const pill=document.getElementById("status-pill");
  const pulse=document.getElementById("pulse");
  const txt=document.getElementById("status-text");
  pill.className="status-pill "+(st==="healthy"?"ok":st==="degraded"?"warn":"err");
  pulse.className="pulse "+(st==="healthy"?"ok":st==="degraded"?"warn":"err");
  txt.textContent=st;
}

async function fetchJSON(path){
  const resp=await fetch(API+path);
  if(!resp.ok)throw new Error(path+": "+resp.status);
  return resp.json()
}

async function refresh(){
  try{
    const[health,quota,eff,shadow,changes]=await Promise.all([
      fetchJSON("/v1/ai/health"),
      fetchJSON("/v1/ai/quota"),
      fetchJSON("/v1/ai/changes/effectiveness?days=90"),
      fetchJSON("/v1/ai/feedback/shadow"),
      fetchJSON("/v1/ai/changes?days=7"),
    ]);
    renderHealth(health);
    renderQuota(quota.quota||quota);
    renderEffectiveness(eff.report||eff);
    renderShadow(shadow.disagreements||[]);
    renderChanges((changes.changes||[]).slice(0,15));
    document.getElementById("last-refresh").textContent=new Date().toLocaleTimeString();
    document.getElementById("time").textContent=new Date().toLocaleString();
  }catch(e){showToast("API 错误: "+e.message)}
}

function renderHealth(h){
  setStatus(h.status||"unknown");
  const st=h.status||"unknown";
  let html='<div class="big-number">'+Math.round((h.uptime_seconds||0)/3600)+'<span style="font-size:16px;font-weight:400;color:var(--text2)"> h</span></div><div class="big-label">运行时间</div>';
  html+='<div style="margin-top:14px">';
  if(!(h.endpoints||[]).length){html+='<div class="empty"><div class="icon">📡</div>暂无流量</div>'}
  else for(const ep of(h.endpoints||[])){
    const cls=ep.error_rate>0.1?"err":ep.error_rate>0.02?"warn":"ok";
    const shortPath=ep.path.replace("/v1/ai/","/").replace("/v1/codex-audit","/codex");
    html+='<div class="stat-row"><span class="stat-label">'+shortPath+'</span><span class="stat-value '+cls+'">'+fmtNum(ep.total)+' req · p95 '+ep.p95_ms+'ms · err '+fmtPct(ep.error_rate)+'</span></div>';
  }
  html+='</div>';
  document.getElementById("health").innerHTML=html
}

function renderQuota(q){
  const repos=q.repos||{};
  if(Object.keys(repos).length===0){document.getElementById("quota").innerHTML='<div class="empty"><div class="icon">💳</div>暂无用量</div>';return}
  let html='';
  for(const[repo,r]of Object.entries(repos)){
    const pct=r.daily_budget?(r.total_cost_usd||0)/r.daily_budget*100:0;
    const cls=pct>80?"err":pct>50?"warn":"ok";
    html+='<div class="quota-item"><div class="quota-header"><span class="quota-repo">'+(repo.split("/")[1]||repo)+'</span><span class="quota-amount">'+fmtUSD(r.total_cost_usd)+' / '+fmtUSD(r.daily_budget)+'</span></div><div class="bar-track"><div class="bar-fill '+cls+'" style="width:'+Math.min(pct,100)+'%"></div></div></div>';
  }
  document.getElementById("quota").innerHTML=html
}

function renderEffectiveness(e){
  const rate=e.improvement_rate||0;
  let html='<div style="display:flex;gap:24px;margin-bottom:16px">';
  html+='<div><div class="big-number" style="color:'+(rate>0.7?'var(--green)':'var(--amber)')+'">'+fmtPct(rate)+'</div><div class="big-label">成功率</div></div>';
  html+='<div><div class="big-number">'+(e.evaluated||0)+'</div><div class="big-label">已评估</div></div>';
  html+='</div>';
  html+='<div class="stat-row"><span class="stat-label">📈 改善</span><span class="stat-value ok">'+(e.improved||0)+'</span></div>';
  html+='<div class="stat-row"><span class="stat-label">📉 退化</span><span class="stat-value err">'+(e.degraded||0)+'</span></div>';
  html+='<div class="stat-row"><span class="stat-label">➖ 持平</span><span class="stat-value">'+(e.neutral||0)+'</span></div>';
  html+='<div class="stat-row"><span class="stat-label">⏳ 待评估</span><span class="stat-value info">'+(e.pending||0)+'</span></div>';
  document.getElementById("effectiveness").innerHTML=html
}

function renderShadow(items){
  if(!items.length){document.getElementById("shadow").innerHTML='<div class="empty"><div class="icon">✅</div>无活跃分歧</div>';return}
  let html='';
  for(const d of items){
    html+='<div class="stat-row"><span class="stat-label">'+d.plugin+'</span><span class="stat-value err"><span class="badge badge-err">'+d.disagreement_count+'x</span> '+(d.ai_verdict||"")+'</span></div>';
  }
  document.getElementById("shadow").innerHTML=html
}

function renderChanges(items){
  if(!items.length){document.getElementById("changes").innerHTML='<div class="empty"><div class="icon">📋</div>窗口内无变更</div>';return}
  let html='<table><thead><tr><th>仓库</th><th>操作</th><th>效果</th><th>置信度</th><th>时间</th></tr></thead><tbody>';
  for(const c of items){
    const eff=c.effect||"pending";
    const effCls=eff==="improved"?"ok":eff==="degraded"?"err":"info";
    const actionCls=c.action==="auto_merge"?"ok":c.action==="auto_pr"?"info":"warn";
    const dt=c.created_at?new Date(c.created_at*1000).toLocaleDateString():"—";
    html+='<tr><td><span class="mono">'+(c.repo||"").split("/")[1]+'</span></td><td><span class="badge badge-'+actionCls+'">'+c.action+'</span></td><td class="stat-value '+effCls+'">'+eff+'</td><td class="mono">'+fmtPct(c.confidence||0)+'</td><td class="mono" style="color:var(--text3)">'+dt+'</td></tr>';
  }
  html+='</tbody></table>';
  document.getElementById("changes").innerHTML=html
}

document.getElementById("time").textContent=new Date().toLocaleString();
refresh();
refreshTimer=setInterval(refresh,30000);
</script>
</body>
</html>`;

// ── Login page ────────────────────────────────────────────────────────

const LOGIN_HTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AiGateway · 登录</title>
<style>
  :root{--bg:#0a0e14;--surface:#131820;--border:#253040;--text:#c8d6e5;--blue:#3b82f6;--purple:#8b5cf6;--radius:12px}
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);color:var(--text);display:flex;align-items:center;justify-content:center;min-height:100vh;-webkit-font-smoothing:antialiased}
  .login-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:40px;width:380px;max-width:90vw;box-shadow:0 4px 24px rgba(0,0,0,.5)}
  .logo{width:48px;height:48px;border-radius:12px;background:linear-gradient(135deg,var(--blue),var(--purple));display:flex;align-items:center;justify-content:center;font-size:24px;margin:0 auto 16px}
  h1{font-size:20px;font-weight:700;text-align:center;margin-bottom:4px;color:#fff}
  .sub{font-size:13px;color:#6b7d95;text-align:center;margin-bottom:24px}
  input{width:100%;padding:10px 14px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;outline:none;transition:border-color .3s}
  input:focus{border-color:var(--blue)}
  button{width:100%;padding:10px;margin-top:12px;background:linear-gradient(135deg,var(--blue),var(--purple));border:none;border-radius:8px;color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .3s}
  button:hover{opacity:.9}
  .err{color:#ef4444;font-size:12px;text-align:center;margin-top:8px;display:none}
</style></head>
<body>
<div class="login-box">
  <div class="logo">⚡</div>
  <h1>AiGateway</h1>
  <div class="sub">QuantStrategyLab · 运维面板</div>
  <input type="password" id="key" placeholder="输入访问密钥" autofocus>
  <button onclick="login()">登录</button>
  <div class="err" id="err">密钥错误</div>
</div>
<script>
async function login(){
  const key = document.getElementById("key").value;
  if(!key) return;
  const resp = await fetch("/api/v1/ai/health",{headers:{"Authorization":"Bearer "+key}});
  if(resp.ok){
    document.cookie = "dash_key="+key+";path=/;max-age=86400;SameSite=Strict";
    location.href = "/";
  } else {
    document.getElementById("err").style.display = "block";
  }
}
document.getElementById("key").addEventListener("keydown",e=>{if(e.key==="Enter")login()});
</script>
</body></html>`;

// ── Auth helper ────────────────────────────────────────────────────────

function getAuthToken(request) {
  // Cookie-based auth (after login)
  const cookie = request.headers.get("Cookie") || "";
  const match = cookie.match(/dash_key=([^;]+)/);
  if (match) return match[1];
  // URL-based auth (?key=xxx)
  const url = new URL(request.url);
  return url.searchParams.get("key") || "";
}

// ── API proxy ──────────────────────────────────────────────────────────

async function proxyAPI(path, env, request) {
  const origin = (env.AI_GATEWAY_ORIGIN_URL || "").trim();
  if (!origin) throw new Error("AI_GATEWAY_ORIGIN_URL not configured");

  // Use user-provided token (login) or Worker's own static token (server-side)
  const userToken = getAuthToken(request);
  const token = userToken || (env.DASHBOARD_API_TOKEN || "").trim();
  const url = origin.replace(/\/+$/, "") + path;

  const resp = await fetch(url, {
    headers: {
      "Authorization": token ? "Bearer " + token : "",
      "Accept": "application/json",
      "User-Agent": "AiGatewayDashboard/1.0",
    },
  });

  const body = await resp.text();
  return new Response(body, {
    status: resp.status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

// ── main ───────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const userToken = getAuthToken(request);

    // API proxy: /api/* → VPS (requires auth)
    if (url.pathname.startsWith("/api/")) {
      if (!userToken) {
        return new Response(JSON.stringify({status:"error",error:"unauthorized"}), {
          status: 401,
          headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
        });
      }
      const apiPath = url.pathname.slice(4);
      try {
        return await proxyAPI(apiPath, env, request);
      } catch (e) {
        return new Response(JSON.stringify({ status: "error", error: e.message }), {
          status: 502,
          headers: { "Content-Type": "application/json" },
        });
      }
    }

    // Dashboard HTML — requires auth
    if (url.pathname === "/" || url.pathname === "") {
      if (!userToken) return new Response(LOGIN_HTML, {
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      });
      return new Response(HTML, {
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      });
    }

    return new Response("Not found", { status: 404 });
  },
};
