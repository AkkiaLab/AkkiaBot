"""Local Mini App with Cloudflare's official Turnstile test keys (``m7.captcha-test-keys``).

Only for ``run --serve``: the bot under test runs from this checkout, the Mini
App is built from ``apps/webapp`` twice (always-pass and always-fail site key)
and served on loopback, and a headless Chromium opens the launch URL Telegram
signed for the member account, so the real Turnstile widget, the real API and
the real Siteverify endpoint take part. Nothing here touches staging or
production configuration.

Test keys: https://developers.cloudflare.com/turnstile/troubleshooting/testing/
(public values that work on any hostname; test secret keys accept only the
dummy token ``XXXX.DUMMY.TOKEN.XXXX`` the test site keys produce).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import json
import os
import subprocess
import threading
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self

from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.scenarios import MiniAppVisit

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright, Response

# Cloudflare's documented test keys: public, not credentials, usable on any host.
SITE_KEY_PASS = "1x00000000000000000000AA"  # noqa: S105 - Cloudflare public test site key
"""Visible widget, always passes."""
SITE_KEY_FAIL = "2x00000000000000000000AB"
"""Visible widget, always fails."""
SECRET_KEY_PASS = "1x0000000000000000000000000000000AA"  # noqa: S105 - Cloudflare's public test key
"""Siteverify always passes (dummy token only)."""
SECRET_KEY_FAIL = "2x0000000000000000000000000000000AA"  # noqa: S105 - Cloudflare's public test key
"""Siteverify always fails."""
SECRET_KEYS: Mapping[str, str] = {"pass": SECRET_KEY_PASS, "fail": SECRET_KEY_FAIL}

VERIFY_PATH = "/api/v1/captcha/verify"
PASSED_HINT = "页面即将自动关闭"
"""Shown by the captcha page only after a successful verification."""
#: Environment the Mini App build may see: no secrets of the run.
_BUILD_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CACHE_HOME", "PNPM_HOME")

BROWSER_SETUP_HINT = (
    "未能启动无头 Chromium。先执行 `just e2e-browser-setup`（下载 Playwright 的 "
    "chromium-headless-shell，并用 sudo 安装其系统依赖）"
)


# ------------------------------------------------------------------ build


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def build_command(out_dir: Path) -> list[str]:
    # `vite build` only: the type check is part of `just typecheck`, not of the run.
    return ["pnpm", "exec", "vite", "build", "--outDir", str(out_dir), "--emptyOutDir"]


def build_env(base: Mapping[str, str], *, api_base_url: str, site_key: str) -> dict[str, str]:
    """Minimal environment for ``vite build``: never the run's secrets.

    Variables already in the environment win over the repository ``.env``
    (Vite's precedence), so a local ``.env`` cannot change the keys.
    """
    env = {k: base[k] for k in _BUILD_ENV_KEYS if k in base}
    env.update(VITE_API_BASE_URL=api_base_url, VITE_TURNSTILE_SITE_KEY=site_key)
    return env


def build_webapp(
    webapp_dir: Path,
    out_dir: Path,
    *,
    api_base_url: str,
    site_key: str,
    log_path: Path,
    base_env: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> None:
    if not (webapp_dir / "package.json").is_file():
        raise E2EError(f"找不到 Mini App 源码 {webapp_dir}；请在仓库根目录运行")
    env = build_env(
        os.environ if base_env is None else base_env, api_base_url=api_base_url, site_key=site_key
    )
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as log:
        result = runner(
            build_command(out_dir),
            cwd=webapp_dir,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        raise E2EError(
            f"构建本地 Mini App 失败（退出码 {result.returncode}），见 {log_path}；"
            "缺少前端依赖时先执行 `just setup`"
        )


# ------------------------------------------------------------------ static hosting


class _SpaHandler(http.server.SimpleHTTPRequestHandler):
    """Static files; unknown paths (``/captcha``) get ``index.html`` like Pages does."""

    def send_head(self) -> Any:
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if not Path(self.translate_path(path)).is_file():
            self.path = "/index.html"
        return super().send_head()

    def log_message(self, format: str, *args: Any) -> None:
        return


class StaticSite:
    """A directory served on ``127.0.0.1:<random port>`` from a background thread."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        handler = functools.partial(_SpaHandler, directory=str(directory))
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()


# ------------------------------------------------------------------ browser


#: Theme a Telegram client adds to the launch URL (``tgWebAppThemeParams``);
#: the Mini App SDK refuses launch parameters without it.
CLIENT_THEME = {"bg_color": "#ffffff", "text_color": "#000000", "button_color": "#2481cc"}
#: Stand-in for the webview bridge a Telegram client injects
#: (``window.TelegramWebviewProxy``); records the events the page posts.
_CLIENT_BRIDGE = """
window.__tgEvents = [];
window.TelegramWebviewProxy = { postEvent: (type) => window.__tgEvents.push(type) };
"""


def client_launch_url(launch: str) -> str:
    """``launch`` as a Telegram client opens it: Telegram's server returns the
    signed ``tgWebAppData``; the client appends its theme to the fragment."""
    parts = urllib.parse.urlsplit(launch)
    fragment = urllib.parse.parse_qsl(parts.fragment, keep_blank_values=True)
    if not any(key == "tgWebAppThemeParams" for key, _ in fragment):
        fragment.append(("tgWebAppThemeParams", json.dumps(CLIENT_THEME, separators=(",", ":"))))
    return urllib.parse.urlunsplit(parts._replace(fragment=urllib.parse.urlencode(fragment)))


class MiniAppBrowser(Protocol):
    version: str

    async def visit(self, url: str) -> MiniAppVisit: ...


_FINAL_STATE = f"""() => document.querySelector('[role="alert"]')
    || document.body.innerText.includes({PASSED_HINT!r})"""


class PlaywrightMiniApp:
    """Headless Chromium; every visit gets a fresh browser context (no shared storage)."""

    def __init__(self, *, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self.version = ""
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> Self:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - a declared dependency
            raise E2EError(BROWSER_SETUP_HINT) from exc
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch()
        except Exception as exc:
            await self._playwright.stop()
            raise E2EError(f"{BROWSER_SETUP_HINT}：{type(exc).__name__}") from exc
        self.version = f"Chromium {self._browser.version}"
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def visit(self, url: str) -> MiniAppVisit:
        if self._browser is None:
            raise E2EError("浏览器尚未启动")
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        context = await self._browser.new_context()
        await context.add_init_script(_CLIENT_BRIDGE)
        try:
            page = await context.new_page()
            responses: list[Response] = []

            def on_response(response: Response) -> None:
                if response.request.method == "POST" and response.url.endswith(VERIFY_PATH):
                    responses.append(response)

            page.on("response", on_response)
            await page.goto(client_launch_url(url))
            try:
                await page.wait_for_function(_FINAL_STATE, timeout=self.timeout * 1000)
            except PlaywrightTimeoutError:
                text = (await page.inner_text("body"))[:300]
                return MiniAppVisit("timeout", text, await _verify_calls(responses), ())
            alert = page.locator('[role="alert"]')
            if await alert.count():
                phase: Literal["passed", "failed"] = "failed"
                text = await alert.first.inner_text()
            else:
                phase, text = "passed", await page.locator('[role="status"]').first.inner_text()
                # After a success the page closes the Mini App (1.5 s later).
                with contextlib.suppress(PlaywrightTimeoutError):
                    await page.wait_for_function(
                        "() => window.__tgEvents.includes('web_app_close')", timeout=10_000
                    )
            events = await page.evaluate("() => window.__tgEvents")
            return MiniAppVisit(
                phase,
                text,
                await _verify_calls(responses),
                tuple(str(e) for e in events) if isinstance(events, list) else (),
            )
        finally:
            await context.close()


async def _verify_calls(responses: Sequence[Response]) -> tuple[tuple[int, str | None], ...]:
    out: list[tuple[int, str | None]] = []
    for response in responses:
        error: str | None = None
        with contextlib.suppress(Exception):
            body = await response.json()
            if isinstance(body, dict):
                value = body.get("error")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                error = value if isinstance(value, str) else None
        out.append((response.status, error))
    return tuple(out)


# ------------------------------------------------------------------ the lab


class Restartable(Protocol):
    async def restart(self, overrides: Mapping[str, str]) -> None: ...


class LocalMiniApps:
    """The two local builds of the Mini App (always-pass / always-fail site key)."""

    def __init__(self, workdir: Path) -> None:
        self.pass_site = StaticSite(workdir / "webapp-sitekey-pass")
        self.fail_site = StaticSite(workdir / "webapp-sitekey-fail")
        self.log_path = workdir / "webapp-build.log"

    def __enter__(self) -> Self:
        for site in (self.pass_site, self.fail_site):
            site.directory.mkdir(mode=0o700, exist_ok=True)
            site.__enter__()
        return self

    def __exit__(self, *_: object) -> None:
        for site in (self.pass_site, self.fail_site):
            site.__exit__()

    def server_env(self) -> dict[str, str]:
        """Settings of the bot under test: Mini App URL, CORS, the always-fail secret first."""
        return {
            "WEBAPP_URL": self.pass_site.origin,
            "CORS_ALLOW_ORIGINS": f"{self.pass_site.origin},{self.fail_site.origin}",
            "TURNSTILE_SECRET_KEY": SECRET_KEY_FAIL,
        }

    async def build(
        self, webapp_dir: Path, api_base_url: str, *, runner: Runner = subprocess.run
    ) -> None:
        for site, key in ((self.pass_site, SITE_KEY_PASS), (self.fail_site, SITE_KEY_FAIL)):
            await asyncio.to_thread(
                build_webapp,
                webapp_dir,
                site.directory,
                api_base_url=api_base_url,
                site_key=key,
                log_path=self.log_path,
                runner=runner,
            )


class LocalTurnstileLab:
    """:class:`~akkiabot_e2e.scenarios.TurnstileLab` over the local builds and bot."""

    def __init__(self, server: Restartable, apps: LocalMiniApps, browser: MiniAppBrowser) -> None:
        self._server = server
        self._browser = browser
        self.pass_origin = apps.pass_site.origin
        self.widget_fail_origin = apps.fail_site.origin
        self.siteverify: Literal["pass", "fail"] = "fail"

    def describe(self) -> list[str]:
        return [
            f"site key：固定通过 `{SITE_KEY_PASS}`（{self.pass_origin}，bot 的 WEBAPP_URL）、"
            f"固定失败 `{SITE_KEY_FAIL}`（{self.widget_fail_origin}）；Mini App 由本次检出构建",
            f"secret key：先固定失败 `{SECRET_KEY_FAIL}`，再以固定通过 `{SECRET_KEY_PASS}` "
            "重启被测 bot；Siteverify 为 Cloudflare 线上接口",
            f"浏览器：{self._browser.version or '无头浏览器'}（Playwright）",
        ]

    async def use_secret(self, which: Literal["pass", "fail"]) -> None:
        await self._server.restart({"TURNSTILE_SECRET_KEY": SECRET_KEYS[which]})
        self.siteverify = which

    async def visit(self, url: str) -> MiniAppVisit:
        return await self._browser.visit(url)
