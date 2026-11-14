// Regression (PR #19 review): inside a (mocked) Telegram environment, the real
// SDK must survive StrictMode double-invoking the /captcha initialisation.
// Kept in its own file because the SDK and initTelegram() are process-global.
import { render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { installDevTelegramMock } from "../src/dev/mockTelegramEnv";
import { initTelegram } from "../src/telegram";

vi.mock("@marsidev/react-turnstile", () => ({
  Turnstile: () => <div>mock-turnstile</div>,
}));

const CHALLENGE = "4f1c2f43-5a0e-4c41-9a7a-0d0b6f1d9a11";

describe("Telegram SDK initialisation", () => {
  it("renders /captcha under StrictMode with the real SDK in the dev mock environment", () => {
    vi.spyOn(console, "info").mockImplementation(() => {});
    installDevTelegramMock();

    render(
      <StrictMode>
        <App location={{ pathname: "/captcha", search: `?challenge=${CHALLENGE}` }} />
      </StrictMode>,
    );

    expect(screen.getByRole("heading", { name: "入群验证" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("mock-turnstile")).toBeInTheDocument();
  });

  it("is idempotent: repeated calls return the same session without re-binding", () => {
    const first = initTelegram();
    expect(first?.initData).toContain("auth_date=");
    expect(() => initTelegram()).not.toThrow();
    expect(initTelegram()).toBe(first);
  });
});
