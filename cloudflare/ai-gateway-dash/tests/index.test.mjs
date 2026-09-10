import assert from "node:assert/strict";
import test from "node:test";

class FakeKV {
  constructor(options = {}) {
    this.store = new Map();
    this.failDelete = Boolean(options.failDelete);
  }

  async get(key) {
    return this.store.get(key) || null;
  }

  async put(key, value) {
    this.store.set(key, value);
  }

  async delete(key) {
    if (this.failDelete) throw new Error("delete failed");
    this.store.delete(key);
  }
}

async function authenticatedDashboard(t, upstreamFetch) {
  const originalFetch = globalThis.fetch;
  const gatewayCalls = [];
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json({ access_token: "github-token" });
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    gatewayCalls.push({ url, init });
    return upstreamFetch(url, init);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  const env = {
    GITHUB_OAUTH_CLIENT_ID: "client",
    GITHUB_OAUTH_CLIENT_SECRET: "oauth-client-test-value",
    DASHBOARD_SESSION_SECRET: "test-session-signing-value",
    DASHBOARD_SESSION_KV: new FakeKV(),
    DASHBOARD_API_TOKEN: "dashboard-api-token",
    AI_GATEWAY_ORIGIN_URL: "https://gateway.example",
  };
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    env,
  );
  const session = /dash_session=([^;]+)/.exec(callback.headers.get("set-cookie") || "")?.[1];
  assert.ok(session);
  return { env, cookie: "dash_session=" + session, gatewayCalls };
}

import worker, {
  REQUIRED_ORG,
  buildDashboardApiUrl,
  codexRemainingClass,
  codexRemainingPercent,
  codexWindowDisplay,
  requiresHumanAudit,
} from "../src/index.mjs";

test("buildDashboardApiUrl preserves query strings for allowed read routes", () => {
  assert.equal(
    buildDashboardApiUrl("https://origin.example/base", "/v1/ai/changes", "?days=7"),
    "https://origin.example/base/v1/ai/changes?days=7",
  );
});

test("buildDashboardApiUrl allows effectiveness route with query", () => {
  assert.equal(
    buildDashboardApiUrl("https://origin.example", "/v1/ai/changes/effectiveness", "?days=90"),
    "https://origin.example/v1/ai/changes/effectiveness?days=90",
  );
});

test("buildDashboardApiUrl maps AiGateway routes at root when origin is legacy audit endpoint", () => {
  assert.equal(
    buildDashboardApiUrl("https://origin.example/v1/codex-audit", "/v1/ai/health", ""),
    "https://origin.example/v1/ai/health",
  );
});

test("buildDashboardApiUrl rejects unsupported origin paths", () => {
  assert.throws(
    () => buildDashboardApiUrl("https://origin.example", "/v1/ai/execute/jobs", ""),
    /not allowed/,
  );
});

test("buildDashboardApiUrl rejects insecure origins", () => {
  assert.throws(
    () => buildDashboardApiUrl("http://origin.example", "/v1/ai/health", ""),
    /HTTPS/,
  );
});

test("buildDashboardApiUrl allows org health route", () => {
  assert.equal(
    buildDashboardApiUrl("https://origin.example", "/v1/ai/org-health", ""),
    "https://origin.example/v1/ai/org-health",
  );
});

test("codex window display shows remaining quota instead of used percent", () => {
  assert.equal(
    codexWindowDisplay({ used_percent: 44, window_duration_mins: 300 }),
    "5h 剩余 56%",
  );
});

test("codex window display prefers explicit remaining percent", () => {
  assert.equal(
    codexRemainingPercent({ used_percent: 44, remaining_percent: 57 }),
    57,
  );
  assert.equal(codexRemainingPercent({ used_percent: 44, remaining_percent: null }), 56);
});

test("codex quota severity is based on low remaining quota", () => {
  assert.equal(codexRemainingClass({ remaining_percent: 57 }), "ok");
  assert.equal(codexRemainingClass({ remaining_percent: 30 }), "warn");
  assert.equal(codexRemainingClass({ remaining_percent: 8 }), "err");
});


test("human audit display trusts backend decision fields", () => {
  assert.equal(requiresHumanAudit({ human_review_required: false, risk: "critical" }), false);
  assert.equal(requiresHumanAudit({ human_review_required: true, risk: "low" }), true);
  assert.equal(requiresHumanAudit({ human_review_required: "false", risk: "low" }), false);
  assert.equal(requiresHumanAudit({ human_review_required: null, risk: "critical" }), true);
  assert.equal(requiresHumanAudit({ state: "human_review_waiting_for_ci" }), true);
  assert.equal(requiresHumanAudit({ risk: "high" }), true);
  assert.equal(requiresHumanAudit({ risk: "low" }), false);
});

test("diagnosis API requires a session and rejects writes or invalid ids without upstream calls", async (t) => {
  const unauthenticated = await worker.fetch(
    new Request("https://dash.example/api/diagnoses"),
    {},
  );
  assert.equal(unauthenticated.status, 401);

  const dashboard = await authenticatedDashboard(t, async () => {
    throw new Error("upstream must not be called");
  });
  const write = await worker.fetch(
    new Request("https://dash.example/api/diagnoses", { method: "POST", headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(write.status, 405);
  const invalid = await worker.fetch(
    new Request("https://dash.example/api/diagnoses/not-a-job", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(invalid.status, 404);
  assert.equal(dashboard.gatewayCalls.length, 0);
});

test("diagnosis list exposes only bounded AIAuditBridge operational jobs", async (t) => {
  const validId = "A".repeat(32);
  const dashboard = await authenticatedDashboard(t, async (url) => {
    assert.match(url, /\/v1\/ai\/automation\/runs\?limit=100$/);
    return Response.json({ status: "ok", ledger: { runs: [
      { run_id: validId, task_name: "operational_data_diagnosis", task_state: "reviewed", updated_at: 10,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/AIAuditBridge", mode: "review_only", diagnosis_status: "succeeded" } },
      { run_id: "F".repeat(32), task_name: "operational_data_diagnosis", task_state: "reviewed", updated_at: 9,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/AIAuditBridge", mode: "review_only" } },
      { run_id: "B".repeat(32), task_name: "operational_data_diagnosis", task_state: "reviewed", updated_at: 20,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/Other", mode: "review_only", private: "do-not-leak" } },
      { run_id: "C".repeat(32), task_name: "other", task_state: "failed", updated_at: 30,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/AIAuditBridge", mode: "review_only" } },
      { run_id: "R".repeat(32), task_name: "historical_operational_diagnosis_rehearsal", task_state: "reviewed", updated_at: 11,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/AIAuditBridge", mode: "review_only", diagnosis_status: "succeeded" } },
    ] } });
  });
  const response = await worker.fetch(
    new Request("https://dash.example/api/diagnoses", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    status: "ok",
    diagnoses: [
      { job_id: "R".repeat(32), status: "succeeded", updated_at: 11, kind: "historical_rehearsal" },
      { job_id: validId, status: "succeeded", updated_at: 10, kind: "operational" },
      { job_id: "F".repeat(32), status: "unknown", updated_at: 9, kind: "operational" },
    ],
  });
});

test("diagnosis detail verifies ledger and job identity before exposing successful text", async (t) => {
  const jobId = "D".repeat(32);
  const output = "<img src=x onerror=alert(1)> read-only advice";
  const dashboard = await authenticatedDashboard(t, async (url) => {
    if (url.endsWith("/v1/ai/automation/runs/" + jobId)) {
      return Response.json({ status: "ok", run: {
        run_id: jobId, task_name: "operational_data_diagnosis", task_state: "reviewed", updated_at: 40,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/AIAuditBridge", mode: "review_only",
          diagnosis_status: "succeeded", diagnosis_summary: output },
      } });
    }
    throw new Error("unexpected upstream " + url);
  });
  const response = await worker.fetch(
    new Request("https://dash.example/api/diagnoses/" + jobId, { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    status: "ok",
    diagnosis: { job_id: jobId, status: "succeeded", updated_at: 40, output, advisory: true, kind: "operational" },
  });

  const html = await worker.fetch(
    new Request("https://dash.example/", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  const body = await html.text();
  assert.match(body, /数据故障诊断/);
  assert.match(body, /历史故障诊断演练/);
  assert.match(body, /diagnosis-output/);
  assert.match(body, /textContent/);
});

test("historical diagnosis rehearsal detail remains separate and read only", async (t) => {
  const jobId = "H".repeat(32);
  const dashboard = await authenticatedDashboard(t, async () => Response.json({ status: "ok", run: {
    run_id: jobId, task_name: "historical_operational_diagnosis_rehearsal", task_state: "reviewed", updated_at: 60,
    metadata: { origin: "service_job", source_repository: "QuantStrategyLab/AIAuditBridge", mode: "review_only",
      diagnosis_status: "succeeded", diagnosis_summary: "<script>历史演练摘要</script>" },
  } }));
  const response = await worker.fetch(
    new Request("https://dash.example/api/diagnoses/" + jobId, { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.deepEqual(await response.json(), { status: "ok", diagnosis: {
    job_id: jobId, status: "succeeded", updated_at: 60, advisory: true,
    kind: "historical_rehearsal", output: "<script>历史演练摘要</script>",
  } });
});

test("diagnosis detail hides mismatched, running, failed, missing, and upstream error data", async (t) => {
  const jobId = "E".repeat(32);
  let scenario = "mismatch";
  const dashboard = await authenticatedDashboard(t, async (url) => {
    if (url.includes("/automation/runs/")) {
      if (scenario === "upstream-error") return new Response("private upstream error", { status: 500 });
      if (scenario === "missing") return Response.json({ status: "error", error: "private missing detail" }, { status: 404 });
      return Response.json({ status: "ok", run: {
        run_id: jobId, task_name: "operational_data_diagnosis", task_state: scenario === "failed" ? "failed" : "running", updated_at: 50,
        metadata: { origin: "service_job", source_repository: scenario === "mismatch" ? "QuantStrategyLab/Other" : "QuantStrategyLab/AIAuditBridge", mode: "review_only",
          diagnosis_status: scenario, diagnosis_summary: "private output" },
      } });
    }
    throw new Error("unexpected upstream " + url);
  });

  for (const [value, expectedStatus] of [["mismatch", 404], ["running", 200], ["failed", 200], ["missing", 200], ["upstream-error", 502]]) {
    scenario = value;
    const response = await worker.fetch(
      new Request("https://dash.example/api/diagnoses/" + jobId, { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, expectedStatus, value);
    const text = await response.text();
    assert.doesNotMatch(text, /private|traceback|prompt/, value);
  }
});

test("diagnosis list returns an empty result without claiming successful automation", async (t) => {
  const dashboard = await authenticatedDashboard(t, async () => Response.json({ status: "ok", ledger: { runs: [] } }));
  const response = await worker.fetch(
    new Request("https://dash.example/api/diagnoses", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.deepEqual(await response.json(), { status: "ok", diagnoses: [] });
});

test("diagnosis API rejects redirects and oversized upstream responses", async (t) => {
  let scenario = "redirect";
  const dashboard = await authenticatedDashboard(t, async () => scenario === "redirect"
    ? new Response(null, { status: 302, headers: { Location: "https://private.example/detail" } })
    : new Response("{}", { headers: { "Content-Length": "1000001" } }));
  for (const value of ["redirect", "oversized"]) {
    scenario = value;
    const response = await worker.fetch(
      new Request("https://dash.example/api/diagnoses", { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, 502, value);
    assert.deepEqual(await response.json(), { status: "error", error: "诊断数据暂不可用" });
  }
});

test("authenticated dashboard html ships codex remaining quota display", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json(Object.fromEntries([["access_token", "github-token"]]));
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    throw new Error("unexpected fetch " + url);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const env = Object.fromEntries([
    ["GITHUB_OAUTH_CLIENT_ID", "client"],
    ["GITHUB_OAUTH_CLIENT_SECRET", "oauth-client-test-value"],
    ["DASHBOARD_SESSION_SECRET", "test-session-signing-value"],
    ["DASHBOARD_SESSION_KV", new FakeKV()],
  ]);
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    env,
  );
  const session = /dash_session=([^;]+)/.exec(callback.headers.get("set-cookie") || "")?.[1];
  assert.ok(session);

  const user = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  assert.equal(user.status, 200);
  assert.equal((await user.json()).login, "operator");

  const dashboard = await worker.fetch(
    new Request("https://dash.example/", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  const html = await dashboard.text();
  assert.equal(dashboard.status, 200);
  assert.match(html, /function codexRemainingPercent/);
  assert.match(html, /Codex 账户/);
  assert.match(html, /剩余/);

  const tampered = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session + "x" } }),
    env,
  );
  assert.equal(tampered.status, 401);

  const logout = await worker.fetch(
    new Request("https://dash.example/logout", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  assert.equal(logout.status, 302);

  const revoked = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  assert.equal(revoked.status, 401, "logout revokes the active signed session when KV is bound");
});

test("dashboard callback requires dedicated session signing secret", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json(Object.fromEntries([["access_token", "github-token"]]));
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    throw new Error("unexpected fetch " + url);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const env = Object.fromEntries([
    ["GITHUB_OAUTH_CLIENT_ID", "client"],
    ["GITHUB_OAUTH_CLIENT_SECRET", "oauth-client-test-value"],
  ]);
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    env,
  );
  assert.equal(callback.status, 302);
  assert.match(callback.headers.get("location") || "", /error=/);
  assert.doesNotMatch(callback.headers.get("set-cookie") || "", /dash_session=/);
});


test("sessions issued before KV binding require re-login", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json(Object.fromEntries([["access_token", "github-token"]]));
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    throw new Error("unexpected fetch " + url);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const baseEnv = Object.fromEntries([
    ["GITHUB_OAUTH_CLIENT_ID", "client"],
    ["GITHUB_OAUTH_CLIENT_SECRET", "oauth-client-test-value"],
    ["DASHBOARD_SESSION_SECRET", "test-session-signing-value"],
  ]);
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    baseEnv,
  );
  const session = /dash_session=([^;]+)/.exec(callback.headers.get("set-cookie") || "")?.[1];
  assert.ok(session);

  const beforeKv = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    baseEnv,
  );
  assert.equal(beforeKv.status, 200);

  const kvEnv = Object.fromEntries([...Object.entries(baseEnv), ["DASHBOARD_SESSION_KV", new FakeKV()]]);
  const afterKv = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    kvEnv,
  );
  assert.equal(afterKv.status, 401);
});

test("revocable sessions fail closed when KV binding is removed", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json(Object.fromEntries([["access_token", "github-token"]]));
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    throw new Error("unexpected fetch " + url);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const baseEnv = Object.fromEntries([
    ["GITHUB_OAUTH_CLIENT_ID", "client"],
    ["GITHUB_OAUTH_CLIENT_SECRET", "oauth-client-test-value"],
    ["DASHBOARD_SESSION_SECRET", "test-session-signing-value"],
  ]);
  const kvEnv = Object.fromEntries([...Object.entries(baseEnv), ["DASHBOARD_SESSION_KV", new FakeKV()]]);
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    kvEnv,
  );
  const session = /dash_session=([^;]+)/.exec(callback.headers.get("set-cookie") || "")?.[1];
  assert.ok(session);

  const withKv = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    kvEnv,
  );
  assert.equal(withKv.status, 200);

  const withoutKv = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    baseEnv,
  );
  assert.equal(withoutKv.status, 401);
});

test("logout surfaces KV revocation failures", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json(Object.fromEntries([["access_token", "github-token"]]));
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    throw new Error("unexpected fetch " + url);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const env = Object.fromEntries([
    ["GITHUB_OAUTH_CLIENT_ID", "client"],
    ["GITHUB_OAUTH_CLIENT_SECRET", "oauth-client-test-value"],
    ["DASHBOARD_SESSION_SECRET", "test-session-signing-value"],
    ["DASHBOARD_SESSION_KV", new FakeKV({ failDelete: true })],
  ]);
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    env,
  );
  const session = /dash_session=([^;]+)/.exec(callback.headers.get("set-cookie") || "")?.[1];
  assert.ok(session);

  const logout = await worker.fetch(
    new Request("https://dash.example/logout", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  assert.equal(logout.status, 503);
  assert.equal((await logout.json()).error, "session_revocation_failed");

  const stillActive = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  assert.equal(stillActive.status, 200);
});
