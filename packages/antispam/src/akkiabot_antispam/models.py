"""Result and input models of the spam classifier (docs/architecture.md §5.2)."""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

type SpamCategory = Literal["ad", "gambling", "fraud", "other", "none"]

FailOpenError = Literal["timeout", "api_error", "parse_error", "error"]
"""Error class recorded on a fail-open verdict."""
FAIL_OPEN_ERRORS: Final[tuple[str, ...]] = get_args(FailOpenError)
"""Every :data:`FailOpenError` value (metrics export them all from startup)."""


class VerdictSource(StrEnum):
    """How a verdict was obtained."""

    STRUCTURED = "structured"
    """JSON structured output (``response_format=json_schema``)."""
    TEXT = "text"
    """Plain ``0`` / ``1`` answer (model without structured output support)."""
    FAIL_OPEN = "fail_open"
    """Timeout, API error or unparsable answer: treated as *not spam*."""


class SpamVerdict(BaseModel):
    """Classifier result. Errors never raise: they produce a fail-open verdict."""

    model_config = ConfigDict(frozen=True)

    is_spam: bool
    category: SpamCategory = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: VerdictSource = VerdictSource.STRUCTURED
    prompt_version: str = ""
    model: str = ""
    error: str | None = None
    """Short error class (``timeout``, ``api_error``, ``parse_error``…) for fail-open verdicts."""

    @property
    def failed(self) -> bool:
        return self.source is VerdictSource.FAIL_OPEN


class SpamContext(BaseModel):
    """Extra facts about the sender (used for ``/spam`` reports)."""

    model_config = ConfigDict(frozen=True)

    sender_name: str | None = None
    sender_bio: str | None = None


class Usage(BaseModel):
    """Token usage reported by the API (missing values stay ``None``)."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def as_dict(self) -> dict[str, int]:
        """Langfuse ``usage_details`` keys (only known values)."""
        values = {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "total": self.total_tokens,
        }
        return {k: v for k, v in values.items() if v is not None}
