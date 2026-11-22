import { Turnstile } from "@marsidev/react-turnstile";
import { useCallback, useEffect, useRef, useState } from "react";
import type { VerifyResult } from "../../api/client";
import type {
  CaptchaErrorCode,
  CaptchaEventName,
  CaptchaEventRequest,
  CaptchaOutcome,
  CaptchaVerifyRequest,
} from "../../api/types";
import type { TelegramSession } from "../../telegram";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Same patterns as the API; values that do not match are left out rather than
// letting the server refuse the whole request.
const PLATFORM_RE = /^[a-z_]{1,16}$/;
const VERSION_RE = /^[0-9]{1,2}(\.[0-9]{1,3}){0,2}$/;

/** Rejections where submitting again (with a fresh Turnstile token) can succeed. */
const RETRYABLE: ReadonlySet<CaptchaErrorCode> = new Set<CaptchaErrorCode>([
  "captcha_unavailable",
  "turnstile_failed",
  "unmute_failed",
]);

export type CaptchaState =
  | { phase: "challenge" }
  | { phase: "submitting" }
  | { phase: "passed"; message: string }
  | { phase: "expired"; message: string }
  | { phase: "failed"; message: string; retryable: boolean };

export interface CaptchaPageProps {
  /** Raw `challenge` query parameter. */
  challengeId: string | null;
  /** `null` when the page is not running inside a Telegram client. */
  session: TelegramSession | null;
  turnstileSiteKey: string | undefined;
  verify: (body: CaptchaVerifyRequest) => Promise<VerifyResult>;
  /** Delay before closing the Mini App after a successful verification. */
  closeDelayMs?: number;
  /** Random id of this page load, sent with every request (logs only). */
  pageId?: string;
  /** Lifecycle telemetry sink; must never throw. */
  report?: (body: CaptchaEventRequest) => void;
}

/** What the page showed for a verify response, as reported in `result` events. */
export function outcomeOf(result: VerifyResult): CaptchaOutcome {
  if (result.ok) return "passed";
  switch (result.kind) {
    case "rejected":
      return result.code;
    case "network":
      return "network_error";
    case "unexpected":
      return "unexpected_response";
  }
}

type PageFields = Pick<CaptchaVerifyRequest, "page_id" | "client_platform" | "client_version">;

function pageFields(pageId: string | undefined, session: TelegramSession | null): PageFields {
  const fields: PageFields = {};
  if (pageId && UUID_RE.test(pageId)) fields.page_id = pageId;
  if (session?.platform && PLATFORM_RE.test(session.platform)) {
    fields.client_platform = session.platform;
  }
  if (session?.version && VERSION_RE.test(session.version)) fields.client_version = session.version;
  return fields;
}

export function resultToState(result: VerifyResult): CaptchaState {
  if (result.ok) return { phase: "passed", message: result.message };
  switch (result.kind) {
    case "rejected":
      if (result.code === "challenge_expired") {
        return { phase: "expired", message: result.message };
      }
      return { phase: "failed", message: result.message, retryable: RETRYABLE.has(result.code) };
    case "network":
      return { phase: "failed", message: "网络连接失败，请检查网络后重试。", retryable: true };
    case "unexpected":
      return {
        phase: "failed",
        message: `服务器返回了意外的响应（HTTP ${result.status}），请稍后重试。`,
        retryable: true,
      };
  }
}

function precondition(props: CaptchaPageProps): string | null {
  if (!props.challengeId || !UUID_RE.test(props.challengeId)) {
    return "验证链接无效，请回到群里重新获取验证链接。";
  }
  if (!props.session) return "请在 Telegram 中通过机器人私聊里的按钮打开此页面。";
  if (!props.turnstileSiteKey) return "验证页面配置不完整，请联系群管理员。";
  return null;
}

export function CaptchaPage(props: CaptchaPageProps) {
  const { challengeId, session, turnstileSiteKey, verify, closeDelayMs = 1500 } = props;
  const { pageId, report } = props;
  const [state, setState] = useState<CaptchaState>({ phase: "challenge" });
  // Bumped on retry to remount the Turnstile widget and obtain a fresh token.
  const [attempt, setAttempt] = useState(0);
  // Submissions made by this page load (sent as `attempt`).
  const submissions = useRef(0);
  const loadedReported = useRef(false);
  const closedReported = useRef(false);
  const blocked = precondition(props);

  // Events need a valid challenge id and initData; without them the API
  // refuses the report anyway.
  const send = useCallback(
    (event: CaptchaEventName, outcome?: CaptchaOutcome) => {
      if (!report || !session || !challengeId || !UUID_RE.test(challengeId)) return;
      report({
        challenge_id: challengeId,
        init_data: session.initData,
        event,
        ...(outcome ? { outcome } : {}),
        attempt: submissions.current,
        ...pageFields(pageId, session),
      });
    },
    [report, session, challengeId, pageId],
  );

  const reportClosed = useCallback(() => {
    if (closedReported.current) return;
    closedReported.current = true;
    send("closed");
  }, [send]);

  const close = useCallback(() => {
    reportClosed();
    session?.close();
  }, [reportClosed, session]);

  useEffect(() => {
    if (loadedReported.current) return; // StrictMode runs effects twice
    loadedReported.current = true;
    send(blocked ? "page_blocked" : "page_loaded");
  }, [send, blocked]);

  useEffect(() => {
    // Telegram tears the webview down without a reliable unload event; the
    // first time the page is hidden counts as "closed" (best effort).
    const onHidden = () => {
      if (document.visibilityState === "hidden") reportClosed();
    };
    window.addEventListener("pagehide", reportClosed);
    document.addEventListener("visibilitychange", onHidden);
    return () => {
      window.removeEventListener("pagehide", reportClosed);
      document.removeEventListener("visibilitychange", onHidden);
    };
  }, [reportClosed]);

  const submit = useCallback(
    async (token: string) => {
      if (!session || !challengeId) return;
      send("turnstile_solved");
      submissions.current += 1;
      setState({ phase: "submitting" });
      send("submitted");
      const result = await verify({
        challenge_id: challengeId,
        init_data: session.initData,
        turnstile_token: token,
        attempt: submissions.current,
        ...pageFields(pageId, session),
      });
      send("result", outcomeOf(result));
      setState(resultToState(result));
    },
    [challengeId, session, verify, send, pageId],
  );

  useEffect(() => {
    if (state.phase !== "passed" || !session) return;
    const timer = window.setTimeout(close, closeDelayMs);
    return () => window.clearTimeout(timer);
  }, [state.phase, session, close, closeDelayMs]);

  const widgetFailed = (event: "turnstile_error" | "turnstile_expired") => {
    send(event);
    setState({
      phase: "failed",
      message: "人机验证组件加载失败，请重试。",
      retryable: true,
    });
  };

  const retry = () => {
    send("retry");
    setAttempt((n) => n + 1);
    setState({ phase: "challenge" });
  };

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-6 p-6 text-center">
      <h1 className="text-xl font-semibold">入群验证</h1>
      {blocked ? (
        <StatusMessage tone="error" text={blocked} />
      ) : (
        <Body
          state={state}
          attempt={attempt}
          siteKey={turnstileSiteKey ?? ""}
          onToken={submit}
          onWidgetError={() => widgetFailed("turnstile_error")}
          onWidgetExpire={() => widgetFailed("turnstile_expired")}
          onRetry={retry}
          onClose={session ? close : undefined}
        />
      )}
    </main>
  );
}

interface BodyProps {
  state: CaptchaState;
  attempt: number;
  siteKey: string;
  onToken: (token: string) => void;
  onWidgetError: () => void;
  onWidgetExpire: () => void;
  onRetry: () => void;
  onClose: (() => void) | undefined;
}

function Body(props: BodyProps) {
  const { state, attempt, siteKey, onToken, onWidgetError, onWidgetExpire, onRetry, onClose } =
    props;
  switch (state.phase) {
    case "challenge":
      return (
        <>
          <p className="text-sm opacity-80">
            请完成下方的人机验证，通过后会自动解除你在群里的禁言。
          </p>
          <Turnstile
            key={attempt}
            siteKey={siteKey}
            options={{ theme: "auto", language: "auto" }}
            onSuccess={onToken}
            onError={onWidgetError}
            onExpire={onWidgetExpire}
          />
        </>
      );
    case "submitting":
      return <StatusMessage tone="info" text="正在提交验证…" />;
    case "passed":
      return (
        <>
          <StatusMessage tone="success" text={state.message} />
          <p className="text-sm opacity-70">页面即将自动关闭。</p>
        </>
      );
    case "expired":
      return (
        <>
          <StatusMessage tone="error" text={state.message} />
          {onClose && <ActionButton label="关闭" onClick={onClose} />}
        </>
      );
    case "failed":
      return (
        <>
          <StatusMessage tone="error" text={state.message} />
          {state.retryable ? (
            <ActionButton label="重试" onClick={onRetry} />
          ) : (
            onClose && <ActionButton label="关闭" onClick={onClose} />
          )}
        </>
      );
  }
}

const TONE_CLASS = {
  info: "",
  success: "text-green-600 dark:text-green-400",
  error: "text-[var(--tg-theme-destructive-text-color,#dc2626)]",
} as const;

function StatusMessage({ tone, text }: { tone: keyof typeof TONE_CLASS; text: string }) {
  return (
    <p role={tone === "error" ? "alert" : "status"} className={TONE_CLASS[tone]}>
      {text}
    </p>
  );
}

function ActionButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg bg-[var(--tg-theme-button-color,#2481cc)] px-6 py-2 font-medium text-[var(--tg-theme-button-text-color,#ffffff)]"
    >
      {label}
    </button>
  );
}
