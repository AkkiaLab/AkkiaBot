import { useMemo } from "react";
import { createApiClient, reportCaptchaEvent, verifyCaptcha } from "./api/client";
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
  // One id per page load: tells a resubmission apart from a reopened page.
  const pageId = useMemo(() => newPageId(), []);
  const api = useMemo(() => {
    const client = createApiClient(config.apiBaseUrl);
    return {
      verify: (body: Parameters<typeof verifyCaptcha>[1]) => verifyCaptcha(client, body),
      report: (body: Parameters<typeof reportCaptchaEvent>[1]) => reportCaptchaEvent(client, body),
    };
  }, [config.apiBaseUrl]);
  return (
    <CaptchaPage
      challengeId={challengeId}
      session={session}
      turnstileSiteKey={config.turnstileSiteKey}
      verify={api.verify}
      report={api.report}
      pageId={pageId}
    />
  );
}

function newPageId(): string | undefined {
  // randomUUID needs a secure context; the Mini App is always served over HTTPS.
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : undefined;
}

function NotFound() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-2 p-6 text-center">
      <h1 className="text-2xl font-semibold">AkkiaBot</h1>
      <p className="text-sm opacity-70">页面不存在。请通过机器人发送的按钮打开。</p>
    </main>
  );
}
