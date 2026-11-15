"""Local Mini App hosting, builds and key switching for m7.captcha-test-keys (no browser)."""

from __future__ import annotations

import json
import subprocess
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.scenarios import MiniAppVisit
from akkiabot_e2e.turnstile_lab import (
    SECRET_KEY_FAIL,
    SECRET_KEY_PASS,
    SITE_KEY_FAIL,
    SITE_KEY_PASS,
    LocalMiniApps,
    LocalTurnstileLab,
    StaticSite,
    build_env,
    build_webapp,
    client_launch_url,
)


def test_client_launch_url_adds_theme_once() -> None:
    launch = "http://127.0.0.1:1/captcha?challenge=x#tgWebAppData=a%3D1&tgWebAppVersion=9.6"
    url = client_launch_url(launch)
    parts = urllib.parse.urlsplit(url)
    fragment = dict(urllib.parse.parse_qsl(parts.fragment))
    assert parts.query == "challenge=x"
    assert fragment["tgWebAppData"] == "a=1"  # Telegram's signed payload, untouched
    assert json.loads(fragment["tgWebAppThemeParams"])["bg_color"]
    assert client_launch_url(url) == url


def test_build_env_has_keys_but_no_run_secrets() -> None:
    base = {
        "PATH": "/bin",
        "HOME": "/h",
        "TELEGRAM_BOT_TOKEN": "x",
        "OP_SERVICE_ACCOUNT_TOKEN": "y",
    }
    env = build_env(base, api_base_url="http://127.0.0.1:9", site_key=SITE_KEY_PASS)
    assert env == {
        "PATH": "/bin",
        "HOME": "/h",
        "VITE_API_BASE_URL": "http://127.0.0.1:9",
        "VITE_TURNSTILE_SITE_KEY": SITE_KEY_PASS,
    }


class Recorder:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, self.returncode)


def _webapp(tmp_path: Path) -> Path:
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}")
    return webapp


def test_build_webapp_runs_vite_with_the_key(tmp_path: Path) -> None:
    runner = Recorder()
    out = tmp_path / "out"
    build_webapp(
        _webapp(tmp_path),
        out,
        api_base_url="http://127.0.0.1:9",
        site_key=SITE_KEY_FAIL,
        log_path=tmp_path / "build.log",
        base_env={"PATH": "/bin"},
        runner=runner,
    )
    ((cmd, kwargs),) = runner.calls
    assert cmd[:4] == ["pnpm", "exec", "vite", "build"]
    assert str(out) in cmd
    assert kwargs["env"]["VITE_TURNSTILE_SITE_KEY"] == SITE_KEY_FAIL
    assert (tmp_path / "build.log").stat().st_mode & 0o777 == 0o600


def test_build_webapp_failure_and_missing_sources(tmp_path: Path) -> None:
    with pytest.raises(E2EError, match="退出码 1"):
        build_webapp(
            _webapp(tmp_path),
            tmp_path / "out",
            api_base_url="http://x",
            site_key=SITE_KEY_PASS,
            log_path=tmp_path / "build.log",
            runner=Recorder(returncode=1),
        )
    with pytest.raises(E2EError, match="找不到 Mini App"):
        build_webapp(
            tmp_path / "nowhere",
            tmp_path / "out",
            api_base_url="http://x",
            site_key=SITE_KEY_PASS,
            log_path=tmp_path / "build.log",
            runner=Recorder(),
        )


async def test_static_site_serves_files_and_falls_back_to_index(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text('<div id="root"></div>')
    (tmp_path / "app.js").write_text("console.log(1)")
    with StaticSite(tmp_path) as site:
        assert site.origin.startswith("http://127.0.0.1:")
        async with httpx.AsyncClient(base_url=site.origin) as client:
            page = await client.get("/captcha?challenge=x")
            asset = await client.get("/app.js")
    assert page.status_code == 200
    assert 'id="root"' in page.text
    assert asset.text == "console.log(1)"


async def test_local_mini_apps_env_and_builds(tmp_path: Path) -> None:
    runner = Recorder()
    with LocalMiniApps(tmp_path) as apps:
        env = apps.server_env()
        assert env["WEBAPP_URL"] == apps.pass_site.origin
        assert env["CORS_ALLOW_ORIGINS"].split(",") == [
            apps.pass_site.origin,
            apps.fail_site.origin,
        ]
        assert env["TURNSTILE_SECRET_KEY"] == SECRET_KEY_FAIL
        await apps.build(_webapp(tmp_path), "http://127.0.0.1:9", runner=runner)
    keys = [kwargs["env"]["VITE_TURNSTILE_SITE_KEY"] for _, kwargs in runner.calls]
    assert keys == [SITE_KEY_PASS, SITE_KEY_FAIL]


class FakeServer:
    def __init__(self) -> None:
        self.restarts: list[Mapping[str, str]] = []

    async def restart(self, overrides: Mapping[str, str]) -> None:
        self.restarts.append(dict(overrides))


class FakeBrowser:
    version = "Chromium test"

    async def visit(self, url: str) -> MiniAppVisit:
        return MiniAppVisit("timeout", url)


async def test_lab_switches_secret_and_describes_keys(tmp_path: Path) -> None:
    server = FakeServer()
    with LocalMiniApps(tmp_path) as apps:
        lab = LocalTurnstileLab(server, apps, FakeBrowser())
        assert lab.siteverify == "fail"
        await lab.use_secret("pass")
        assert lab.siteverify == "pass"
        assert server.restarts == [{"TURNSTILE_SECRET_KEY": SECRET_KEY_PASS}]
        text = "\n".join(lab.describe())
        assert (await lab.visit("http://u")).text == "http://u"
    for key in (SITE_KEY_PASS, SITE_KEY_FAIL, SECRET_KEY_PASS, SECRET_KEY_FAIL, "Chromium test"):
        assert key in text
