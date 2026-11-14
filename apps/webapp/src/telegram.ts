// Thin wrapper over @telegram-apps/sdk-react so pages stay testable.
import {
  bindThemeParamsCssVars,
  closeMiniApp,
  init,
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

/**
 * Initialise the SDK when running inside Telegram. Returns `null` outside a
 * Telegram client (e.g. the page opened in a normal browser in production).
 */
export function initTelegram(): TelegramSession | null {
  if (!isTMA()) return null;
  init();
  if (mountThemeParamsSync.isAvailable()) {
    mountThemeParamsSync();
    if (bindThemeParamsCssVars.isAvailable()) bindThemeParamsCssVars();
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
