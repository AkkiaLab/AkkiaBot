import { useMemo } from "react";
import { createApiClient, verifyCaptcha } from "./api/client";
import { readConfig } from "./env";
import { CaptchaPage } from "./pages/captcha/CaptchaPage";
import { initTelegram } from "./telegram";

export interface AppProps {
  /** Current location; injectable for tests. */
  location?: Pick<Location, "pathname" | "search">;
}

export function App({ location = window.location }: AppProps) {
  const path = location.pathname.replace(/\/+$/, "") || "/";
  if (path === "/captcha") {
    const challengeId = new URLSearchParams(location.search).get("challenge");
    return <CaptchaRoute challengeId={challengeId} />;
  }
  return <NotFound />;
}

function CaptchaRoute({ challengeId }: { challengeId: string | null }) {
  const config = useMemo(() => readConfig(), []);
  const session = useMemo(() => initTelegram(), []);
  const verify = useMemo(() => {
    const client = createApiClient(config.apiBaseUrl);
    return (body: Parameters<typeof verifyCaptcha>[1]) => verifyCaptcha(client, body);
  }, [config.apiBaseUrl]);
  return (
    <CaptchaPage
      challengeId={challengeId}
      session={session}
      turnstileSiteKey={config.turnstileSiteKey}
      verify={verify}
    />
  );
}

function NotFound() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-2 p-6 text-center">
      <h1 className="text-2xl font-semibold">AkkiaBot</h1>
      <p className="text-sm opacity-70">页面不存在。请通过机器人发送的按钮打开。</p>
    </main>
  );
}
