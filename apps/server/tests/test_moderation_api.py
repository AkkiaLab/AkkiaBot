"""PTB adapter of ModerationApi: role mapping and error translation."""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast

import pytest
from telegram import Bot, ChatMember
from telegram.error import BadRequest, RetryAfter
from telegram.warnings import PTBDeprecationWarning

from akkiabot_server.bot.moderation_api import PtbModerationApi
from akkiabot_server.services.moderation import (
    MemberInfo,
    MemberRole,
    ModerationApiError,
    RateLimitedError,
)
from akkiabot_server.testing import UpdateFactory


class StubBot:
    def __init__(self, *, member: dict[str, Any] | None = None, error: Exception | None = None):
        self.member = member
        self.error = error
        self.deleted: list[Sequence[int]] = []

    def _maybe_fail(self) -> None:
        if self.error is not None:
            raise self.error

    async def get_chat_member(self, chat_id: int, user_id: int) -> ChatMember:
        self._maybe_fail()
        assert self.member is not None
        member = ChatMember.de_json(dict(self.member), cast(Bot, None))
        assert member is not None
        return member

    async def ban_chat_member(self, chat_id: int, user_id: int) -> bool:
        self._maybe_fail()
        return True

    async def delete_messages(self, chat_id: int, message_ids: Sequence[int]) -> bool:
        self._maybe_fail()
        self.deleted.append(message_ids)
        return True


def _api(bot: StubBot) -> PtbModerationApi:
    return PtbModerationApi(cast(Bot, bot))


@pytest.mark.parametrize(
    ("status", "fields", "expected"),
    [
        ("creator", {}, MemberInfo(MemberRole.OWNER)),
        ("administrator", {}, MemberInfo(MemberRole.ADMIN, can_restrict_members=True)),
        (
            "administrator",
            {"can_restrict_members": False},
            MemberInfo(MemberRole.ADMIN, can_restrict_members=False),
        ),
        ("member", {}, MemberInfo(MemberRole.MEMBER)),
        ("left", {}, MemberInfo(MemberRole.LEFT)),
        ("kicked", {"until_date": 0}, MemberInfo(MemberRole.BANNED)),
    ],
)
async def test_member_roles(status: str, fields: dict[str, Any], expected: MemberInfo) -> None:
    member = UpdateFactory.chat_member(UpdateFactory.user(7), status, **fields)
    assert await _api(StubBot(member=member)).get_member(-100, 7) == expected


@pytest.mark.parametrize("retry_after", [5, timedelta(seconds=5)])
async def test_rate_limit_is_translated(retry_after: Any) -> None:
    with warnings.catch_warnings():  # PTB itself warns while building the error
        warnings.simplefilter("ignore", PTBDeprecationWarning)
        error = RetryAfter(retry_after)
    api = _api(StubBot(error=error))
    for call in (api.ban(-100, 7), api.delete_messages(-100, [1]), api.get_member(-100, 7)):
        with pytest.raises(RateLimitedError) as info:
            await call
        assert info.value.retry_after == 5


async def test_other_errors_are_translated() -> None:
    api = _api(StubBot(error=BadRequest("Not enough rights to restrict/unrestrict chat member")))
    for call in (api.ban(-100, 7), api.delete_messages(-100, [1]), api.get_member(-100, 7)):
        with pytest.raises(ModerationApiError):
            await call


async def test_delete_messages_passes_ids() -> None:
    bot = StubBot()
    await _api(bot).delete_messages(-100, [1, 2, 3])
    assert bot.deleted == [[1, 2, 3]]
