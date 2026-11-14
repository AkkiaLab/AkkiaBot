import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "../src/App";

describe("App routing", () => {
  it("renders the captcha page on /captcha (outside Telegram it explains how to open it)", () => {
    render(
      <App
        location={{
          pathname: "/captcha",
          search: "?challenge=4f1c2f43-5a0e-4c41-9a7a-0d0b6f1d9a11",
        }}
      />,
    );
    expect(screen.getByRole("heading", { name: "入群验证" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("请在 Telegram 中");
  });

  it("accepts a trailing slash on /captcha/", () => {
    render(<App location={{ pathname: "/captcha/", search: "" }} />);
    expect(screen.getByRole("heading", { name: "入群验证" })).toBeInTheDocument();
  });

  it("renders a not-found page for other paths", () => {
    render(<App location={{ pathname: "/", search: "" }} />);
    expect(screen.getByRole("heading", { name: "AkkiaBot" })).toBeInTheDocument();
    expect(screen.getByText(/页面不存在/)).toBeInTheDocument();
  });
});
