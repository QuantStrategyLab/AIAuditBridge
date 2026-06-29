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
<title>AiGateway Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px}
h1{font-size:20px;margin-bottom:4px}
.sub{color:#8b949e;font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.card h2{font-size:14px;color:#8b949e;margin-bottom:10px;text-transform:uppercase;letter-spacing:.5px}
.stat{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #21262d;font-size:13px}
.stat:last-child{border-bottom:none}
.stat .label{color:#8b949e}
.stat .value{font-weight:600;font-variant-numeric:tabular-nums}
.ok{color:#3fb950} .warn{color:#d29922} .err{color:#f85149} .info{color:#58a6ff}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
.badge-ok{background:#1b3a1b;color:#3fb950}
.badge-warn{background:#3a2f1b;color:#d29922}
.badge-err{background:#3a1b1b;color:#f85149}
.bar{height:6px;border-radius:3px;background:#21262d;margin-top:4px}
.bar-fill{height:100%;border-radius:3px;transition:width .5s}
.bar-ok{background:#3fb950} .bar-warn{background:#d29922} .bar-err{background:#f85149}
table{width:100%;font-size:12px;border-collapse:collapse}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:500}
.refresh{color:#8b949e;font-size:11px;text-align:right;margin-top:8px}
.error-msg{background:#3a1b1b;color:#f85149;padding:10px;border-radius:6px;margin-bottom:16px;display:none}
</style>
</head>
<body>
<h1>🤖 AiGateway Operations</h1>
<div class="sub">QuantStrategyLab · <span id="time"></span> · <span id="status-dot" class="ok">●</span> <span id="status-text">loading</span></div>
<div class="error-msg" id="error"></div>

<div class="grid">
  <div class="card">
    <h2>📊 Health</h2>
    <div id="health"></div>
  </div>
  <div class="card">
    <h2>💰 Quota</h2>
    <div id="quota"></div>
  </div>
  <div class="card">
    <h2>📈 Effectiveness (90d)</h2>
    <div id="effectiveness"></div>
  </div>
  <div class="card">
    <h2>🔍 Shadow Disagreements</h2>
    <div id="shadow"></div>
  </div>
  <div class="card">
    <h2>⚡ Recent Changes (7d)</h2>
    <div id="changes"></div>
  </div>
</div>
<div class="refresh">Auto-refresh: 30s · Last: <span id="last-refresh">—</span></div>

<script>
const API = "/api";
let refreshTimer;

async function fetchJSON(path) {
  const resp = await fetch(API + path);
  if (!resp.ok) throw new Error(path + ": " + resp.status);
  return resp.json();
}

function fmtUSD(n) { return "$" + (n||0).toFixed(2); }
function fmtPct(n) { return ((n||0)*100).toFixed(0) + "%"; }
function fmtNum(n) { return (n||0).toLocaleString(); }
function fmtTime(ts) { return new Date(ts*1000).toLocaleTimeString(); }

async function refresh() {
  document.getElementById("error").style.display = "none";
  try {
    const [health, quota, eff, shadow, changes] = await Promise.all([
      fetchJSON("/v1/ai/health"),
      fetchJSON("/v1/ai/quota"),
      fetchJSON("/v1/ai/changes/effectiveness?days=90"),
      fetchJSON("/v1/ai/feedback/shadow"),
      fetchJSON("/v1/ai/changes?days=7"),
    ]);
    renderHealth(health);
    renderQuota(quota.quota || quota);
    renderEffectiveness(eff.report || eff);
    renderShadow(shadow.disagreements || []);
    renderChanges((changes.changes || []).slice(0, 10));
    document.getElementById("last-refresh").textContent = new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById("error").style.display = "block";
    document.getElementById("error").textContent = "API error: " + e.message;
  }
}

function renderHealth(h) {
  const st = h.status || "unknown";
  const dot = document.getElementById("status-dot");
  const txt = document.getElementById("status-text");
  dot.className = st === "healthy" ? "ok" : st === "degraded" ? "warn" : "err";
  txt.textContent = st + " · uptime " + Math.round((h.uptime_seconds||0)/3600) + "h";
  txt.className = st === "healthy" ? "ok" : st === "degraded" ? "warn" : "err";

  let html = "";
  for (const ep of (h.endpoints||[])) {
    const cls = ep.error_rate > 0.1 ? "err" : ep.error_rate > 0.02 ? "warn" : "ok";
    html += '<div class="stat"><span class="label">' + ep.path + '</span><span class="value ' + cls + '">'
      + fmtNum(ep.total) + ' req · p95=' + ep.p95_ms + 'ms · err=' + fmtPct(ep.error_rate) + '</span></div>';
  }
  if (!(h.endpoints||[]).length) html = '<div class="stat"><span class="label">No traffic yet</span></div>';
  document.getElementById("health").innerHTML = html;
}

function renderQuota(q) {
  const repos = q.repos || {};
  if (Object.keys(repos).length === 0) {
    document.getElementById("quota").innerHTML = '<div class="stat"><span class="label">No usage yet</span></div>';
    return;
  }
  let html = "";
  for (const [repo, r] of Object.entries(repos)) {
    const pct = r.daily_budget ? (r.total_cost_usd||0) / r.daily_budget * 100 : 0;
    const cls = pct > 80 ? "err" : pct > 50 ? "warn" : "ok";
    html += '<div class="stat"><span class="label">' + repo.split("/")[1] + '</span>'
      + '<span class="value ' + cls + '">' + fmtUSD(r.total_cost_usd) + ' / ' + fmtUSD(r.daily_budget) + '</span></div>';
    html += '<div class="bar"><div class="bar-fill bar-' + cls + '" style="width:' + Math.min(pct,100) + '%"></div></div>';
  }
  document.getElementById("quota").innerHTML = html;
}

function renderEffectiveness(e) {
  const rate = e.improvement_rate || 0;
  let html = '<div class="stat"><span class="label">Changes evaluated</span><span class="value">' + e.evaluated + ' / ' + e.total_changes + '</span></div>';
  html += '<div class="stat"><span class="label">Improved</span><span class="value ok">' + (e.improved||0) + '</span></div>';
  html += '<div class="stat"><span class="label">Degraded</span><span class="value err">' + (e.degraded||0) + '</span></div>';
  html += '<div class="stat"><span class="label">Neutral</span><span class="value">' + (e.neutral||0) + '</span></div>';
  html += '<div class="stat"><span class="label">Success rate</span><span class="value ' + (rate>0.7?"ok":"warn") + '">' + fmtPct(rate) + '</span></div>';
  document.getElementById("effectiveness").innerHTML = html;
}

function renderShadow(items) {
  if (!items.length) {
    document.getElementById("shadow").innerHTML = '<div class="stat"><span class="label ok">No active disagreements ✓</span></div>';
    return;
  }
  let html = "";
  for (const d of items) {
    html += '<div class="stat"><span class="label">' + d.plugin + '</span><span class="value err">'
      + d.disagreement_count + 'x · ' + (d.ai_verdict||"") + '</span></div>';
  }
  document.getElementById("shadow").innerHTML = html;
}

function renderChanges(items) {
  if (!items.length) {
    document.getElementById("changes").innerHTML = '<div class="stat"><span class="label">No changes in window</span></div>';
    return;
  }
  let html = '<table><tr><th>Repo</th><th>Action</th><th>Effect</th><th>Conf</th></tr>';
  for (const c of items) {
    const effCls = c.effect==="improved"?"ok":c.effect==="degraded"?"err":"";
    html += '<tr><td>' + (c.repo||"").split("/")[1] + '</td>'
      + '<td><span class="badge badge-' + (c.action==="auto_merge"?"ok":"warn") + '">' + c.action + '</span></td>'
      + '<td class="' + effCls + '">' + (c.effect||"pending") + '</td>'
      + '<td>' + fmtPct(c.confidence||0) + '</td></tr>';
  }
  html += '</table>';
  document.getElementById("changes").innerHTML = html;
}

document.getElementById("time").textContent = new Date().toLocaleString();
refresh();
refreshTimer = setInterval(refresh, 30000);
</script>
</body>
</html>`;

// ── API proxy ──────────────────────────────────────────────────────────

async function proxyAPI(path, env) {
  const origin = (env.AI_GATEWAY_ORIGIN_URL || "").trim();
  if (!origin) throw new Error("AI_GATEWAY_ORIGIN_URL not configured");

  const token = (env.DASHBOARD_API_TOKEN || "").trim();
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

    // API proxy: /api/* → origin
    if (url.pathname.startsWith("/api/")) {
      const apiPath = url.pathname.slice(4); // strip /api prefix
      try {
        return await proxyAPI(apiPath, env);
      } catch (e) {
        return new Response(JSON.stringify({ status: "error", error: e.message }), {
          status: 502,
          headers: { "Content-Type": "application/json" },
        });
      }
    }

    // Dashboard HTML
    if (url.pathname === "/" || url.pathname === "") {
      return new Response(HTML, {
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "public, max-age=300" },
      });
    }

    return new Response("Not found", { status: 404 });
  },
};
