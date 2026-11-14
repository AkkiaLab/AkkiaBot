"""AntiSpamClassifier against an in-memory OpenAI-compatible endpoint (FakeLlm)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest

from akkiabot_antispam import (
    AntiSpamClassifier,
    ClassifierSettings,
    Prompt,
    SpamContext,
    Usage,
    VerdictSource,
)
from akkiabot_antispam.classifier import parse_text_answer
from akkiabot_antispam.prompts import DEFAULT_PROMPT_VERSION, user_message
from akkiabot_antispam.testing import FakeLlm, completion, error


def bad_request() -> httpx2.Response:
    return error(400, "response_format is not supported")


@dataclass
class RecordedGeneration:
    start: dict[str, Any]
    end: dict[str, Any] | None = None

    def end_(self, **kwargs: Any) -> None:
        self.end = kwargs


@dataclass
class FakeTracer:
    generations: list[RecordedGeneration] = field(default_factory=list)

    def start(self, **kwargs: Any) -> Any:
        generation = RecordedGeneration(kwargs)
        self.generations.append(generation)

        class _Handle:
            def end(self, **end_kwargs: Any) -> None:
                generation.end_(**end_kwargs)

        return _Handle()

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


@pytest.fixture
def llm() -> FakeLlm:
    return FakeLlm()


def make(
    llm: FakeLlm,
    *,
    mode: str = "auto",
    tracer: FakeTracer | None = None,
    prompt: Prompt | None = None,
) -> AntiSpamClassifier:
    return AntiSpamClassifier(
        llm.client(),
        ClassifierSettings(model="test-model", timeout=5, structured_output=mode),  # pyright: ignore[reportArgumentType]
        prompt=prompt,
        tracer=tracer,
    )


def sent(llm: FakeLlm, index: int = -1) -> dict[str, Any]:
    return llm.requests[index]


# ------------------------------------------------------------ structured output


async def test_structured_output_spam(llm: FakeLlm) -> None:
    llm.queue(completion('{"is_spam": true, "category": "gambling", "confidence": 0.93}'))
    tracer = FakeTracer()
    verdict = await make(llm, tracer=tracer).classify("注册即送888彩金")

    assert verdict.is_spam
    assert (verdict.category, verdict.confidence) == ("gambling", 0.93)
    assert verdict.source is VerdictSource.STRUCTURED
    assert verdict.prompt_version == DEFAULT_PROMPT_VERSION
    request = sent(llm)
    assert request["model"] == "test-model"
    assert request["response_format"]["type"] == "json_schema"
    assert request["messages"][1] == {"role": "user", "content": "注册即送888彩金"}
    assert "JSON" in request["messages"][0]["content"]

    (generation,) = tracer.generations
    assert generation.start["prompt_version"] == DEFAULT_PROMPT_VERSION
    assert generation.start["model"] == "test-model"
    assert generation.start["input"] == request["messages"]
    assert generation.end is not None
    assert generation.end["usage"] == Usage(input_tokens=120, output_tokens=8, total_tokens=128)
    assert generation.end["output"].startswith('{"is_spam": true')
    assert "error" not in generation.end


async def test_structured_output_not_spam_in_code_fence(llm: FakeLlm) -> None:
    llm.queue(
        completion(
            '```json\n{"is_spam": false, "category": "none", "confidence": 0.8}\n```', usage=False
        )
    )
    verdict = await make(llm).classify("今天天气真好啊")
    assert not verdict.is_spam
    assert (verdict.category, verdict.source) == ("none", VerdictSource.STRUCTURED)


async def test_structured_spam_without_category_is_other(llm: FakeLlm) -> None:
    llm.queue(completion('{"is_spam": true, "category": "none", "confidence": 0.7}'))
    verdict = await make(llm).classify("看我主页日入2W")
    assert (verdict.is_spam, verdict.category) == (True, "other")


async def test_structured_request_that_answers_digits(llm: FakeLlm) -> None:
    # Some endpoints accept response_format but still follow the old habit.
    llm.queue(completion("1"))
    verdict = await make(llm).classify("看我主页日入2W")
    assert verdict.is_spam
    assert verdict.source is VerdictSource.TEXT


# --------------------------------------------------------------- 0/1 fallback


async def test_auto_falls_back_to_digits_and_remembers(llm: FakeLlm) -> None:
    llm.queue(bad_request(), completion("输出： 1"), completion("0"))
    tracer = FakeTracer()
    classifier = make(llm, tracer=tracer)

    first = await classifier.classify("看我主页日入2W")
    assert first.is_spam
    assert first.source is VerdictSource.TEXT
    assert not classifier.structured
    assert "response_format" in sent(llm, 0)
    retry = sent(llm, 1)
    assert "response_format" not in retry
    assert "只输出一个数字" in retry["messages"][0]["content"]

    second = await classifier.classify("今天天气真好啊")
    assert not second.is_spam
    assert len(llm.requests) == 3  # no further structured attempts
    assert "response_format" not in sent(llm, 2)
    # Every HTTP call is traced; the rejected one carries the error.
    assert [g.start["metadata"]["mode"] for g in tracer.generations] == [
        "structured",
        "text",
        "text",
    ]
    assert tracer.generations[0].end == {"error": "BadRequestError"}


async def test_json_schema_mode_does_not_fall_back(llm: FakeLlm) -> None:
    llm.queue(bad_request())
    verdict = await make(llm, mode="json_schema").classify("看我主页日入2W")
    assert not verdict.is_spam
    assert (verdict.source, verdict.error) == (VerdictSource.FAIL_OPEN, "api_error")
    assert len(llm.requests) == 1


async def test_off_mode_uses_digits_only(llm: FakeLlm) -> None:
    llm.queue(completion("1"))
    verdict = await make(llm, mode="off").classify("看我主页日入2W")
    assert verdict.is_spam
    assert verdict.source is VerdictSource.TEXT
    assert "response_format" not in sent(llm)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("1", True), ("0", False), ("输出： 1", True), ("案例 3 的结果是 0", False), (" 1\n", True)],
)
def test_parse_text_answer(answer: str, expected: bool) -> None:
    assert parse_text_answer(answer) is expected


# ------------------------------------------------------------------ fail open


async def test_unparsable_answer_fails_open(llm: FakeLlm) -> None:
    llm.queue(completion("我无法判断"))
    tracer = FakeTracer()
    verdict = await make(llm, tracer=tracer).classify("hello")
    assert not verdict.is_spam
    assert (verdict.source, verdict.error) == (VerdictSource.FAIL_OPEN, "parse_error")
    assert verdict.failed
    assert tracer.generations[0].end is not None
    assert tracer.generations[0].end["error"] == "AnswerParseError"


@pytest.mark.parametrize(
    "response",
    [
        error(500, "boom"),
        error(429, "slow down"),
        error(401, "bad key"),
        httpx2.ConnectError("refused"),
        httpx2.ReadTimeout("slow"),
    ],
    ids=["500", "429", "401", "connect", "read-timeout"],
)
async def test_api_errors_fail_open(llm: FakeLlm, response: httpx2.Response | Exception) -> None:
    llm.queue(response)
    verdict = await make(llm).classify("看我主页日入2W")
    assert not verdict.is_spam
    assert (verdict.source, verdict.error) == (VerdictSource.FAIL_OPEN, "api_error")
    assert len(llm.requests) == 1  # the SDK does not retry


class _SlowCompletions:
    async def create(self, **_kwargs: Any) -> Any:
        await asyncio.sleep(10)


class _SlowClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": _SlowCompletions()})()


async def test_timeout_fails_open() -> None:
    tracer = FakeTracer()
    classifier = AntiSpamClassifier(
        _SlowClient(),  # pyright: ignore[reportArgumentType]
        ClassifierSettings(model="test-model", timeout=0.05),
        tracer=tracer,
    )
    verdict = await classifier.classify("看我主页日入2W")
    assert not verdict.is_spam
    assert (verdict.source, verdict.error) == (VerdictSource.FAIL_OPEN, "timeout")
    assert tracer.generations[0].end == {"output": None, "error": "CancelledError"}


async def test_unexpected_error_fails_open() -> None:
    class Broken:
        @property
        def chat(self) -> Any:
            raise RuntimeError("bug")

    verdict = await AntiSpamClassifier(
        Broken(),  # pyright: ignore[reportArgumentType]
        ClassifierSettings(model="m"),
    ).classify("x")
    assert (verdict.is_spam, verdict.error) == (False, "error")


# ------------------------------------------------------------ prompt & input


async def test_custom_prompt_and_context(llm: FakeLlm) -> None:
    llm.queue(completion('{"is_spam": false, "category": "none", "confidence": 0.6}'))
    prompt = Prompt.custom("你是群管助手，判断消息是否为垃圾信息。")
    verdict = await make(llm, prompt=prompt).classify(
        "你好", context=SpamContext(sender_name="Bob", sender_bio="日结兼职")
    )
    request = sent(llm)
    assert request["messages"][0]["content"].startswith("你是群管助手")
    assert request["messages"][1]["content"] == (
        "发送者名字: Bob\n发送者简介: 日结兼职\n消息内容: 你好"
    )
    assert verdict.prompt_version == prompt.version
    assert prompt.version.startswith("custom-")


def test_prompt_versions() -> None:
    assert Prompt.custom("abc").version == Prompt.custom(" abc ").version
    assert Prompt.custom("abc").version != Prompt.custom("abd").version
    assert Prompt.custom("abc", "v7").version == "v7"
    with pytest.raises(ValueError, match="不能为空"):
        Prompt.custom("  ")
    default = Prompt()
    assert "只输出一个数字" in default.system(structured=False)
    assert "JSON" in default.system(structured=True)
    assert "看我主页日入2W" in default.base  # migrated few-shot examples


def test_user_message_without_context() -> None:
    assert user_message("hi") == "hi"
    assert user_message("hi", SpamContext()) == "hi"
    assert user_message("hi", SpamContext(sender_name="A")) == "发送者名字: A\n消息内容: hi"


async def test_long_input_is_truncated(llm: FakeLlm) -> None:
    llm.queue(completion("0"))
    classifier = AntiSpamClassifier(
        llm.client(),
        ClassifierSettings(model="m", max_input_chars=10, structured_output="off"),
    )
    await classifier.classify("x" * 100)
    assert sent(llm)["messages"][1]["content"] == "x" * 10
    await classifier.aclose()
