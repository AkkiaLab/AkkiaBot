"""How user names appear in public group messages sent by the bot.

Spammers put ads in their display names, so every *public* notice that names a
user (captcha prompt, ban / warn / anti-spam notices…) shows only a fragment of
the name; the clickable ``tg://user`` mention still identifies the user.
Never put the full name or ``@username`` of the user being handled in such a
message.
"""

from __future__ import annotations

#: Names longer than this are cut to their first and last :data:`_KEEP` characters.
_MAX_FULL = 4
_KEEP = 2


def public_name(name: str, fallback: str = "该成员") -> str:
    """Name fragment for public notices: keep 2 chars at each end of long names.

    ``"Alice Smith"`` → ``"Al…th"``; names of at most 4 characters are kept.
    A blank name becomes ``fallback``.
    """
    name = name.strip() or fallback
    return f"{name[:_KEEP]}…{name[-_KEEP:]}" if len(name) > _MAX_FULL else name
