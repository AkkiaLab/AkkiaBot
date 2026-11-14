"""Offline evaluation helpers and the Langfuse tracer adapter (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from akkiabot_antispam import (
    AntiSpamClassifier,
    ClassifierSettings,
    LangfuseTracer,
    SpamVerdict,
    Usage,
    VerdictSource,
)
from akkiabot_antispam import evaluate as evaluate_module
from akkiabot_antispam.evaluate import (
    EvalReport,
    Sample,
    build_from_env,
    evaluate,
    load_dataset,
)
from akkiabot_antispam.testing import FakeLlm, completion

FIXTURE = Path(__file__).parent / "fixtures" / "eval_set.jsonl"


def test_fixture_dataset_is_balanced() -> None:
    samples = load_dataset(FIXTURE)
    assert len(samples) >= 30
    assert len({s.id for s in samples}) == len(samples)
    spam = sum(s.is_spam for s in samples)
    assert 10 <= spam <= len(samples) - 10


def test_load_dataset_rejects_bad_labels(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "a", "label": 2, "text": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="label"):
        load_dataset(path)
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="为空"):
        load_dataset(path)


class KeywordClassifier:
    """Spam when the text contains a keyword; ``boom`` simulates a fail-open verdict."""

    async def classify(self, text: str) -> SpamVerdict:
        if "boom" in text:
            return SpamVerdict(is_spam=False, source=VerdictSource.FAIL_OPEN, error="timeout")
        return SpamVerdict(is_spam="buy" in text)


async def test_evaluate_reports_accuracy() -> None:
    samples = [
        Sample(id="tp", label=1, text="buy now"),
        Sample(id="tn", label=0, text="hello"),
        Sample(id="fp", label=0, text="I will buy lunch"),
        Sample(id="fn", label=1, text="cheap stuff"),
        Sample(id="fo", label=1, text="boom"),
    ]
    report = await evaluate(KeywordClassifier(), samples, concurrency=2)
    assert (report.total, report.correct) == (5, 2)
    assert (report.true_positive, report.true_negative) == (1, 1)
    assert (report.false_positive, report.false_negative, report.fail_open) == (1, 1, 1)
    assert report.accuracy == pytest.approx(0.4)
    assert report.precision == pytest.approx(0.5)
    assert report.recall == pytest.approx(1 / 3)
    text = report.render()
    assert "准确率: 40.0%" in text
    assert "fo: 期望 1，得到 fail_open(timeout)" in text
    assert "fp: 期望 0，得到 1" in text


@pytest.mark.parametrize(("label", "recall"), [(0, 1.0), (1, 0.5)])
async def test_fail_open_is_never_correct(label: int, recall: float) -> None:
    # Regression: a timed-out *ham* sample used to count as a correct "not spam".
    samples = [
        Sample(id="tp", label=1, text="buy now"),
        Sample(id="tn", label=0, text="hello"),
        Sample(id="fo", label=label, text="boom"),
    ]
    report = await evaluate(KeywordClassifier(), samples)
    assert (report.total, report.correct, report.fail_open) == (3, 2, 1)
    assert (report.true_positive, report.true_negative) == (1, 1)
    assert (report.false_positive, report.false_negative) == (0, 0)
    assert report.accuracy == pytest.approx(2 / 3)
    assert report.precision == pytest.approx(1.0)
    assert report.recall == pytest.approx(recall)
    text = report.render()
    assert "准确率: 66.7%" in text
    assert f"fo: 期望 {label}，得到 fail_open(timeout)" in text


def test_empty_report() -> None:
    report = EvalReport()
    assert (report.accuracy, report.precision, report.recall) == (0.0, 0.0, 0.0)


class FakeObservation:
    def __init__(self, fail: bool = False) -> None:
        self.updates: list[dict[str, Any]] = []
        self.ended = False
        self.fail = fail

    def update(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("langfuse down")
        self.updates.append(kwargs)

    def end(self) -> None:
        self.ended = True


class FakeLangfuse:
    def __init__(self, *, fail_start: bool = False, fail_update: bool = False) -> None:
        self.started: list[dict[str, Any]] = []
        self.observation = FakeObservation(fail_update)
        self.fail_start = fail_start
        self.flushed = self.shut = False

    def start_observation(self, **kwargs: Any) -> FakeObservation:
        if self.fail_start:
            raise RuntimeError("langfuse down")
        self.started.append(kwargs)
        return self.observation

    def flush(self) -> None:
        self.flushed = True

    def shutdown(self) -> None:
        self.shut = True


def test_langfuse_tracer_records_a_generation() -> None:
    client = FakeLangfuse()
    tracer = LangfuseTracer(client)  # pyright: ignore[reportArgumentType]
    generation = tracer.start(
        name="antispam.classify",
        model="m",
        input=[{"role": "user", "content": "x"}],
        prompt_version="antispam-v1",
        metadata={"mode": "structured"},
    )
    generation.end(output="1", usage=Usage(input_tokens=3, total_tokens=4))
    (started,) = client.started
    assert started["as_type"] == "generation"
    assert started["version"] == "antispam-v1"
    assert started["metadata"] == {"prompt_version": "antispam-v1", "mode": "structured"}
    assert client.observation.updates == [
        {
            "output": "1",
            "usage_details": {"input": 3, "total": 4},
            "level": None,
            "status_message": None,
            "metadata": None,
        }
    ]
    assert client.observation.ended
    tracer.flush()
    tracer.shutdown()
    assert client.flushed
    assert client.shut


def test_langfuse_tracer_marks_errors() -> None:
    client = FakeLangfuse()
    LangfuseTracer(client).start(  # pyright: ignore[reportArgumentType]
        name="n", model="m", input="x", prompt_version="v"
    ).end(error="TimeoutError")
    assert client.observation.updates[0]["level"] == "ERROR"
    assert client.observation.updates[0]["status_message"] == "TimeoutError"


def test_langfuse_failures_never_propagate() -> None:
    tracer = LangfuseTracer(FakeLangfuse(fail_start=True))  # pyright: ignore[reportArgumentType]
    tracer.start(name="n", model="m", input="x", prompt_version="v").end(output="0")
    failing_update = LangfuseTracer(FakeLangfuse(fail_update=True))  # pyright: ignore[reportArgumentType]
    failing_update.start(name="n", model="m", input="x", prompt_version="v").end(output="0")


ENV_KEYS = (
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "ANTISPAM_TIMEOUT",
    "ANTISPAM_STRUCTURED_OUTPUT",
    "ANTISPAM_PROMPT_FILE",
    "ANTISPAM_PROMPT_VERSION",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.usefixtures("clean_env")
async def test_build_from_env_file(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("自定义提示词", encoding="utf-8")
    env = tmp_path / ".env"
    # mock: 人工构造的不可用测试值，不是凭据。
    mock_key_line = "LLM_API_KEY=fake-api-key-for-tests"
    env.write_text(
        "\n".join(
            [
                "# comment",
                mock_key_line,
                "LLM_MODEL='doubao-test'",
                "LLM_BASE_URL=https://llm.example.test/v1",
                "ANTISPAM_STRUCTURED_OUTPUT=off",
                "ANTISPAM_TIMEOUT=7",
                f"ANTISPAM_PROMPT_FILE={prompt}",
                "ANTISPAM_PROMPT_VERSION=",
            ]
        ),
        encoding="utf-8",
    )
    classifier = build_from_env(env)
    assert classifier.settings.model == "doubao-test"
    assert classifier.settings.timeout == 7
    assert not classifier.structured
    assert classifier.prompt.base == "自定义提示词"
    assert classifier.prompt.version.startswith("custom-")
    await classifier.aclose()


@pytest.mark.usefixtures("clean_env")
def test_build_from_env_requires_llm(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="LLM_API_KEY"):
        build_from_env(tmp_path / "missing.env")


@pytest.mark.usefixtures("clean_env")
def test_main_prints_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    llm = FakeLlm(default=completion('{"is_spam": false, "category": "none", "confidence": 0.9}'))
    monkeypatch.setattr(
        evaluate_module,
        "build_from_env",
        lambda _path: AntiSpamClassifier(llm.client(), ClassifierSettings(model="fake")),
    )
    assert evaluate_module.main(["--dataset", str(FIXTURE), "--limit", "4"]) == 0
    out = capsys.readouterr().out
    assert "模型: fake  提示词版本: antispam-v1  结构化输出: 是" in out
    assert "样本数: 4" in out
    assert len(llm.requests) == 4
