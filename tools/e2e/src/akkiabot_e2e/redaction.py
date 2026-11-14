"""Secret wrapper and explicit redaction of *known* secret values.

Following the workspace logging rule, nothing here guesses what a secret looks
like: the caller registers the values it knows (sessions, api_hash, bot token)
and every line the tool prints or writes goes through :class:`Redactor`.
"""

from __future__ import annotations

from collections.abc import Iterable

REDACTED = "[REDACTED]"
# Very short values would redact unrelated text (e.g. an api_id like "12").
_MIN_SECRET_LENGTH = 6


class Secret:
    """Holds a secret string; ``repr``/``str`` never reveal it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret('**********')"

    __str__ = __repr__


class Redactor:
    """Replaces registered secret values with ``[REDACTED]``."""

    def __init__(self, values: Iterable[str | Secret | None] = ()) -> None:
        self._values: set[str] = set()
        for value in values:
            self.add(value)

    def add(self, value: str | Secret | None) -> None:
        raw = value.reveal() if isinstance(value, Secret) else value
        if raw and len(raw) >= _MIN_SECRET_LENGTH:
            self._values.add(raw)

    def __call__(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, REDACTED)
        return text
