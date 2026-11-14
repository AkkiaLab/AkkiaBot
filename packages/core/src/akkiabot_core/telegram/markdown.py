"""MarkdownV2 escaping, per https://core.telegram.org/bots/api#markdownv2-style."""

from __future__ import annotations

import re

_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_CODE_SPECIAL = re.compile(r"([`\\])")


def escape_markdown_v2(text: str) -> str:
    """Escape ``text`` for use as plain text in a MarkdownV2 message."""
    return _SPECIAL.sub(r"\\\1", text)


def escape_markdown_v2_code(text: str) -> str:
    """Escape ``text`` for use inside ``inline code`` or a ``pre`` block."""
    return _CODE_SPECIAL.sub(r"\\\1", text)
