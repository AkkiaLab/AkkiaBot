import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createApiClient, type FetchFn, verifyCaptcha } from "../src/api/client";
import type { CaptchaEventRequest } from "../src/api/types";
import type { CaptchaPageProps } from "../src/pages/captcha/CaptchaPage";
import { CaptchaPage, outcomeOf } from "../src/pages/captcha/CaptchaPage";
import { fetchSequence, jsonResponse } from "./fetch";

// Stand-in for the Cloudflare widget: one button per callback.
vi.mock("@marsidev/react-turnstile", () => ({
  Turnstile: (props: {
    onSuccess: (token: string) => void;
    onError: () => void;
    onExpire: () => void;
  }) => (
    <div>
      <button type="button" onClick={() => props.onSuccess("turnstile-token")}>
        mock-turnstile-pass
      </button>
      <button type="button" onClick={() => props.onError()}>
        mock-turnstile-error
      </button>
      <button type="button" onClick={() => props.onExpire()}>
        mock-turnstile-expire
      </button>
    </div>
  ),
}));

const CHALLENGE = "4f1c2f43-5a0e-4c41-9a7a-0d0b6f1d9a11";
const INIT_DATA = "user=%7B%22id%22%3A1%7D&auth_date=1&hash=x";

let close: ReturnType<typeof vi.fn<() => void>>;

beforeEach(() => {
  close = vi.fn<() => void>();
});

function renderPage(fetch: FetchFn, overrides: Partial<CaptchaPageProps> = {}) {
  const client = createApiClient("https://api.example.test", fetch);
  return render(
    <CaptchaPage
      challengeId={CHALLENGE}
      session={{ initData: INIT_DATA, close }}
      turnstileSiteKey="site-key"
      verify={(body) => verifyCaptcha(client, body)}
      closeDelayMs={10}
      {...overrides}
    />,
  );
}

function passTurnstile() {
  fireEvent.click(screen.getByRole("button", { name: "mock-turnstile-pass" }));
}

describe("CaptchaPage submit flow", () => {
  it("submits challenge, initData and token, shows success and closes the Mini App", async () => {
    const fetch = fetchSequence(
      jsonResponse(200, { status: "passed", message: "验证成功，你已被解除禁言。" }),
    );
    renderPage(fetch);

    passTurnstile();

    expect(await screen.findByText("验证成功，你已被解除禁言。")).toBeInTheDocument();
    expect(await fetch.mock.calls[0]?.[0].json()).toEqual({
      challenge_id: CHALLENGE,
      init_data: INIT_DATA,
      turnstile_token: "turnstile-token",
      attempt: 1,
    });
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
  });

  it("shows a retryable failure and succeeds with a fresh Turnstile token", async () => {
    const fetch = fetchSequence(
      jsonResponse(400, { error: "turnstile_failed", message: "人机验证未通过，请重试。" }),
      jsonResponse(200, { status: "passed", message: "验证成功" }),
    );
    renderPage(fetch);

    passTurnstile();
    expect(await screen.findByRole("alert")).toHaveTextContent("人机验证未通过，请重试。");
    expect(close).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    passTurnstile();

    expect(await screen.findByText("验证成功")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("shows the timeout state without a retry button", async () => {
    const message = "验证已超时。请联系群管理员，或退群后重新加入以获取新的验证。";
    renderPage(fetchSequence(jsonResponse(410, { error: "challenge_expired", message })));

    passTurnstile();

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it.each([
    ["wrong_user", 403, "这个验证不属于你。"],
    ["init_data_expired", 401, "页面已过期，请从机器人私聊中重新打开验证页面。"],
    ["challenge_used", 409, "这次验证已经完成，无需重复提交。"],
  ])("shows non-retryable rejection %s", async (code, status, message) => {
    renderPage(fetchSequence(jsonResponse(status, { error: code, message })));

    passTurnstile();

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });

  it("offers a retry after a network error", async () => {
    renderPage(fetchSequence(new TypeError("Failed to fetch")));

    passTurnstile();

    expect(await screen.findByRole("alert")).toHaveTextContent("网络连接失败");
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });

  it("shows the HTTP status of unexpected responses", async () => {
    renderPage(fetchSequence(new Response("bad gateway", { status: 502 })));

    passTurnstile();

    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 502");
  });

  it("lets the user retry when the Turnstile widget fails", async () => {
    const fetch = fetchSequence();
    renderPage(fetch);

    fireEvent.click(screen.getByRole("button", { name: "mock-turnstile-error" }));

    expect(screen.getByRole("alert")).toHaveTextContent("人机验证组件加载失败");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(screen.getByRole("button", { name: "mock-turnstile-pass" })).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("shows a submitting state while the request is in flight", async () => {
    let resolve: (response: Response) => void = () => {};
    const fetch = vi.fn((_request: Request) => new Promise<Response>((r) => (resolve = r)));
    renderPage(fetch);

    passTurnstile();
    expect(await screen.findByText("正在提交验证…")).toBeInTheDocument();

    await act(async () => resolve(jsonResponse(200, { status: "passed", message: "ok" })));
    expect(await screen.findByText("ok")).toBeInTheDocument();
  });
});

describe("CaptchaPage preconditions", () => {
  it.each([
    [null, "验证链接无效"],
    ["not-a-uuid", "验证链接无效"],
  ])("rejects challenge %s before rendering Turnstile", (challengeId, text) => {
    renderPage(fetchSequence(), { challengeId });
    expect(screen.getByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByRole("button", { name: "mock-turnstile-pass" })).not.toBeInTheDocument();
  });

  it("asks to open the page from Telegram when there is no Telegram session", () => {
    renderPage(fetchSequence(), { session: null });
    expect(screen.getByRole("alert")).toHaveTextContent("请在 Telegram 中");
  });

  it("refuses to render without a Turnstile site key", () => {
    renderPage(fetchSequence(), { turnstileSiteKey: undefined });
    expect(screen.getByRole("alert")).toHaveTextContent("配置不完整");
  });
});

describe("CaptchaPage telemetry", () => {
  const PAGE = "9b2e4c1a-7d3f-4e8a-b5c6-1f2a3b4c5d6e";
  const session = () => ({ initData: INIT_DATA, close, platform: "android", version: "8.0" });

  function renderWithEvents(fetch: FetchFn, overrides: Partial<CaptchaPageProps> = {}) {
    const events: CaptchaEventRequest[] = [];
    renderPage(fetch, {
      session: session(),
      pageId: PAGE,
      report: (body) => events.push(body),
      ...overrides,
    });
    return events;
  }

  const names = (events: CaptchaEventRequest[]) =>
    events.map((e) => (e.outcome ? `${e.event}:${e.outcome}` : e.event));

  it("reports the funnel and tags the verify request with the page", async () => {
    const fetch = fetchSequence(
      jsonResponse(400, { error: "turnstile_failed", message: "人机验证未通过，请重试。" }),
      jsonResponse(200, { status: "passed", message: "验证成功" }),
    );
    const events = renderWithEvents(fetch);

    passTurnstile();
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    passTurnstile();
    await screen.findByText("验证成功");
    await waitFor(() => expect(close).toHaveBeenCalledOnce());

    expect(names(events)).toEqual([
      "page_loaded",
      "turnstile_solved",
      "submitted",
      "result:turnstile_failed",
      "retry",
      "turnstile_solved",
      "submitted",
      "result:passed",
      "closed",
    ]);
    expect(events[0]).toEqual({
      challenge_id: CHALLENGE,
      init_data: INIT_DATA,
      event: "page_loaded",
      attempt: 0,
      page_id: PAGE,
      client_platform: "android",
      client_version: "8.0",
    });
    expect(events.map((e) => e.attempt)).toEqual([0, 0, 1, 1, 1, 1, 2, 2, 2]);
    const second = await fetch.mock.calls[1]?.[0].json();
    expect(second).toMatchObject({ attempt: 2, page_id: PAGE, client_platform: "android" });
  });

  it("reports widget errors and expiry separately", () => {
    const events = renderWithEvents(fetchSequence());
    fireEvent.click(screen.getByRole("button", { name: "mock-turnstile-error" }));
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    fireEvent.click(screen.getByRole("button", { name: "mock-turnstile-expire" }));
    expect(names(events)).toEqual(["page_loaded", "turnstile_error", "retry", "turnstile_expired"]);
  });

  it("reports closing once, whether hidden or closed by the button", async () => {
    const message = "这次验证已经完成，无需重复提交。";
    const events = renderWithEvents(
      fetchSequence(jsonResponse(409, { error: "challenge_used", message })),
    );
    passTurnstile();
    await screen.findByRole("alert");
    window.dispatchEvent(new Event("pagehide"));
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(names(events).filter((n) => n === "closed")).toHaveLength(1);
    expect(close).toHaveBeenCalledOnce();
  });

  it("reports a blocked page, and nothing without initData or a valid challenge", () => {
    const blocked = renderWithEvents(fetchSequence(), { turnstileSiteKey: undefined });
    expect(names(blocked)).toEqual(["page_blocked"]);

    const noSession = renderWithEvents(fetchSequence(), { session: null });
    const badChallenge = renderWithEvents(fetchSequence(), { challengeId: "nope" });
    expect([...noSession, ...badChallenge]).toEqual([]);
  });

  it("leaves out page fields the API would refuse", async () => {
    const fetch = fetchSequence(jsonResponse(200, { status: "passed", message: "ok" }));
    const events = renderWithEvents(fetch, {
      session: { initData: INIT_DATA, close, platform: "Weird Platform", version: "8.0-beta" },
      pageId: "not-a-uuid",
    });
    passTurnstile();
    await screen.findByText("ok");
    expect(events[0]).toEqual({
      challenge_id: CHALLENGE,
      init_data: INIT_DATA,
      event: "page_loaded",
      attempt: 0,
    });
    expect(await fetch.mock.calls[0]?.[0].json()).toEqual({
      challenge_id: CHALLENGE,
      init_data: INIT_DATA,
      turnstile_token: "turnstile-token",
      attempt: 1,
    });
  });

  it.each([
    [{ ok: true, message: "ok" }, "passed"],
    [{ ok: false, kind: "network" }, "network_error"],
    [{ ok: false, kind: "unexpected", status: 502 }, "unexpected_response"],
    [{ ok: false, kind: "rejected", code: "wrong_user", message: "x", status: 403 }, "wrong_user"],
  ] as const)("maps %o to outcome %s", (result, outcome) => {
    expect(outcomeOf(result)).toBe(outcome);
  });
});
