// Run with `node --test deploy/sentry-relay/` (also driven by tests/test_sentry_relay.py).
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { handle } from "./worker.mjs";

// mock values: not real secrets or endpoints.
const MOCK_CLIENT_SECRET = "mock-sentry-client-secret";
const MOCK_MULTICA_URL = "https://multica.invalid/api/webhooks/autopilots/mock-token";
const NOW_MS = Date.UTC(2026, 10, 17, 8, 0, 0);

const env = {
  SENTRY_CLIENT_SECRET: MOCK_CLIENT_SECRET,
  MULTICA_WEBHOOK_URL: MOCK_MULTICA_URL,
  ALLOWED_PROJECT_SLUGS: "akkiabot-staging",
  SENTRY_INSTALLATION_UUID: "mock-installation-uuid",
};

function issuePayload(overrides = {}) {
  return {
    action: "created",
    installation: { uuid: "mock-installation-uuid" },
    actor: { type: "user", name: "Mock Person", email: "mock@example.invalid" },
    data: {
      issue: {
        id: "1234567",
        shortId: "AKKIABOT-STAGING-Z",
        title: "ValueError: secret-looking free text 123456:mock",
        culprit: "app.handlers in handle",
        metadata: { value: "free text from the exception" },
        level: "error",
        status: "unresolved",
        substatus: "new",
        project: { id: "1", slug: "akkiabot-staging" },
        ...overrides.issue,
      },
    },
    ...overrides.top,
  };
}

function signedRequest(payload, { resource = "issue", secret = MOCK_CLIENT_SECRET, ts } = {}) {
  const body = typeof payload === "string" ? payload : JSON.stringify(payload);
  const signature = createHmac("sha256", secret).update(body).digest("hex");
  return new Request("https://relay.invalid/", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "sentry-hook-resource": resource,
      "sentry-hook-signature": signature,
      "sentry-hook-timestamp": String(ts ?? Math.floor(NOW_MS / 1000)),
      "request-id": "0f2b9c1e-mock-4a7d-request",
    },
    body,
  });
}

function recorder() {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return new Response("{}", { status: 200 });
  };
  return { calls, deps: { fetch: fetchImpl, now: () => NOW_MS } };
}

test("forwards a trimmed payload with an idempotency key", async () => {
  const { calls, deps } = recorder();
  const res = await handle(signedRequest(issuePayload()), env, undefined, deps);
  assert.equal(res.status, 202);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, MOCK_MULTICA_URL);
  assert.equal(calls[0].init.headers["idempotency-key"], "sentry-0f2b9c1e-mock-4a7d-request");
  const sent = JSON.parse(calls[0].init.body);
  assert.deepEqual(sent, {
    event: "sentry_issue.created",
    eventPayload: {
      source: "sentry",
      resource: "issue",
      action: "created",
      issue_id: "1234567",
      project_slug: "akkiabot-staging",
      short_id: "AKKIABOT-STAGING-Z",
      substatus: "new",
      level: "error",
      request_id: "0f2b9c1e-mock-4a7d-request",
    },
  });
  for (const leaked of ["free text", "Mock Person", "mock@example", "handlers", "123456:mock"]) {
    assert.ok(!calls[0].init.body.includes(leaked), `forwarded body leaked ${leaked}`);
  }
});

test("rejects a bad or missing signature without forwarding", async () => {
  const { calls, deps } = recorder();
  const wrong = await handle(
    signedRequest(issuePayload(), { secret: "mock-other-secret" }),
    env,
    undefined,
    deps,
  );
  assert.equal(wrong.status, 401);
  const req = signedRequest(issuePayload());
  req.headers.delete("sentry-hook-signature");
  const missing = await handle(req, env, undefined, deps);
  assert.equal(missing.status, 401);
  assert.equal(calls.length, 0);
});

test("rejects stale timestamps", async () => {
  const { calls, deps } = recorder();
  const stale = Math.floor(NOW_MS / 1000) - 3600;
  const res = await handle(signedRequest(issuePayload(), { ts: stale }), env, undefined, deps);
  assert.equal(res.status, 401);
  assert.equal(calls.length, 0);
});

test("ignores non-target project, installation, resource and action", async () => {
  const { calls, deps } = recorder();
  const cases = [
    signedRequest(issuePayload({ issue: { project: { id: "2", slug: "akkia-bot-lab" } } })),
    signedRequest(issuePayload({ top: { installation: { uuid: "mock-other-installation" } } })),
    signedRequest(issuePayload(), { resource: "installation" }),
    signedRequest(issuePayload({ top: { action: "assigned" } })),
    signedRequest(issuePayload({ issue: { id: "not-a-number" } })),
  ];
  for (const req of cases) {
    const res = await handle(req, env, undefined, deps);
    assert.equal(res.status, 200);
    assert.equal((await res.json()).status, "ignored");
  }
  assert.equal(calls.length, 0);
});

test("forwards resolved and unresolved (regression) actions", async () => {
  const { calls, deps } = recorder();
  for (const [action, substatus] of [
    ["resolved", null],
    ["unresolved", "regressed"],
  ]) {
    const res = await handle(
      signedRequest(issuePayload({ top: { action }, issue: { substatus } })),
      env,
      undefined,
      deps,
    );
    assert.equal(res.status, 202);
  }
  assert.deepEqual(
    calls.map((c) => JSON.parse(c.init.body).event),
    ["sentry_issue.resolved", "sentry_issue.unresolved"],
  );
});

test("refuses to run unconfigured and rejects non-POST", async () => {
  const { deps } = recorder();
  const res = await handle(signedRequest(issuePayload()), {}, undefined, deps);
  assert.equal(res.status, 503);
  const get = await handle(new Request("https://relay.invalid/"), env, undefined, deps);
  assert.equal(get.status, 405);
});

test("a forward failure is not surfaced to Sentry", async () => {
  const deps = {
    fetch: async () => {
      throw new TypeError("network");
    },
    now: () => NOW_MS,
  };
  const res = await handle(signedRequest(issuePayload()), env, undefined, deps);
  assert.equal(res.status, 202);
});
