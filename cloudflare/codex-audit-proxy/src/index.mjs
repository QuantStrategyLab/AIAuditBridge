const AUDIT_ROUTE = "/v1/codex-audit";
const HEALTH_ROUTE = "/healthz";
const JOB_ID_PATTERN = /^[A-Za-z0-9_-]{24,96}$/;

function jsonResponse(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

function isLocalhost(hostname) {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}

function withoutTrailingSlash(pathname) {
  return pathname.replace(/\/+$/, "");
}

function isAuditPath(pathname) {
  if (pathname === AUDIT_ROUTE || pathname === `${AUDIT_ROUTE}/jobs`) {
    return true;
  }
  const prefix = `${AUDIT_ROUTE}/jobs/`;
  return pathname.startsWith(prefix) && JOB_ID_PATTERN.test(pathname.slice(prefix.length));
}

function methodAllowed(method, pathname) {
  if (pathname === HEALTH_ROUTE) {
    return method === "GET";
  }
  if (pathname === AUDIT_ROUTE || pathname === `${AUDIT_ROUTE}/jobs`) {
    return method === "POST";
  }
  if (pathname.startsWith(`${AUDIT_ROUTE}/jobs/`)) {
    return method === "GET";
  }
  return false;
}

export function buildOriginUrl(rawOriginUrl, routePath, search = "") {
  if (!rawOriginUrl || !rawOriginUrl.trim()) {
    throw new Error("CODEX_AUDIT_ORIGIN_URL is required");
  }
  if (!isAuditPath(routePath)) {
    throw new Error("route is not allowed");
  }

  const origin = new URL(rawOriginUrl.trim());
  if (origin.protocol !== "https:" && !(origin.protocol === "http:" && isLocalhost(origin.hostname))) {
    throw new Error("CODEX_AUDIT_ORIGIN_URL must use HTTPS");
  }

  let basePath = withoutTrailingSlash(origin.pathname);
  if (!basePath.endsWith(AUDIT_ROUTE)) {
    basePath = `${basePath}${AUDIT_ROUTE}`;
  }
  const suffix = routePath.slice(AUDIT_ROUTE.length);

  origin.pathname = `${basePath}${suffix}`;
  origin.search = search;
  origin.hash = "";
  return origin.toString();
}

function forwardedHeaders(request, url) {
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("content-length");
  headers.set("X-Forwarded-Host", url.host);
  headers.set("X-Forwarded-Proto", "https");
  headers.set("X-Codex-Audit-Proxy", "cloudflare-worker");
  return headers;
}

async function proxyRequest(request, env) {
  const url = new URL(request.url);
  const originUrl = buildOriginUrl(env.CODEX_AUDIT_ORIGIN_URL, url.pathname, url.search);
  const hasBody = request.method !== "GET" && request.method !== "HEAD";

  return fetch(originUrl, {
    method: request.method,
    headers: forwardedHeaders(request, url),
    body: hasBody ? request.body : undefined,
    redirect: "manual",
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== HEALTH_ROUTE && !isAuditPath(url.pathname)) {
      return jsonResponse(404, { status: "error", error: "not found" });
    }
    if (!methodAllowed(request.method, url.pathname)) {
      return jsonResponse(405, { status: "error", error: "method not allowed" });
    }
    if (url.pathname === HEALTH_ROUTE) {
      return jsonResponse(200, { status: "ok" });
    }

    try {
      return await proxyRequest(request, env);
    } catch (error) {
      const message = error instanceof Error ? error.message : "origin request failed";
      if (message.includes("CODEX_AUDIT_ORIGIN_URL") || message.includes("HTTPS")) {
        return jsonResponse(500, { status: "error", error: message });
      }
      return jsonResponse(502, { status: "error", error: "origin request failed" });
    }
  },
};
