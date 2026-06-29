/** CodexAuditBridge Cloudflare proxy — forwards authenticated requests to VPS origin.

  Allowed paths (expanded for AiGateway v2):
    GET  /healthz
    GET  /v1/ai/health
    GET  /v1/ai/quota
    GET  /v1/ai/changes
    GET  /v1/ai/changes/*
    GET  /v1/ai/feedback/shadow
    GET  /v1/ai/execute/jobs/*
    GET  /v1/codex-audit/jobs/*
    POST /v1/ai/analyze
    POST /v1/ai/execute
    POST /v1/ai/execute/jobs
    POST /v1/ai/review
    POST /v1/ai/feedback/register
    POST /v1/ai/feedback/evaluate
    POST /v1/ai/feedback/shadow
    POST /v1/codex-audit
    POST /v1/codex-audit/jobs

  The dashboard Worker at quantstrategylab-ai-gateway-dash serves the UI.
 */

const JOB_ID_PATTERN = /^[A-Za-z0-9_-]{24,96}$/;
const HEALTH_ROUTE = "/healthz";

// Allowed route prefixes for AiGateway v2 + backward compat
const ALLOWED_ROUTES = [
  // v2 endpoints
  "/v1/ai/analyze",
  "/v1/ai/execute",
  "/v1/ai/execute/jobs",
  "/v1/ai/review",
  "/v1/ai/health",
  "/v1/ai/quota",
  "/v1/ai/changes",
  "/v1/ai/feedback/register",
  "/v1/ai/feedback/evaluate",
  "/v1/ai/feedback/shadow",
  // backward compat
  "/v1/codex-audit",
  "/v1/codex-audit/jobs",
];

// GET-only routes
const GET_ROUTES = new Set([
  "/healthz",
  "/v1/ai/health",
  "/v1/ai/quota",
  "/v1/ai/feedback/shadow",
]);

// POST-only routes (everything else in ALLOWED_ROUTES)


function jsonResponse(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

function withoutTrailingSlash(pathname) {
  return pathname.replace(/\/+$/, "");
}

/** Check if pathname matches any allowed route or is a valid sub-path (jobs/{id}, changes/{id}). */
function matchRoute(pathname) {
  const clean = withoutTrailingSlash(pathname);

  // Exact matches
  if (ALLOWED_ROUTES.includes(clean)) return clean;

  // Sub-path: /v1/ai/execute/jobs/{id}  or  /v1/codex-audit/jobs/{id}
  for (const jobsPrefix of ["/v1/ai/execute/jobs/", "/v1/codex-audit/jobs/"]) {
    if (clean.startsWith(jobsPrefix)) {
      const id = clean.slice(jobsPrefix.length);
      if (JOB_ID_PATTERN.test(id)) return jobsPrefix.slice(0, -1); // parent route
    }
  }

  // Sub-path: /v1/ai/changes/{id} or /v1/ai/changes/effectiveness
  if (clean.startsWith("/v1/ai/changes/")) {
    return "/v1/ai/changes";
  }

  return null;
}

function methodAllowed(method, pathname) {
  if (pathname === HEALTH_ROUTE) return method === "GET";
  if (pathname === "/v1/ai/health" || pathname === "/v1/ai/quota") return method === "GET";
  if (pathname === "/v1/ai/feedback/shadow") return method === "GET" || method === "POST";
  if (pathname === "/v1/ai/changes") return method === "GET";
  // All other routes: POST only
  if (["/v1/ai/analyze", "/v1/ai/execute", "/v1/ai/execute/jobs",
       "/v1/ai/review", "/v1/ai/feedback/register", "/v1/ai/feedback/evaluate",
       "/v1/codex-audit", "/v1/codex-audit/jobs"].includes(pathname)) {
    return method === "POST";
  }
  // GET for job polling and change detail
  if (pathname.startsWith("/v1/ai/execute/jobs/") || pathname.startsWith("/v1/codex-audit/jobs/")) {
    return method === "GET";
  }
  if (pathname.startsWith("/v1/ai/changes/")) return method === "GET";
  return false;
}

export function buildOriginUrl(rawOriginUrl, pathname, search = "") {
  if (!rawOriginUrl || !rawOriginUrl.trim()) {
    throw new Error("CODEX_AUDIT_ORIGIN_URL is required");
  }
  const route = matchRoute(pathname);
  if (!route) throw new Error("route is not allowed");

  const origin = new URL(rawOriginUrl.trim());
  origin.pathname = withoutTrailingSlash(origin.pathname) + pathname;
  origin.search = search;
  origin.hash = "";
  return origin.toString();
}

function forwardedHeaders(request) {
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("content-length");
  headers.set("X-Forwarded-Proto", "https");
  headers.set("X-Codex-Audit-Proxy", "cloudflare-worker");
  return headers;
}

async function proxyRequest(request, env) {
  const url = new URL(request.url);
  const originUrl = buildOriginUrl(env.CODEX_AUDIT_ORIGIN_URL, url.pathname, url.search);
  const hasBody = request.method !== "GET" && request.method !== "HEAD";

  const resp = await fetch(originUrl, {
    method: request.method,
    headers: forwardedHeaders(request),
    body: hasBody ? request.body : undefined,
    redirect: "manual",
  });

  // Add CORS headers to proxied responses
  const corsHeaders = new Headers(resp.headers);
  corsHeaders.set("Access-Control-Allow-Origin", "*");
  return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers: corsHeaders });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
          "Access-Control-Allow-Headers": "Authorization, Content-Type",
        },
      });
    }

    const route = matchRoute(url.pathname);

    if (url.pathname !== HEALTH_ROUTE && !route) {
      return jsonResponse(404, { status: "error", error: "not found" });
    }
    if (!methodAllowed(request.method, route || url.pathname)) {
      return jsonResponse(405, { status: "error", error: "method not allowed" });
    }
    if (url.pathname === HEALTH_ROUTE) {
      return jsonResponse(200, { status: "ok" });
    }

    try {
      return await proxyRequest(request, env);
    } catch (error) {
      const message = error instanceof Error ? error.message : "origin request failed";
      return jsonResponse(502, { status: "error", error: message });
    }
  },
};
