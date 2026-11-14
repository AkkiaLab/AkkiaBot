"""Flood-wait handling and send pacing around an :class:`Account`."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from akkiabot_e2e.errors import FloodWait, RateLimitExceeded
from akkiabot_e2e.transport import Account, ChatRef, Msg, Role, UserInfo

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class Pacer:
    """Retries calls after a ``FloodWait`` up to ``max_wait`` seconds and spaces out sends.

    Telegram's flood wait must be honoured: retrying earlier only extends it, and
    repeated violations can get the test account limited. Waits longer than
    ``max_wait`` stop the run with :class:`RateLimitExceeded` instead of hanging.
    """

    def __init__(
        self,
        *,
        max_wait: float = 120.0,
        max_retries: int = 3,
        send_interval: float = 2.0,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.max_wait = max_wait
        self.max_retries = max_retries
        self.send_interval = send_interval
        self._sleep = sleep
        self._clock = clock
        self._last_send: float | None = None
        self.waited: float = 0.0
        """Total seconds slept because of flood waits (reported)."""

    async def call[T](self, label: str, fn: Callable[[], Awaitable[T]]) -> T:
        attempt = 0
        while True:
            try:
                return await fn()
            except FloodWait as exc:
                attempt += 1
                if exc.seconds > self.max_wait or attempt > self.max_retries:
                    raise RateLimitExceeded(
                        f"{label}: Telegram 要求等待 {exc.seconds:g} 秒，超过上限"
                        f"（{self.max_wait:g} 秒 / {self.max_retries} 次），停止本次验收"
                    ) from None
                delay = exc.seconds + 1
                logger.warning("flood wait on %s, sleeping %.0fs", label, delay)
                self.waited += delay
                await self._sleep(delay)

    async def before_send(self) -> None:
        now = self._clock()
        if self._last_send is not None:
            remaining = self.send_interval - (now - self._last_send)
            if remaining > 0:
                await self._sleep(remaining)
        self._last_send = self._clock()


class PacedAccount:
    """:class:`Account` wrapper routing every call through a shared :class:`Pacer`."""

    def __init__(self, inner: Account, pacer: Pacer, name: str) -> None:
        self.inner = inner
        self.pacer = pacer
        self.name = name

    def _label(self, op: str) -> str:
        return f"{self.name}.{op}"

    async def me(self) -> UserInfo:
        return await self.pacer.call(self._label("me"), self.inner.me)

    async def resolve_user(self, username: str) -> UserInfo:
        return await self.pacer.call(
            self._label("resolve_user"), lambda: self.inner.resolve_user(username)
        )

    async def find_chat(self, chat_id: int) -> ChatRef | None:
        return await self.pacer.call(
            self._label("find_chat"), lambda: self.inner.find_chat(chat_id)
        )

    async def owned_supergroups(self, title: str) -> list[ChatRef]:
        return await self.pacer.call(
            self._label("owned_supergroups"), lambda: self.inner.owned_supergroups(title)
        )

    async def create_supergroup(self, title: str, about: str) -> ChatRef:
        return await self.pacer.call(
            self._label("create_supergroup"), lambda: self.inner.create_supergroup(title, about)
        )

    async def participant_role(self, chat_id: int, user_id: int) -> Role | None:
        return await self.pacer.call(
            self._label("participant_role"), lambda: self.inner.participant_role(chat_id, user_id)
        )

    async def admin_rights(self, chat_id: int, user_id: int) -> frozenset[str]:
        return await self.pacer.call(
            self._label("admin_rights"), lambda: self.inner.admin_rights(chat_id, user_id)
        )

    async def invite(self, chat_id: int, user_id: int) -> None:
        await self.pacer.call(self._label("invite"), lambda: self.inner.invite(chat_id, user_id))

    async def promote(self, chat_id: int, user_id: int, rights: frozenset[str]) -> None:
        await self.pacer.call(
            self._label("promote"), lambda: self.inner.promote(chat_id, user_id, rights)
        )

    async def export_invite_link(self, chat_id: int) -> str:
        return await self.pacer.call(
            self._label("export_invite_link"), lambda: self.inner.export_invite_link(chat_id)
        )

    async def join_by_link(self, link: str) -> int | None:
        return await self.pacer.call(
            self._label("join_by_link"), lambda: self.inner.join_by_link(link)
        )

    async def leave(self, chat_id: int) -> int | None:
        return await self.pacer.call(self._label("leave"), lambda: self.inner.leave(chat_id))

    async def is_banned(self, chat_id: int, user_id: int) -> bool:
        return await self.pacer.call(
            self._label("is_banned"), lambda: self.inner.is_banned(chat_id, user_id)
        )

    async def unban(self, chat_id: int, user_id: int) -> None:
        await self.pacer.call(self._label("unban"), lambda: self.inner.unban(chat_id, user_id))

    async def click(self, chat_id: int, message_id: int, data: bytes) -> str | None:
        await self.pacer.before_send()
        return await self.pacer.call(
            self._label("click"), lambda: self.inner.click(chat_id, message_id, data)
        )

    async def send(self, chat_id: int, text: str, *, reply_to: int | None = None) -> Msg:
        await self.pacer.before_send()
        return await self.pacer.call(
            self._label("send"), lambda: self.inner.send(chat_id, text, reply_to=reply_to)
        )

    async def history(self, chat_id: int, *, min_id: int, limit: int = 50) -> list[Msg]:
        return await self.pacer.call(
            self._label("history"), lambda: self.inner.history(chat_id, min_id=min_id, limit=limit)
        )

    async def get_message(self, chat_id: int, message_id: int) -> Msg | None:
        return await self.pacer.call(
            self._label("get_message"), lambda: self.inner.get_message(chat_id, message_id)
        )

    async def close(self) -> None:
        await self.inner.close()
