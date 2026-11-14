"""LLM call tracing (Langfuse).

The classifier reports every call through the small :class:`Tracer` protocol:
prompt version, input, output, latency (start/end of the observation), token
usage and errors. :class:`LangfuseTracer` sends them to Langfuse;
:class:`NullTracer` drops them. Tracing failures never affect classification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from akkiabot_antispam.models import Usage

if TYPE_CHECKING:
    from langfuse import Langfuse

logger = logging.getLogger(__name__)


class Generation(Protocol):
    def end(
        self,
        *,
        output: Any = None,
        usage: Usage | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...


class Tracer(Protocol):
    def start(
        self,
        *,
        name: str,
        model: str,
        input: Any,
        prompt_version: str,
        metadata: dict[str, Any] | None = None,
    ) -> Generation: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


class _NullGeneration:
    def end(
        self,
        *,
        output: Any = None,
        usage: Usage | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None


class NullTracer:
    """Tracing disabled."""

    def start(
        self,
        *,
        name: str,
        model: str,
        input: Any,
        prompt_version: str,
        metadata: dict[str, Any] | None = None,
    ) -> Generation:
        return _NullGeneration()

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


@dataclass(slots=True)
class _LangfuseGeneration:
    observation: Any

    def end(
        self,
        *,
        output: Any = None,
        usage: Usage | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.observation.update(
                output=output,
                usage_details=usage.as_dict() if usage else None,
                level="ERROR" if error else None,
                status_message=error,
                metadata=metadata,
            )
            self.observation.end()
        except Exception:
            logger.warning("langfuse: failed to end generation", exc_info=True)


@dataclass(slots=True)
class LangfuseTracer:
    """Reports each classification as a Langfuse *generation* observation."""

    client: Langfuse

    @classmethod
    def create(
        cls,
        *,
        public_key: str,
        secret_key: str,
        host: str | None = None,
        environment: str | None = None,
        release: str | None = None,
    ) -> LangfuseTracer:
        from langfuse import Langfuse  # heavy (OpenTelemetry): imported only when enabled

        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=host,
            environment=environment,
            release=release,
        )
        return cls(client)

    def start(
        self,
        *,
        name: str,
        model: str,
        input: Any,
        prompt_version: str,
        metadata: dict[str, Any] | None = None,
    ) -> Generation:
        try:
            observation = self.client.start_observation(
                name=name,
                as_type="generation",
                model=model,
                input=input,
                version=prompt_version,
                metadata={"prompt_version": prompt_version, **(metadata or {})},
            )
        except Exception:
            logger.warning("langfuse: failed to start generation", exc_info=True)
            return _NullGeneration()
        return _LangfuseGeneration(observation)

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception:
            logger.warning("langfuse: flush failed", exc_info=True)

    def shutdown(self) -> None:
        try:
            self.client.shutdown()
        except Exception:
            logger.warning("langfuse: shutdown failed", exc_info=True)
