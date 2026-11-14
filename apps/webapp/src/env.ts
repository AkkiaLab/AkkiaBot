// Build-time configuration (Vite inlines `import.meta.env.VITE_*` during `vite build`).

/**
 * Cloudflare's public "always passes" Turnstile site key. Only used by the dev
 * server when `VITE_TURNSTILE_SITE_KEY` is unset. Behind `import.meta.env.DEV`
 * so the literal is dropped from production bundles.
 */
const DEV_TURNSTILE_SITE_KEY = import.meta.env.DEV ? "1x00000000000000000000AA" : undefined;

export interface WebAppConfig {
  /** Backend origin, e.g. `https://bot-api.akkia.moe` (no trailing slash). */
  apiBaseUrl: string;
  /** Turnstile site key (public), or `undefined` when not configured. */
  turnstileSiteKey: string | undefined;
}

function nonEmpty(value: string | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed ? trimmed : undefined;
}

export function readConfig(env: ImportMetaEnv = import.meta.env): WebAppConfig {
  const apiBaseUrl = (nonEmpty(env.VITE_API_BASE_URL) ?? "").replace(/\/+$/, "");
  const siteKey = nonEmpty(env.VITE_TURNSTILE_SITE_KEY);
  return {
    apiBaseUrl,
    turnstileSiteKey: siteKey ?? (env.DEV ? DEV_TURNSTILE_SITE_KEY : undefined),
  };
}
