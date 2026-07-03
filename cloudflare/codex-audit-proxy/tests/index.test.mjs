import assert from "node:assert/strict";
import test from "node:test";

import worker, { buildOriginUrl } from "../src/index.mjs";

const JOB_ID = "abcdefghijklmnopqrstuvwxyzABCD12";

test("buildOriginUrl appends audit route to base origin", () => {
  assert.equal(
    buildOriginUrl("https://origin.example", "/v1/codex-audit", "?run=1"),
    "https://origin.example/v1/codex-audit?run=1",
  );
});

test("buildOriginUrl accepts full audit endpoint origin", () => {
  assert.equal(
    buildOriginUrl("https://origin.example/v1/codex-audit", "/v1/codex-audit"),
    "https://origin.example/v1/codex-audit",
  );
});

test("buildOriginUrl maps async submit route next to audit endpoint", () => {
  assert.equal(
    buildOriginUrl("https://origin.example/v1/codex-audit", "/v1/codex-audit/jobs"),
    "https://origin.example/v1/codex-audit/jobs",
  );
});

test("buildOriginUrl maps async status route next to audit endpoint", () => {
  assert.equal(
    buildOriginUrl("https://origin.example/v1/codex-audit", `/v1/codex-audit/jobs/${JOB_ID}`),
    `https://origin.example/v1/codex-audit/jobs/${JOB_ID}`,
  );
});

test("buildOriginUrl maps AiGateway routes at root when origin is legacy audit endpoint", () => {
  assert.equal(
    buildOriginUrl("https://origin.example/v1/codex-audit", "/v1/ai/health"),
    "https://origin.example/v1/ai/health",
  );
});

test("buildOriginUrl preserves nested base path", () => {
  assert.equal(
    buildOriginUrl("https://origin.example/codex", "/v1/codex-audit"),
    "https://origin.example/codex/v1/codex-audit",
  );
});

test("buildOriginUrl rejects insecure external origins", () => {
  assert.throws(
    () => buildOriginUrl("http://origin.example", "/v1/codex-audit"),
    /must use HTTPS/,
  );
});

test("worker health check is local and does not require origin", async () => {
  const response = await worker.fetch(new Request("https://proxy.example/healthz"), {});
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: "ok" });
});

test("worker rejects unsupported route before origin lookup", async () => {
  const response = await worker.fetch(new Request("https://proxy.example/other"), {});
  assert.equal(response.status, 404);
  assert.deepEqual(await response.json(), { status: "error", error: "not found" });
});

test("worker rejects unsupported method before origin lookup", async () => {
  const response = await worker.fetch(new Request("https://proxy.example/v1/codex-audit", { method: "GET" }), {});
  assert.equal(response.status, 405);
  assert.deepEqual(await response.json(), { status: "error", error: "method not allowed" });
});

test("worker rejects missing bearer token before origin lookup", async () => {
  const response = await worker.fetch(new Request("https://proxy.example/v1/codex-audit/jobs", { method: "POST" }), {});
  assert.equal(response.status, 401);
  assert.deepEqual(await response.json(), { status: "error", error: "missing bearer token" });
});

test("worker rejects malformed job ids before origin lookup", async () => {
  const response = await worker.fetch(new Request("https://proxy.example/v1/codex-audit/jobs/nope"), {});
  assert.equal(response.status, 404);
  assert.deepEqual(await response.json(), { status: "error", error: "not found" });
});

test("worker allows authenticated job status polling subroutes", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    assert.equal(url, `https://origin.example/v1/codex-audit/jobs/${JOB_ID}`);
    assert.equal(init.method, "GET");
    return new Response(JSON.stringify({ status: "queued" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  try {
    const response = await worker.fetch(
      new Request(`https://proxy.example/v1/codex-audit/jobs/${JOB_ID}`, {
        headers: { Authorization: "Bearer test-token" },
      }),
      { CODEX_AUDIT_ORIGIN_URL: "https://origin.example" },
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { status: "queued" });
  } finally {
    globalThis.fetch = originalFetch;
  }
});
