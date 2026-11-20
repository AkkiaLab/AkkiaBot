// Run with `node --test deploy/grafana-relay/` (also driven by tests/test_grafana_relay.py).
// All alert data below is fictional.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { handle } from "./worker.mjs";

// mock values: not real secrets or endpoints.
const MOCK_HMAC_SECRET = "mock-grafana-hmac-secret";
const MOCK_MULTICA_URL = "https://multica.invalid/api/webhooks/autopilots/mock-token";
const NOW_MS = Date.UTC(2026, 10, 20, 8, 0, 0);

const env = { GRAFANA_HMAC_SECRET: MOCK_HMAC_SECRET, MULTICA_WEBHOOK_URL: MOCK_MULTICA_URL };

function alert(overrides = {}) {
  return {
    status: "firing",
    labels: {
      alertname: "fictional rule",
      akkiabot_rule: "akkiabot-readyz-failing",
      env: "staging",
      severity: "critical",
      service: "akkiabot",
      grafana_folder: "AkkiaBot",
      chat_id: "-1009999999999",
      ...overrides.labels,
    },
    annotations: { summary: "fictional summary 42", description: "free text" },
    startsAt: "2026-11-20T07:58:30.123Z",
    endsAt: "0001-01-01T00:00:00Z",
    generatorURL: "https://grafana.invalid/alerting/grafana/mock/view",
    fingerprint: "0123456789abcdef",
    values: { B: 42.5, C: 1 },
    valueString: "[ var='B' value=42.5 ]",
    ...overrides.top,
  };
}

function signedRequest(alerts, { secret = MOCK_HMAC_SECRET, ts, raw } = {}) {
  const body = raw ?? JSON.stringify({ receiver: "akkiabot-dispatch", status: "firing", alerts });
  const stamp = String(ts ?? Math.floor(NOW_MS / 1000));
  const signature = createHmac("sha256", secret).update(`${stamp}:${body}`).digest("hex");
  return new Request("https://relay.invalid/", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-grafana-alerting-signature": signature,
      "x-grafana-alerting-timestamp": stamp,
    },
    body,
  });
}

function recorder(status = 200) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return new Response("{}", { status });
  };
  return { calls, deps: { fetch: fetchImpl, now: () => NOW_MS } };
}

test("forwards a trimmed alert with an episode idempotency key", async () => {
  const { calls, deps } = recorder();
  const res = await handle(signedRequest([alert()]), env, undefined, deps);
  assert.equal(res.status, 202);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, MOCK_MULTICA_URL);
  assert.equal(
    calls[0].init.headers["idempotency-key"],
    "grafana-0123456789abcdef-1795161510-firing",
  );
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    event: "grafana_alert.firing",
    eventPayload: {
      source: "grafana",
      rule: "akkiabot-readyz-failing",
      env: "staging",
      status: "firing",
      fingerprint: "0123456789abcdef",
      starts_at: "2026-11-20T07:58:30Z",
      severity: "critical",
      handler: null,
      command: null,
      watch: null,
      watch_kind: null,
      dispatch_only: false,
      drill: false,
    },
  });
  for (const leaked of [
    "42",
    "free text",
    "summary",
    "-100",
    "grafana.invalid",
    "fictional rule",
  ]) {
    assert.ok(!calls[0].init.body.includes(leaked), `forwarded body leaked ${leaked}`);
  }
});

test("repeat notifications share a key; resolution and a new episode do not", async () => {
  const { calls, deps } = recorder();
  await handle(signedRequest([alert()]), env, undefined, deps);
  await handle(signedRequest([alert()]), env, undefined, deps);
  await handle(signedRequest([alert({ top: { status: "resolved" } })]), env, undefined, deps);
  await handle(
    signedRequest([alert({ top: { startsAt: "2026-11-20T07:59:30Z" } })]),
    env,
    undefined,
    deps,
  );
  const keys = calls.map((c) => c.init.headers["idempotency-key"]);
  assert.equal(keys[0], keys[1]);
  assert.equal(new Set(keys).size, 3);
});

test("rejects a forged, missing or unsigned-timestamp signature", async () => {
  const { calls, deps } = recorder();
  const forged = await handle(
    signedRequest([alert()], { secret: "mock-other-secret" }),
    env,
    undefined,
    deps,
  );
  assert.equal(forged.status, 401);
  const missing = signedRequest([alert()]);
  missing.headers.delete("x-grafana-alerting-signature");
  assert.equal((await handle(missing, env, undefined, deps)).status, 401);
  // The timestamp is part of the signed material: changing it breaks the signature.
  const moved = signedRequest([alert()]);
  moved.headers.set("x-grafana-alerting-timestamp", String(Math.floor(NOW_MS / 1000) - 1));
  assert.equal((await handle(moved, env, undefined, deps)).status, 401);
  assert.equal(calls.length, 0);
});

test("rejects stale timestamps", async () => {
  const { calls, deps } = recorder();
  const stale = Math.floor(NOW_MS / 1000) - 3600;
  const res = await handle(signedRequest([alert()], { ts: stale }), env, undefined, deps);
  assert.equal(res.status, 401);
  assert.equal((await res.json()).reason, "timestamp");
  assert.equal(calls.length, 0);
});

test("ignores other rules, environments and malformed alerts", async () => {
  const { calls, deps } = recorder();
  const res = await handle(
    signedRequest([
      alert({ labels: { akkiabot_rule: "someone-else" } }),
      alert({ labels: { akkiabot_rule: undefined } }),
      alert({ labels: { env: "dev" } }),
      alert({ top: { fingerprint: "not hex!" } }),
      alert({ top: { startsAt: "garbage" } }),
      alert({ top: { status: "pending" } }),
    ]),
    env,
    undefined,
    deps,
  );
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.status, "ignored");
  assert.deepEqual(body.ignored, {
    rule_not_allowed: 2,
    env_not_allowed: 1,
    malformed: 2,
    status: 1,
  });
  assert.equal(calls.length, 0);
});

test("drops resolved notices of dispatch-only rules but forwards their firing", async () => {
  const { calls, deps } = recorder();
  const watch = {
    akkiabot_rule: "akkiabot-watch-regression",
    env: "production",
    handler: "command:spam",
    watch_kind: "regression",
    dispatch_only: "true",
  };
  const resolved = await handle(
    signedRequest([alert({ labels: watch, top: { status: "resolved" } })]),
    env,
    undefined,
    deps,
  );
  assert.equal(resolved.status, 200);
  assert.equal(calls.length, 0);
  const firing = await handle(signedRequest([alert({ labels: watch })]), env, undefined, deps);
  assert.equal(firing.status, 202);
  const sent = JSON.parse(calls[0].init.body).eventPayload;
  assert.equal(sent.handler, "command:spam");
  assert.equal(sent.watch_kind, "regression");
  assert.equal(sent.dispatch_only, true);
});

test("forwards each alert of a grouped notification separately", async () => {
  const { calls, deps } = recorder();
  const res = await handle(
    signedRequest([
      alert(),
      alert({ top: { fingerprint: "fedcba9876543210" }, labels: { env: "production" } }),
    ]),
    env,
    undefined,
    deps,
  );
  assert.equal(res.status, 202);
  assert.equal((await res.json()).forwarded, 2);
  assert.equal(calls.length, 2);
});

test("a forward failure answers 502 so Grafana retries", async () => {
  const { deps } = recorder(500);
  assert.equal((await handle(signedRequest([alert()]), env, undefined, deps)).status, 502);
  const throwing = {
    fetch: async () => {
      throw new TypeError("network");
    },
    now: () => NOW_MS,
  };
  assert.equal((await handle(signedRequest([alert()]), env, undefined, throwing)).status, 502);
});

test("refuses to run unconfigured, rejects non-POST and bad JSON", async () => {
  const { deps } = recorder();
  assert.equal((await handle(signedRequest([alert()]), {}, undefined, deps)).status, 503);
  assert.equal(
    (await handle(new Request("https://relay.invalid/"), env, undefined, deps)).status,
    405,
  );
  assert.equal((await handle(signedRequest([], { raw: "{" }), env, undefined, deps)).status, 400);
});
