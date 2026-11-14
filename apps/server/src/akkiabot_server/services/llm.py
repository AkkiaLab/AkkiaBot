"""Build the spam classifier (and its Langfuse tracer) from the settings."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from akkiabot_antispam import (
    AntiSpamClassifier,
    ClassifierSettings,
    LangfuseTracer,
    NullTracer,
    Prompt,
    Tracer,
    build_client,
)
from akkiabot_core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LlmComponents:
    classifier: AntiSpamClassifier | None
    tracer: Tracer

    async def aclose(self) -> None:
        if self.classifier is not None:
            await self.classifier.aclose()
        self.tracer.shutdown()


def load_prompt(settings: Settings) -> Prompt:
    """The built-in prompt, or ``ANTISPAM_PROMPT_FILE`` (read once at startup)."""
    antispam = settings.antispam
    if antispam.prompt_file is None:
        return Prompt()
    text = antispam.prompt_file.read_text(encoding="utf-8")
    return Prompt.custom(text, antispam.prompt_version)


def build_tracer(settings: Settings, *, release: str | None = None) -> Tracer:
    langfuse = settings.langfuse
    if langfuse.public_key is None or langfuse.secret_key is None:
        return NullTracer()
    return LangfuseTracer.create(
        public_key=langfuse.public_key.get_secret_value(),
        secret_key=langfuse.secret_key.get_secret_value(),
        host=str(langfuse.host).rstrip("/") if langfuse.host else None,
        environment=settings.observability.sentry_environment,
        release=release,
    )


def build_llm(
    settings: Settings,
    *,
    release: str | None = None,
    client: AsyncOpenAI | None = None,
    tracer: Tracer | None = None,
) -> LlmComponents:
    """Classifier when ``LLM_API_KEY`` and ``LLM_MODEL`` are set, otherwise ``None``.

    ``client`` / ``tracer`` replace the real ones (tests).
    """
    llm, antispam = settings.llm, settings.antispam
    prompt = load_prompt(settings)
    if llm.model is None or (llm.api_key is None and client is None):
        logger.info("antispam classifier disabled: LLM_API_KEY / LLM_MODEL not configured")
        return LlmComponents(None, tracer or NullTracer())
    tracer = tracer or build_tracer(settings, release=release)
    if client is None:
        assert llm.api_key is not None  # noqa: S101 - checked above
        client = build_client(
            api_key=llm.api_key.get_secret_value(),
            base_url=str(llm.base_url) if llm.base_url else None,
            timeout=antispam.timeout,
        )
    classifier = AntiSpamClassifier(
        client,
        ClassifierSettings(
            model=llm.model,
            timeout=antispam.timeout,
            structured_output=antispam.structured_output,
        ),
        prompt=prompt,
        tracer=tracer,
    )
    logger.info(
        "antispam classifier enabled",
        extra={
            "model": llm.model,
            "prompt_version": prompt.version,
            "structured_output": antispam.structured_output,
            "langfuse": not isinstance(tracer, NullTracer),
        },
    )
    return LlmComponents(classifier, tracer)
