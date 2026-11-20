// Grafana Alerting → Multica webhook relay (AKKIA-28), deployed as a
// Cloudflare Pages advanced-mode worker (`_worker.js`).
//
// Multica's autopilot webhook can neither verify Grafana's HMAC signature nor
// trim payloads, and Grafana sends no per-notification Idempotency-Key, so this
// relay sits in front of it:
//
//   1. Verifies `X-Grafana-Alerting-Signature` = hex HMAC-SHA256 over
//      `<timestamp>:<raw body>` (contact point hmacConfig with a timestamp
//      header) and rejects timestamps outside MAX_SKEW_SECONDS.
//   2. Only keeps alerts whose `akkiabot_rule` label (added to every rule by
//      scripts/grafana_sync.py) starts with `akkiabot-` and whose `env` label
//      is in ALLOWED_ENVS. Resolved notices of dispatch-only rules (watch
//      rules, heartbeat) are dropped: nothing consumes them.
//   3. Forwards one minimal envelope per alert. `values`, `annotations`,
//      URLs and any other label never leave the relay.
//   4. Sends `Idempotency-Key: grafana-<fingerprint>-<startsAt epoch>-<status>`,
//      so Grafana's repeat notifications (repeat_interval) of the same firing
//      episode are stored by Multica only once, while a new episode (new
//      startsAt) or its resolution is a new event.
//   5. The Multica URL only lives in the relay's secrets.
//
// Secrets: GRAFANA_HMAC_SECRET, MULTICA_WEBHOOK_URL. Vars: ALLOWED_ENVS
// (comma separated, default "production,staging"), MAX_SKEW_SECONDS (300).
// Responses carry only a status and short reasons; bodies, URLs and secrets
// are never echoed or logged.

const MAX_BODY_BYTES = 512 * 1024;
const MAX_ALERTS = 20;
const DEFAULT_MAX_SKEW_SECONDS = 300;
const DEFAULT_ALLOWED_ENVS = "production,staging";
const FORWARD_TIMEOUT_MS = 10_000;
const SIGNATURE_HEADER = "x-grafana-alerting-signature";
const TIMESTAMP_HEADER = "x-grafana-alerting-timestamp";

const encoder = new TextEncoder();

const PATTERNS = {
  rule: /^akkiabot-[a-z0-9-]{1,80}$/,
  env: /^[a-z][a-z0-9-]{0,31}$/,
  fingerprint: /^[0-9a-f]{8,32}$/,
  handler: /^[A-Za-z0-9_.:<>-]{1,100}$/,
  command: /^[a-z0-9_]{1,32}$/,
  watch: /^AKKIA-\d{1,6}$/,
  watch_kind: /^(regression|exercised)$/,
  severity: /^[a-z]{1,16}$/,
};

function reply(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function hexToBytes(hex) {
  if (typeof hex !== "string" || !/^[0-9a-fA-F]+$/.test(hex) || hex.length % 2 !== 0) {
    return null;
  }
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

/** Constant-time HMAC-SHA256 check of `signatureHex` over `<timestamp>:<body>`. */
export async function verifySignature(secret, timestamp, body, signatureHex) {
  const expected = hexToBytes((signatureHex || "").trim());
  if (!secret || !expected || !/^\d{1,12}$/.test(timestamp || "")) return false;
  const prefix = encoder.encode(`${timestamp}:`);
  const signed = new Uint8Array(prefix.byteLength + body.byteLength);
  signed.set(prefix, 0);
  signed.set(body, prefix.byteLength);
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  return crypto.subtle.verify("HMAC", key, expected, signed);
}

function splitList(value) {
  return (value || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

const pick = (value, re) => (typeof value === "string" && re.test(value) ? value : null);

/**
 * Turn one (already authenticated) Grafana alert into the envelope to forward.
 * Returns `{ forward, key }` or `{ ignore: reason }`.
 */
export function buildForward(alert, allowedEnvs) {
  if (!alert || typeof alert !== "object") return { ignore: "malformed" };
  const labels = alert.labels && typeof alert.labels === "object" ? alert.labels : {};
  const status = alert.status;
  if (status !== "firing" && status !== "resolved") return { ignore: "status" };
  const rule = pick(labels.akkiabot_rule, PATTERNS.rule);
  if (!rule) return { ignore: "rule_not_allowed" };
  const env = pick(labels.env, PATTERNS.env);
  if (!env || !allowedEnvs.includes(env)) return { ignore: "env_not_allowed" };
  const dispatchOnly = labels.dispatch_only === "true";
  if (dispatchOnly && status === "resolved") return { ignore: "resolved_not_needed" };
  const fingerprint = pick(alert.fingerprint, PATTERNS.fingerprint);
  const startsMs = Date.parse(alert.startsAt);
  if (!fingerprint || !Number.isFinite(startsMs) || startsMs <= 0) return { ignore: "malformed" };
  const startsAt = new Date(Math.floor(startsMs / 1000) * 1000).toISOString().replace(".000Z", "Z");
  const startsEpoch = Math.floor(startsMs / 1000);

  const eventPayload = {
    source: "grafana",
    rule,
    env,
    status,
    fingerprint,
    starts_at: startsAt,
    severity: pick(labels.severity, PATTERNS.severity),
    handler: pick(labels.handler, PATTERNS.handler),
    command: pick(labels.command, PATTERNS.command),
    watch: pick(labels.watch, PATTERNS.watch),
    watch_kind: pick(labels.watch_kind, PATTERNS.watch_kind),
    dispatch_only: dispatchOnly,
    drill: labels.drill === "true",
  };
  return {
    forward: { event: `grafana_alert.${status}`, eventPayload },
    key: `grafana-${fingerprint}-${startsEpoch}-${status}`,
  };
}

async function forwardToMultica(env, envelope, idempotencyKey, fetchImpl) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FORWARD_TIMEOUT_MS);
  try {
    const res = await fetchImpl(env.MULTICA_WEBHOOK_URL, {
      method: "POST",
      headers: { "content-type": "application/json", "idempotency-key": idempotencyKey },
      body: JSON.stringify(envelope),
      signal: controller.signal,
    });
    // Only the status is logged: the URL carries the trigger token.
    if (!res.ok) {
      console.error(`multica forward failed: HTTP ${res.status}`);
      return false;
    }
    console.log(`multica forward ok: HTTP ${res.status} ${envelope.event}`);
    return true;
  } catch (err) {
    console.error(`multica forward failed: ${err?.name || "error"}`);
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/** Worker entry point; `deps` lets tests inject `fetch` and the clock. */
export async function handle(request, env, _ctx, deps = {}) {
  const fetchImpl = deps.fetch || fetch;
  const now = deps.now || (() => Date.now());

  if (request.method !== "POST") return reply(405, { status: "rejected", reason: "method" });
  if (!env.GRAFANA_HMAC_SECRET || !env.MULTICA_WEBHOOK_URL) {
    return reply(503, { status: "rejected", reason: "not_configured" });
  }

  const declared = Number(request.headers.get("content-length") || "0");
  if (declared > MAX_BODY_BYTES) return reply(413, { status: "rejected", reason: "too_large" });
  const body = new Uint8Array(await request.arrayBuffer());
  if (body.byteLength > MAX_BODY_BYTES) {
    return reply(413, { status: "rejected", reason: "too_large" });
  }

  const timestamp = request.headers.get(TIMESTAMP_HEADER) || "";
  const ok = await verifySignature(
    env.GRAFANA_HMAC_SECRET,
    timestamp,
    body,
    request.headers.get(SIGNATURE_HEADER),
  );
  if (!ok) return reply(401, { status: "rejected", reason: "signature" });
  const skew = Number(env.MAX_SKEW_SECONDS || DEFAULT_MAX_SKEW_SECONDS);
  if (Math.abs(now() / 1000 - Number(timestamp)) > skew) {
    return reply(401, { status: "rejected", reason: "timestamp" });
  }

  let payload;
  try {
    payload = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return reply(400, { status: "rejected", reason: "json" });
  }
  const alerts = Array.isArray(payload?.alerts) ? payload.alerts.slice(0, MAX_ALERTS) : [];
  const allowedEnvs = splitList(env.ALLOWED_ENVS || DEFAULT_ALLOWED_ENVS);

  const ignored = {};
  const forwards = new Map();
  for (const alert of alerts) {
    const result = buildForward(alert, allowedEnvs);
    if (result.ignore) ignored[result.ignore] = (ignored[result.ignore] || 0) + 1;
    else forwards.set(result.key, result.forward);
  }
  if (forwards.size === 0) return reply(200, { status: "ignored", ignored });

  // Forward synchronously: a failure answers 502 so Grafana retries the
  // notification; the idempotency key makes the retry harmless.
  const results = await Promise.all(
    [...forwards].map(([key, envelope]) => forwardToMultica(env, envelope, key, fetchImpl)),
  );
  const failed = results.filter((r) => !r).length;
  if (failed) return reply(502, { status: "forward_failed", failed, forwarded: results.length });
  return reply(202, { status: "accepted", forwarded: results.length, ignored });
}

export default {
  fetch(request, env, ctx) {
    return handle(request, env, ctx);
  },
};
