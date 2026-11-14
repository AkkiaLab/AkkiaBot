import { describe, expect, it } from "vitest";
import { readConfig } from "../src/env";

function env(values: Partial<ImportMetaEnv>): ImportMetaEnv {
  return { BASE_URL: "/", MODE: "test", DEV: false, PROD: true, SSR: false, ...values };
}

describe("readConfig", () => {
  it("uses the build-time API base URL without a trailing slash", () => {
    const config = readConfig(env({ VITE_API_BASE_URL: "https://bot-api.example.test/" }));
    expect(config.apiBaseUrl).toBe("https://bot-api.example.test");
  });

  it("never falls back to the Turnstile test key in production builds", () => {
    expect(readConfig(env({})).turnstileSiteKey).toBeUndefined();
    expect(readConfig(env({ VITE_TURNSTILE_SITE_KEY: " site-key " })).turnstileSiteKey).toBe(
      "site-key",
    );
  });

  it("falls back to Cloudflare's always-pass test key on the dev server", () => {
    const config = readConfig(env({ DEV: true, PROD: false }));
    expect(config.turnstileSiteKey).toBe("1x00000000000000000000AA");
  });
});
