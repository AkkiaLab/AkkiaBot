import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

async function bootstrap(): Promise<void> {
  // Telegram environment simulation is a dev-server convenience only. Vite
  // replaces `import.meta.env.DEV` with `false` in `vite build`, so the dynamic
  // import (and the mock module) is dropped from production bundles.
  if (import.meta.env.DEV) {
    const { installDevTelegramMock } = await import("./dev/mockTelegramEnv");
    installDevTelegramMock();
  }

  const root = document.getElementById("root");
  if (!root) {
    throw new Error("#root element not found");
  }

  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void bootstrap();
