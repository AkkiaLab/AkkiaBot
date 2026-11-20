// Sentry → outgoing webhook relay, deployed as a Cloudflare Worker.
//
// The receiving webhook (authenticated only by a token in its URL) can neither
// verify `Sentry-Hook-Signature` nor trim payloads, so this relay sits in front
// of it:
//
//   1. Verifies the HMAC-SHA256 signature (client secret of the Sentry internal
//      integration) over the raw body and rejects stale timestamps.
//   2. Only forwards `issue` webhooks with action created / unresolved /
//      resolved, for the configured installation and project slugs.
//   3. Forwards a minimal, non-free-text payload. Titles, culprits, exception
//      metadata, actors and stats never leave the relay; the dispatcher reads
//      what it needs from the Sentry API with a read-only token.
//   4. Sends `Idempotency-Key: sentry-<Request-ID>` so the receiver can store a
//      redelivery only once.
//
// Secrets (Worker secrets, never logged): SENTRY_CLIENT_SECRET, FORWARD_WEBHOOK_URL.
// Vars: ALLOWED_PROJECT_SLUGS (comma separated), SENTRY_INSTALLATION_UUID
// (optional until the integration exists), MAX_SKEW_SECONDS (default 300).
//
// Responses carry only a status and a short reason; request bodies, URLs and
// secrets are never echoed or logged.

const MAX_BODY_BYTES = 256 * 1024;
const FORWARD_ACTIONS = new Set(["created", "unresolved", "resolved"]);
const DEFAULT_MAX_SKEW_SECONDS = 300;
const FORWARD_TIMEOUT_MS = 10_000;

const encoder = new TextEncoder();

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

/** Constant-time HMAC-SHA256 check of `signatureHex` over `body`. */
export async function verifySignature(secret, body, signatureHex) {
  const expected = hexToBytes((signatureHex || "").trim());
  if (!secret || !expected) return false;
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  return crypto.subtle.verify("HMAC", key, expected, body);
}

function splitList(value) {
  return (value || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Validate an already-authenticated Sentry webhook and build the trimmed
 * envelope to forward. Returns `{ forward }` or `{ ignore: reason }`.
 */
export function buildForward(resource, payload, env, requestId) {
  if (resource !== "issue") return { ignore: `resource_${resource || "missing"}` };
  if (!payload || typeof payload !== "object") return { ignore: "malformed" };
  const action = payload.action;
  if (!FORWARD_ACTIONS.has(action)) return { ignore: "action_not_forwarded" };

  const wantInstallation = (env.SENTRY_INSTALLATION_UUID || "").trim();
  const installation = payload.installation?.uuid;
  if (wantInstallation && installation !== wantInstallation) return { ignore: "installation" };

  const issue = payload.data?.issue;
  const projectSlug = issue?.project?.slug;
  if (!splitList(env.ALLOWED_PROJECT_SLUGS).includes(projectSlug)) {
    return { ignore: "project_not_allowed" };
  }
  const issueId = String(issue?.id ?? "");
  if (!/^\d{1,20}$/.test(issueId)) return { ignore: "malformed" };

  const pick = (v, re) => (typeof v === "string" && re.test(v) ? v : null);
  return {
    forward: {
      event: `sentry_issue.${action}`,
      eventPayload: {
        source: "sentry",
        resource: "issue",
        action,
        issue_id: issueId,
        project_slug: projectSlug,
        short_id: pick(issue.shortId, /^[A-Z0-9-]{1,64}$/),
        substatus: pick(issue.substatus, /^[a-z_]{1,32}$/),
        level: pick(issue.level, /^[a-z]{1,16}$/),
        request_id: pick(requestId, /^[0-9A-Za-z-]{1,64}$/),
      },
    },
  };
}

function forwardUrl(env) {
  return env.FORWARD_WEBHOOK_URL;
}

async function forward(env, envelope, idempotencyKey, fetchImpl) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FORWARD_TIMEOUT_MS);
  try {
    const res = await fetchImpl(forwardUrl(env), {
      method: "POST",
      headers: { "content-type": "application/json", "idempotency-key": idempotencyKey },
      body: JSON.stringify(envelope),
      signal: controller.signal,
    });
    // Only the status is logged: the URL carries the trigger token.
    if (!res.ok) console.error(`forward failed: HTTP ${res.status}`);
    else console.log(`forward ok: HTTP ${res.status} ${envelope.event}`);
  } catch (err) {
    console.error(`forward failed: ${err?.name || "error"}`);
  } finally {
    clearTimeout(timer);
  }
}

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Worker entry point; `deps` lets tests inject `fetch` and the clock. */
export async function handle(request, env, ctx, deps = {}) {
  const fetchImpl = deps.fetch || fetch;
  const now = deps.now || (() => Date.now());

  if (request.method !== "POST") return reply(405, { status: "rejected", reason: "method" });
  if (!env.SENTRY_CLIENT_SECRET || !forwardUrl(env)) {
    return reply(503, { status: "rejected", reason: "not_configured" });
  }

  const declared = Number(request.headers.get("content-length") || "0");
  if (declared > MAX_BODY_BYTES) return reply(413, { status: "rejected", reason: "too_large" });
  const body = new Uint8Array(await request.arrayBuffer());
  if (body.byteLength > MAX_BODY_BYTES) {
    return reply(413, { status: "rejected", reason: "too_large" });
  }

  const ok = await verifySignature(
    env.SENTRY_CLIENT_SECRET,
    body,
    request.headers.get("sentry-hook-signature"),
  );
  if (!ok) return reply(401, { status: "rejected", reason: "signature" });

  const skew = Number(env.MAX_SKEW_SECONDS || DEFAULT_MAX_SKEW_SECONDS);
  const ts = Number(request.headers.get("sentry-hook-timestamp"));
  if (!Number.isFinite(ts) || Math.abs(now() / 1000 - ts) > skew) {
    return reply(401, { status: "rejected", reason: "timestamp" });
  }

  let payload;
  try {
    payload = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return reply(400, { status: "rejected", reason: "json" });
  }

  const requestId = request.headers.get("request-id") || "";
  const result = buildForward(request.headers.get("sentry-hook-resource"), payload, env, requestId);
  if (result.ignore) return reply(200, { status: "ignored", reason: result.ignore });

  const idempotencyKey = /^[0-9A-Za-z-]{1,64}$/.test(requestId)
    ? `sentry-${requestId}`
    : `sentry-body-${await sha256Hex(body)}`;
  // Sentry gives up after ~1s, so the forward continues in the background; a
  // lost forward is picked up by the dispatcher's scheduled reconciliation.
  const pending = forward(env, result.forward, idempotencyKey, fetchImpl);
  if (ctx?.waitUntil) ctx.waitUntil(pending);
  else await pending;
  return reply(202, { status: "accepted", event: result.forward.event });
}

export default {
  fetch(request, env, ctx) {
    return handle(request, env, ctx);
  },
};
