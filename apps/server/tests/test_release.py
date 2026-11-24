"""Deployment release identity: image sha (AKKIABOT_GIT_SHA) with a package-version fallback."""

from __future__ import annotations

from typing import Any

import pytest

from akkiabot_antispam import NullTracer
from akkiabot_core.cache import MemoryCache
from akkiabot_server import GIT_SHA_ENV, __version__, main, release_name, runtime
from akkiabot_server.services.llm import LlmComponents
from akkiabot_server.testing import FakeBotApi

from .conftest import MOCK_BOT_TOKEN, MakeSettings

# mock: 虚构的 40 位十六进制值，仅用于测试，不对应任何真实提交。
FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_release_uses_image_sha() -> None:
    assert release_name({GIT_SHA_ENV: FAKE_SHA}) == f"akkiabot@{FAKE_SHA}"
    assert release_name({GIT_SHA_ENV: f"  {FAKE_SHA}\n"}) == f"akkiabot@{FAKE_SHA}"


@pytest.mark.parametrize("environ", [{}, {GIT_SHA_ENV: ""}, {GIT_SHA_ENV: "  "}])
def test_release_falls_back_to_package_version(environ: dict[str, str]) -> None:
    # Local runs and images built without the build arg (ENV is then empty).
    assert release_name(environ) == f"akkiabot@{__version__}"


def test_release_reads_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GIT_SHA_ENV, FAKE_SHA)
    assert release_name() == f"akkiabot@{FAKE_SHA}"
    monkeypatch.delenv(GIT_SHA_ENV)
    assert release_name() == f"akkiabot@{__version__}"


async def test_langfuse_gets_the_same_release(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    monkeypatch.setenv(GIT_SHA_ENV, FAKE_SHA)
    seen: list[Any] = []

    def fake_build_llm(settings: Any, *, release: str | None = None) -> LlmComponents:
        seen.append(release)
        return LlmComponents(None, NullTracer())

    monkeypatch.setattr(runtime, "build_llm", fake_build_llm)
    api = FakeBotApi()
    rt = runtime.Runtime.build(
        make_settings(), cache=MemoryCache(), request=api, get_updates_request=api
    )
    await rt.http.aclose()
    await rt.engine.dispose()
    assert seen == [f"akkiabot@{FAKE_SHA}"]


def test_serve_reports_the_same_release_to_sentry(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    monkeypatch.setenv(GIT_SHA_ENV, FAKE_SHA)
    monkeypatch.setattr(main, "load_settings", make_settings)
    seen: list[str | None] = []
    scrubbed: list[tuple[str, ...]] = []

    class StopServeError(Exception):
        pass

    def fake_setup(
        settings: Any, *, release: str | None = None, secrets: tuple[str, ...] = ()
    ) -> None:
        seen.append(release)
        scrubbed.append(tuple(secrets))
        raise StopServeError  # stop before the bot / HTTP server start

    monkeypatch.setattr(main, "setup_observability", fake_setup)
    with pytest.raises(StopServeError):
        main.main(["serve"])
    assert seen == [f"akkiabot@{FAKE_SHA}"]
    # Secrets such as the bot token are masked in Sentry events.
    assert scrubbed == [make_settings().secret_values()]
    assert MOCK_BOT_TOKEN in scrubbed[0]
