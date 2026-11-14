// Thin wrapper over @telegram-apps/sdk-react so pages stay testable.
import {
  bindThemeParamsCssVars,
  closeMiniApp,
  init,
  isThemeParamsCssVarsBound,
  isTMA,
  miniAppReady,
  mountMiniAppSync,
  mountThemeParamsSync,
  retrieveRawInitData,
} from "@telegram-apps/sdk-react";

export interface TelegramSession {
  /** Raw `initData` query string, sent to the backend as-is. */
  initData: string;
  close: () => void;
}

// The SDK is process-global (event listeners, bound CSS variables), so it is
// initialised at most once per page load. React may call initTelegram() more
// than once (StrictMode double-invokes useMemo/effects in development);
// repeated calls return the cached result instead of re-binding, which would
// throw CSSVarsBoundError.
let cached: { session: TelegramSession | null } | undefined;

/**
 * Initialise the SDK when running inside Telegram. Returns `null` outside a
 * Telegram client (e.g. the page opened in a normal browser in production).
 * Idempotent: later calls return the first result.
 */
export function initTelegram(): TelegramSession | null {
  cached ??= { session: setUp() };
  return cached.session;
}

function setUp(): TelegramSession | null {
  if (!isTMA()) return null;
  init();
  if (mountThemeParamsSync.isAvailable()) {
    mountThemeParamsSync();
    if (bindThemeParamsCssVars.isAvailable() && !isThemeParamsCssVarsBound()) {
      bindThemeParamsCssVars();
    }
  }
  if (mountMiniAppSync.isAvailable()) mountMiniAppSync();
  if (miniAppReady.isAvailable()) miniAppReady();

  const initData = retrieveRawInitData();
  if (!initData) return null;
  return {
    initData,
    close: () => {
      if (closeMiniApp.isAvailable()) closeMiniApp();
    },
  };
}
