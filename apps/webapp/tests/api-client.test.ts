import { describe, expect, it, vi } from "vitest";
import { createApiClient, reportCaptchaEvent, verifyCaptcha } from "../src/api/client";
import { fetchSequence, jsonResponse } from "./fetch";

const BASE = "https://api.example.test";

const BODY = {
  challenge_id: "4f1c2f43-5a0e-4c41-9a7a-0d0b6f1d9a11",
  init_data: "user=%7B%7D&auth_date=1&hash=x",
  turnstile_token: "turnstile-token",
};

describe("verifyCaptcha", () => {
  it("POSTs the request body to /api/v1/captcha/verify on the configured base URL", async () => {
    const fetch = fetchSequence(jsonResponse(200, { status: "passed", message: "ok" }));
    const client = createApiClient(BASE, fetch);

    const result = await verifyCaptcha(client, BODY);

    expect(result).toEqual({ ok: true, message: "ok" });
    const request = fetch.mock.calls[0]?.[0];
    expect(request?.method).toBe("POST");
    expect(request?.url).toBe("https://api.example.test/api/v1/captcha/verify");
    expect(await request?.json()).toEqual(BODY);
  });

  it("maps a CaptchaError body to a rejected result", async () => {
    const fetch = fetchSequence(
      jsonResponse(410, { error: "challenge_expired", message: "验证已超时。" }),
    );
    const result = await verifyCaptcha(createApiClient(BASE, fetch), BODY);
    expect(result).toEqual({
      ok: false,
      kind: "rejected",
      code: "challenge_expired",
      message: "验证已超时。",
      status: 410,
    });
  });

  it("reports network failures without throwing", async () => {
    const fetch = fetchSequence(new TypeError("Failed to fetch"));
    expect(await verifyCaptcha(createApiClient(BASE, fetch), BODY)).toEqual({
      ok: false,
      kind: "network",
    });
  });

  it("treats non-CaptchaError failures (e.g. 422, 502 HTML) as unexpected", async () => {
    const fetch = fetchSequence(
      jsonResponse(422, { detail: [] }),
      new Response("<html>Bad gateway</html>", { status: 502 }),
    );
    const client = createApiClient(BASE, fetch);
    expect(await verifyCaptcha(client, BODY)).toEqual({
      ok: false,
      kind: "unexpected",
      status: 422,
    });
    expect(await verifyCaptcha(client, BODY)).toEqual({
      ok: false,
      kind: "unexpected",
      status: 502,
    });
  });
});

describe("reportCaptchaEvent", () => {
  const EVENT = {
    challenge_id: BODY.challenge_id,
    init_data: BODY.init_data,
    event: "page_loaded" as const,
  };

  it("POSTs the event with keepalive to /api/v1/captcha/events", async () => {
    const fetch = fetchSequence(new Response(null, { status: 204 }));
    reportCaptchaEvent(createApiClient(BASE, fetch), EVENT);

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    const request = fetch.mock.calls[0]?.[0];
    expect(request?.url).toBe("https://api.example.test/api/v1/captcha/events");
    expect(request?.keepalive).toBe(true);
    expect(await request?.json()).toEqual(EVENT);
  });

  it("swallows network failures and refusals", async () => {
    const fetch = fetchSequence(
      new TypeError("Failed to fetch"),
      jsonResponse(429, { error: "rate_limited", message: "rate_limited" }),
    );
    const client = createApiClient(BASE, fetch);
    expect(() => reportCaptchaEvent(client, EVENT)).not.toThrow();
    expect(() => reportCaptchaEvent(client, EVENT)).not.toThrow();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  });
});
