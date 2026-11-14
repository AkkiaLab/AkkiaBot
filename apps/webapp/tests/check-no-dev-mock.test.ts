import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

const SCRIPT = join(import.meta.dirname, "../scripts/check-no-dev-mock.sh");
const dirs: string[] = [];

function fakeDist(js: string): string {
  const dist = mkdtempSync(join(tmpdir(), "webapp-dist-"));
  dirs.push(dist);
  mkdirSync(join(dist, "assets"));
  writeFileSync(join(dist, "index.html"), "<!doctype html>");
  writeFileSync(join(dist, "assets", "index.js"), js);
  return dist;
}

function run(dist: string): number {
  try {
    execFileSync("bash", [SCRIPT, dist], { stdio: "pipe" });
    return 0;
  } catch (error) {
    return (error as { status: number }).status;
  }
}

afterEach(() => {
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe("scripts/check-no-dev-mock.sh", () => {
  it("passes a bundle without the dev mock", () => {
    expect(run(fakeDist("console.log('captcha')"))).toBe(0);
  });

  it.each(["akkiabot-dev-telegram-mock", "mockTelegramEnv(", "installDevTelegramMock"])(
    "fails when the bundle contains %s",
    (needle) => {
      expect(run(fakeDist(`var x = ${JSON.stringify(needle)};`))).toBe(1);
    },
  );

  it("fails when there is no build output", () => {
    const dist = mkdtempSync(join(tmpdir(), "webapp-dist-"));
    dirs.push(dist);
    expect(run(dist)).toBe(2);
  });
});
