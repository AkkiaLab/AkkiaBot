"""BackgroundTasks and building the classifier from settings."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from akkiabot_antispam import LangfuseTracer, NullTracer
from akkiabot_antispam.testing import FakeLlm, RecordingTracer
from akkiabot_server.services.background import BackgroundTasks
from akkiabot_server.services.llm import build_llm, build_tracer

from .conftest import MakeSettings


async def test_background_tasks_run_and_drain() -> None:
    tasks = BackgroundTasks()
    done: list[str] = []

    async def child() -> None:
        await asyncio.sleep(0)
        done.append("child")

    async def parent() -> None:
        tasks.spawn(child(), name="child")
        done.append("parent")

    tasks.spawn(parent(), name="parent")
    assert len(tasks) == 1
    await tasks.drain()
    assert done == ["parent", "child"]
    assert len(tasks) == 0


async def test_background_failures_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    tasks = BackgroundTasks()

    async def boom() -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        tasks.spawn(boom(), name="boom")
        await tasks.drain()
    assert any(r.message == "background task failed" for r in caplog.records)


async def test_background_close_cancels_and_refuses() -> None:
    tasks = BackgroundTasks()
    task = tasks.spawn(asyncio.sleep(60), name="slow")
    await asyncio.sleep(0)
    await tasks.aclose()
    assert task is not None
    assert task.cancelled()

    async def late() -> None:
        raise AssertionError("must not run")

    assert tasks.spawn(late(), name="late") is None


async def test_llm_disabled_without_model_or_key(make_settings: MakeSettings) -> None:
    assert build_llm(make_settings()).classifier is None
    assert build_llm(make_settings(llm={"model": "m"})).classifier is None
    assert build_llm(make_settings(llm={"api_key": "fake-api-key-for-tests"})).classifier is None


async def test_llm_enabled(make_settings: MakeSettings) -> None:
    settings = make_settings(
        llm={"model": "m", "api_key": "fake-api-key-for-tests", "base_url": "https://llm.test/v1"},
        antispam={"structured_output": "off", "timeout": 3},
    )
    components = build_llm(settings)
    classifier = components.classifier
    assert classifier is not None
    assert classifier.settings.model == "m"
    assert classifier.settings.timeout == 3
    assert not classifier.structured
    assert isinstance(components.tracer, NullTracer)
    await components.aclose()


async def test_prompt_file_override(make_settings: MakeSettings, tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("你是群管助手。\n", encoding="utf-8")
    fake = FakeLlm()
    tracer = RecordingTracer()
    components = build_llm(
        make_settings(
            llm={"model": "m"},
            antispam={"prompt_file": str(prompt), "prompt_version": "v2"},
        ),
        client=fake.client(),
        tracer=tracer,
    )
    assert components.classifier is not None
    verdict = await components.classifier.classify("hello")
    assert verdict.prompt_version == "v2"
    assert fake.last()["messages"][0]["content"].startswith("你是群管助手。")
    await components.aclose()
    assert tracer.closed


def test_missing_prompt_file_fails_at_startup(make_settings: MakeSettings, tmp_path: Path) -> None:
    settings = make_settings(llm={"model": "m"}, antispam={"prompt_file": str(tmp_path / "x")})
    with pytest.raises(FileNotFoundError):
        build_llm(settings, client=FakeLlm().client())


def test_langfuse_tracer_only_with_both_keys(
    make_settings: MakeSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, Any]] = []

    def fake_create(**kwargs: Any) -> str:
        created.append(kwargs)
        return "tracer"

    monkeypatch.setattr(LangfuseTracer, "create", staticmethod(fake_create))
    assert isinstance(build_tracer(make_settings()), NullTracer)
    settings = make_settings().model_copy(
        update={
            "langfuse": make_settings().langfuse.model_validate(
                {
                    "public_key": "fake-langfuse-public-for-tests",
                    "secret_key": "fake-langfuse-secret-for-tests",
                    "host": "https://langfuse.example.test/",
                }
            )
        }
    )
    assert build_tracer(settings, release="1.0") == "tracer"
    assert created == [
        {
            "public_key": "fake-langfuse-public-for-tests",
            "secret_key": "fake-langfuse-secret-for-tests",
            "host": "https://langfuse.example.test",
            "environment": "development",
            "release": "1.0",
        }
    ]
