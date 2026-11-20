"""``AntiSpamClassifier``: one LLM call per message, fail open on any error.

Answer modes (``structured_output``):

* ``auto`` (default): ask for JSON via ``response_format=json_schema``. If the
  endpoint rejects that parameter (HTTP 400/422), switch this classifier to the
  plain ``0``/``1`` prompt for good and retry the same message once.
* ``json_schema``: structured output only, no fallback.
* ``off``: always the plain ``0``/``1`` prompt.

Whatever happens — timeout, API error, unparsable answer — :meth:`classify`
returns a *not spam* verdict with ``source=fail_open`` instead of raising, so a
broken LLM can never delete legitimate messages.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Literal

import openai
from openai import AsyncOpenAI, Omit, omit
from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat.completion_create_params import ResponseFormat
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from akkiabot_antispam.models import (
    FailOpenError,
    SpamContext,
    SpamVerdict,
    Usage,
    VerdictSource,
)
from akkiabot_antispam.prompts import VERDICT_JSON_SCHEMA, Prompt, user_message
from akkiabot_antispam.tracing import NullTracer, Tracer

if TYPE_CHECKING:
    import httpx2

logger = logging.getLogger(__name__)

type StructuredOutput = Literal["auto", "json_schema", "off"]

GENERATION_NAME = "antispam.classify"
_DIGITS = re.compile(r"\d+")


class ClassifierSettings(BaseModel):
    """Passed in by the caller (the package reads no global configuration)."""

    model_config = ConfigDict(frozen=True)

    model: str
    timeout: float = Field(default=45.0, gt=0)
    """Overall budget of one :meth:`AntiSpamClassifier.classify` call, fallback included."""
    structured_output: StructuredOutput = "auto"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_input_chars: int = Field(default=4000, gt=0)
    """Longer texts are truncated before being sent."""


class _StructuredAnswer(BaseModel):
    is_spam: bool
    category: Literal["ad", "gambling", "fraud", "other", "none"] = "none"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class AnswerParseError(ValueError):
    """The model answered something that is neither the JSON object nor 0/1."""


class _StructuredUnsupportedError(Exception):
    """The endpoint rejected ``response_format``."""


def parse_text_answer(content: str) -> bool:
    """Legacy answer: the last number in the text, ``1`` = spam, ``0`` = not spam."""
    numbers = _DIGITS.findall(content)
    if not numbers or numbers[-1] not in {"0", "1"}:
        raise AnswerParseError("expected 0 or 1")
    return numbers[-1] == "1"


_BARE_DIGIT = re.compile(r"[01]")


def parse_bare_digit(content: str) -> bool:
    """Strict fallback for structured mode: the whole answer must be ``0`` or ``1``.

    Anything else (notably invalid JSON that merely contains numbers such as a
    ``confidence`` of 1) is a parse error, so it fails open instead of being
    read as a verdict.
    """
    text = _strip_fence(content)
    if not _BARE_DIGIT.fullmatch(text):
        raise AnswerParseError("expected a JSON verdict or a bare 0/1")
    return text == "1"


def parse_structured_answer(content: str) -> _StructuredAnswer:
    try:
        return _StructuredAnswer.model_validate_json(_strip_fence(content))
    except ValidationError as exc:
        raise AnswerParseError("invalid JSON verdict") from exc


def _strip_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return text


def _usage(completion: Any) -> Usage | None:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None
    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )


class AntiSpamClassifier:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: ClassifierSettings,
        *,
        prompt: Prompt | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._client = client
        self.settings = settings
        self.prompt = prompt or Prompt()
        self._tracer: Tracer = tracer or NullTracer()
        self._structured = settings.structured_output != "off"

    @property
    def structured(self) -> bool:
        """Whether the next call asks for JSON (``False`` after an ``auto`` fallback)."""
        return self._structured

    async def classify(self, text: str, *, context: SpamContext | None = None) -> SpamVerdict:
        """Classify ``text``; never raises (fail open)."""
        content = user_message(text[: self.settings.max_input_chars], context)
        try:
            async with asyncio.timeout(self.settings.timeout):
                return await self._classify(content)
        except TimeoutError:
            return self._fail_open("timeout")
        except openai.APIError as exc:
            logger.warning("antispam: llm api error", extra={"error_type": type(exc).__name__})
            return self._fail_open("api_error")
        except AnswerParseError:
            logger.warning("antispam: unparsable llm answer")
            return self._fail_open("parse_error")
        except Exception:
            logger.exception("antispam: unexpected classifier error")
            return self._fail_open("error")

    async def _classify(self, content: str) -> SpamVerdict:
        if self._structured:
            try:
                return await self._ask(content, structured=True)
            except _StructuredUnsupportedError:
                self._structured = False
                logger.warning(
                    "antispam: structured output rejected by the endpoint, using 0/1 answers",
                    extra={"model": self.settings.model},
                )
        return await self._ask(content, structured=False)

    async def _ask(self, content: str, *, structured: bool) -> SpamVerdict:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": self.prompt.system(structured=structured)},
            {"role": "user", "content": content},
        ]
        response_format: ResponseFormat | Omit = omit
        if structured:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "spam_verdict",
                    "strict": True,
                    "schema": VERDICT_JSON_SCHEMA,
                },
            }
        generation = self._tracer.start(
            name=GENERATION_NAME,
            model=self.settings.model,
            input=messages,
            prompt_version=self.prompt.version,
            metadata={"mode": "structured" if structured else "text"},
        )
        answer: str | None = None
        try:
            completion = await self._client.chat.completions.create(
                model=self.settings.model,
                messages=messages,
                temperature=self.settings.temperature,
                response_format=response_format,
            )
            answer = completion.choices[0].message.content or ""
            verdict = self._verdict(answer, structured=structured)
        except (openai.BadRequestError, openai.UnprocessableEntityError) as exc:
            generation.end(error=type(exc).__name__)
            if structured and self.settings.structured_output == "auto":
                raise _StructuredUnsupportedError from exc
            raise
        except BaseException as exc:  # timeouts arrive as CancelledError
            generation.end(output=answer, error=type(exc).__name__)
            raise
        generation.end(
            output=answer,
            usage=_usage(completion),
            metadata={"verdict": verdict.model_dump(mode="json")},
        )
        return verdict

    def _verdict(self, answer: str, *, structured: bool) -> SpamVerdict:
        common = {"prompt_version": self.prompt.version, "model": self.settings.model}
        if structured:
            try:
                parsed = parse_structured_answer(answer)
            except AnswerParseError:
                # Some endpoints accept response_format but still answer a bare 0/1.
                is_spam = parse_bare_digit(answer)
                return SpamVerdict(
                    is_spam=is_spam,
                    category="other" if is_spam else "none",
                    confidence=1.0,
                    source=VerdictSource.TEXT,
                    **common,
                )
            category = parsed.category if parsed.is_spam else "none"
            if parsed.is_spam and category == "none":
                category = "other"
            return SpamVerdict(
                is_spam=parsed.is_spam,
                category=category,
                confidence=parsed.confidence,
                source=VerdictSource.STRUCTURED,
                **common,
            )
        is_spam = parse_text_answer(answer)
        return SpamVerdict(
            is_spam=is_spam,
            category="other" if is_spam else "none",
            confidence=1.0,
            source=VerdictSource.TEXT,
            **common,
        )

    def _fail_open(self, error: FailOpenError) -> SpamVerdict:
        return SpamVerdict(
            is_spam=False,
            category="none",
            confidence=0.0,
            source=VerdictSource.FAIL_OPEN,
            prompt_version=self.prompt.version,
            model=self.settings.model,
            error=error,
        )

    async def aclose(self) -> None:
        await self._client.close()
        self._tracer.flush()


def build_client(
    *,
    api_key: str,
    base_url: str | None = None,
    timeout: float = 45.0,
    http_client: httpx2.AsyncClient | None = None,
) -> AsyncOpenAI:
    """An OpenAI-compatible client without SDK retries (the caller's budget rules).

    ``http_client`` replaces the transport (tests); note that the openai SDK
    uses ``httpx2``, not ``httpx``.
    """
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=0,
        http_client=http_client,
    )


__all__ = [
    "GENERATION_NAME",
    "AnswerParseError",
    "AntiSpamClassifier",
    "ClassifierSettings",
    "StructuredOutput",
    "build_client",
    "parse_structured_answer",
    "parse_text_answer",
]
