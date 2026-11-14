"""Offline evaluation of the classifier against a labelled data set.

Run it by hand (it calls the real LLM) to get a baseline before and after
changing the prompt or the model::

    just antispam-eval                       # LLM_* / ANTISPAM_* from the environment or .env
    uv run akkiabot-antispam-eval --dataset my.jsonl --concurrency 4

Each line of the data set is a JSON object ``{"id", "label" (1 spam / 0 not),
"text"}``. Fail-open verdicts (timeouts, API errors) are counted as wrong for both
classes (never as a correct "not spam"), kept out of TP/TN/FP/FN and reported
separately, so a broken endpoint cannot look like a good prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from akkiabot_antispam.classifier import (
    AntiSpamClassifier,
    ClassifierSettings,
    StructuredOutput,
    build_client,
)
from akkiabot_antispam.models import SpamVerdict
from akkiabot_antispam.prompts import Prompt

DEFAULT_DATASET = Path("packages/antispam/tests/fixtures/eval_set.jsonl")


class Sample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    label: int
    text: str

    @property
    def is_spam(self) -> bool:
        return self.label == 1


class Classifier(Protocol):
    async def classify(self, text: str) -> SpamVerdict: ...


def load_dataset(path: Path) -> list[Sample]:
    samples = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        sample = Sample.model_validate_json(line)
        if sample.label not in (0, 1):
            raise ValueError(f"{path}:{number}: label 必须是 0 或 1")
        samples.append(sample)
    if not samples:
        raise ValueError(f"{path}: 评测集为空")
    return samples


@dataclass(slots=True)
class EvalReport:
    total: int = 0
    true_positive: int = 0
    true_negative: int = 0
    false_positive: int = 0
    false_negative: int = 0
    fail_open: int = 0
    fail_open_spam: int = 0
    """Fail-open verdicts on spam samples (missed spam, counted in recall)."""
    seconds: float = 0.0
    mistakes: list[tuple[Sample, SpamVerdict]] = field(default_factory=list)

    @property
    def correct(self) -> int:
        return self.true_positive + self.true_negative

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def precision(self) -> float:
        predicted = self.true_positive + self.false_positive
        return self.true_positive / predicted if predicted else 0.0

    @property
    def recall(self) -> float:
        actual = self.true_positive + self.false_negative + self.fail_open_spam
        return self.true_positive / actual if actual else 0.0

    def add(self, sample: Sample, verdict: SpamVerdict) -> None:
        """Fail-open verdicts are never correct and stay out of the confusion matrix."""
        self.total += 1
        if verdict.failed:
            self.fail_open += 1
            self.fail_open_spam += sample.is_spam
            self.mistakes.append((sample, verdict))
            return
        match (sample.is_spam, verdict.is_spam):
            case (True, True):
                self.true_positive += 1
            case (False, False):
                self.true_negative += 1
            case (False, True):
                self.false_positive += 1
            case (True, False):
                self.false_negative += 1
        if verdict.is_spam != sample.is_spam:
            self.mistakes.append((sample, verdict))

    def render(self) -> str:
        lines = [
            f"样本数: {self.total}  正确: {self.correct}  准确率: {self.accuracy:.1%}",
            f"精确率: {self.precision:.1%}  召回率: {self.recall:.1%}",
            f"TP={self.true_positive} TN={self.true_negative} "
            f"FP={self.false_positive} FN={self.false_negative} fail_open={self.fail_open}",
            f"耗时: {self.seconds:.1f}s",
        ]
        if self.mistakes:
            lines.append("判错的样本:")
            for sample, verdict in self.mistakes:
                got = f"fail_open({verdict.error})" if verdict.failed else int(verdict.is_spam)
                lines.append(f"  - {sample.id}: 期望 {sample.label}，得到 {got}")
        return "\n".join(lines)


async def evaluate(
    classifier: Classifier, samples: Iterable[Sample], *, concurrency: int = 4
) -> EvalReport:
    semaphore = asyncio.Semaphore(concurrency)
    samples = list(samples)

    async def run(sample: Sample) -> SpamVerdict:
        async with semaphore:
            return await classifier.classify(sample.text)

    start = time.perf_counter()
    verdicts = await asyncio.gather(*(run(s) for s in samples))
    report = EvalReport()
    for sample, verdict in zip(samples, verdicts, strict=True):
        report.add(sample, verdict)
    report.seconds = time.perf_counter() - start
    return report


def _load_env_file(path: Path) -> dict[str, str]:
    """Minimal ``.env`` reader (``KEY=VALUE`` lines) so the command matches ``akkiabot serve``."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().upper()] = value.strip().strip("'\"")
    return values


def _setting(env: dict[str, str], key: str) -> str | None:
    value = os.environ.get(key) or env.get(key)
    return value or None


def build_from_env(env_file: Path = Path(".env")) -> AntiSpamClassifier:
    """The classifier as the server would build it (same ``LLM_*`` / ``ANTISPAM_*`` keys)."""
    env = _load_env_file(env_file)
    api_key, model = _setting(env, "LLM_API_KEY"), _setting(env, "LLM_MODEL")
    if not api_key or not model:
        raise SystemExit("需要配置 LLM_API_KEY 与 LLM_MODEL（环境变量或 .env）")
    timeout = float(_setting(env, "ANTISPAM_TIMEOUT") or 45.0)
    mode = _setting(env, "ANTISPAM_STRUCTURED_OUTPUT") or "auto"
    if mode not in ("auto", "json_schema", "off"):
        raise SystemExit("ANTISPAM_STRUCTURED_OUTPUT 只能是 auto / json_schema / off")
    structured: StructuredOutput = mode  # pyright: ignore[reportAssignmentType]
    prompt = None
    if prompt_file := _setting(env, "ANTISPAM_PROMPT_FILE"):
        prompt = Prompt.custom(
            Path(prompt_file).read_text(encoding="utf-8"), _setting(env, "ANTISPAM_PROMPT_VERSION")
        )
    return AntiSpamClassifier(
        build_client(api_key=api_key, base_url=_setting(env, "LLM_BASE_URL"), timeout=timeout),
        ClassifierSettings(model=model, timeout=timeout, structured_output=structured),
        prompt=prompt,
    )


async def _main(args: argparse.Namespace) -> int:
    samples = load_dataset(args.dataset)
    if args.limit:
        samples = samples[: args.limit]
    classifier = build_from_env(args.env_file)
    try:
        report = await evaluate(classifier, samples, concurrency=args.concurrency)
    finally:
        await classifier.aclose()
    print(  # noqa: T201 - CLI output
        f"模型: {classifier.settings.model}  提示词版本: {classifier.prompt.version}  "
        f"结构化输出: {'是' if classifier.structured else '否（0/1）'}"
    )
    print(report.render())  # noqa: T201
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="反垃圾分类器离线评测（会调用真实 LLM）")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="只评测前 N 条（0 = 全部）")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
