"""PTB implementation of :class:`~akkiabot_server.services.moderation.ModerationApi`."""

from __future__ import annotations

import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from telegram import Bot, ChatMember, ChatMemberAdministrator
from telegram.error import RetryAfter, TelegramError
from telegram.warnings import PTBDeprecationWarning

from akkiabot_server.services.moderation import (
    MemberInfo,
    MemberRole,
    ModerationApiError,
    RateLimitedError,
)

_ROLES = {
    ChatMember.OWNER: MemberRole.OWNER,
    ChatMember.ADMINISTRATOR: MemberRole.ADMIN,
    ChatMember.MEMBER: MemberRole.MEMBER,
    ChatMember.RESTRICTED: MemberRole.RESTRICTED,
    ChatMember.LEFT: MemberRole.LEFT,
    ChatMember.BANNED: MemberRole.BANNED,
}


def member_info(member: ChatMember) -> MemberInfo:
    can_restrict = isinstance(member, ChatMemberAdministrator) and member.can_restrict_members
    return MemberInfo(_ROLES.get(member.status, MemberRole.MEMBER), can_restrict)


def _retry_after(exc: RetryAfter) -> float:
    # PTB 22.2+ warns that ``retry_after`` will become a timedelta; accept both.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBDeprecationWarning)
        value = exc.retry_after
    return value if isinstance(value, int | float) else value.total_seconds()


@contextmanager
def translate_errors() -> Iterator[None]:
    """Map PTB errors to :class:`RateLimitedError` / :class:`ModerationApiError`."""
    try:
        yield
    except RetryAfter as exc:
        raise RateLimitedError(_retry_after(exc)) from exc
    except TelegramError as exc:
        raise ModerationApiError(str(exc)) from exc


class PtbModerationApi:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def get_member(self, chat_id: int, user_id: int) -> MemberInfo:
        try:
            return member_info(await self._bot.get_chat_member(chat_id, user_id))
        except RetryAfter as exc:
            raise RateLimitedError(_retry_after(exc)) from exc
        except TelegramError as exc:
            raise ModerationApiError(str(exc)) from exc

    async def ban(self, chat_id: int, user_id: int) -> None:
        try:
            await self._bot.ban_chat_member(chat_id, user_id)
        except RetryAfter as exc:
            raise RateLimitedError(_retry_after(exc)) from exc
        except TelegramError as exc:
            raise ModerationApiError(str(exc)) from exc

    async def delete_messages(self, chat_id: int, message_ids: Sequence[int]) -> None:
        try:
            await self._bot.delete_messages(chat_id, message_ids)
        except RetryAfter as exc:
            raise RateLimitedError(_retry_after(exc)) from exc
        except TelegramError as exc:
            raise ModerationApiError(str(exc)) from exc
