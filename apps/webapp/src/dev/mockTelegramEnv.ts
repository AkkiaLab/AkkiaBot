// DEV-ONLY: imitate a Telegram client so the page can be opened in a normal
// browser via `pnpm dev`. Loaded exclusively behind `import.meta.env.DEV` in
// main.tsx; production bundles must not contain this module (CI greps for the
// marker below, see scripts/check-no-dev-mock.sh).
import { isTMA, mockTelegramEnv } from "@telegram-apps/sdk-react";

export const DEV_MOCK_MARKER = "akkiabot-dev-telegram-mock";

export function installDevTelegramMock(): void {
  if (isTMA()) return;
  // Unsigned sample payload: the backend rejects it as init_data_invalid,
  // which is exactly what the dev server should exercise.
  const initData = new URLSearchParams([
    [
      "user",
      JSON.stringify({ id: 10001, first_name: "Dev", last_name: "Mock", language_code: "zh" }),
    ],
    ["auth_date", String(Math.floor(Date.now() / 1000))],
    ["hash", "dev-mock-unsigned"],
    ["signature", DEV_MOCK_MARKER],
  ]).toString();

  mockTelegramEnv({
    launchParams: {
      tgWebAppThemeParams: {
        bg_color: "#17212b",
        text_color: "#f5f5f5",
        hint_color: "#708499",
        button_color: "#5288c1",
        button_text_color: "#ffffff",
        destructive_text_color: "#ec3942",
      },
      tgWebAppData: initData,
      tgWebAppVersion: "8.0",
      tgWebAppPlatform: "tdesktop",
    },
    onEvent: (event) => {
      console.info(`[${DEV_MOCK_MARKER}]`, event[0]);
    },
  });
}
