"""Test helpers for Telegram interactions (used by the test suites only).

* :class:`FakeBotApi` replaces the HTTP layer of a real PTB ``Bot``: every Bot
  API call is recorded instead of sent, and answered with a canned result.
  Handlers therefore run unchanged, including ``message.reply_text()``.
* :class:`UpdateFactory` builds realistic :class:`telegram.Update` objects
  (group / private messages, replies, service messages, membership changes)
  bound to a bot, with increasing ids.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, override

from telegram import Bot, Update
from telegram.request import BaseRequest, RequestData

from akkiabot_core.observability import REGISTRY

BOT_ID = 100_000
BOT_USERNAME = "akkiabot_test_bot"
BOT_USER: dict[str, Any] = {
    "id": BOT_ID,
    "is_bot": True,
    "first_name": "AkkiaBot",
    "username": BOT_USERNAME,
    "can_join_groups": True,
    "can_read_all_group_messages": True,
    "supports_inline_queries": False,
}


@dataclass(frozen=True, slots=True)
class ApiCall:
    method: str
    params: dict[str, Any]


class ApiError(Exception):
    """Raise (or return) from a responder to make the Bot API call fail."""

    def __init__(
        self, description: str, *, status: int = 400, retry_after: int | None = None
    ) -> None:
        super().__init__(description)
        self.description = description
        self.status = status
        self.retry_after = retry_after
        """Sent as ``parameters.retry_after`` (PTB then raises ``RetryAfter``)."""


type Responder = Callable[[dict[str, Any]], Any]


class FakeBotApi(BaseRequest):
    """In-memory Bot API. Use one instance for both ``request`` and
    ``get_updates_request`` of the application."""

    def __init__(self) -> None:
        self.calls: list[ApiCall] = []
        self.responders: dict[str, Responder] = {}
        self._message_ids = itertools.count(10_000)

    # -- configuration -----------------------------------------------------
    def respond(self, method: str, responder: Responder) -> None:
        """Override the result for ``method``; the responder gets the params."""
        self.responders[method] = responder

    def fail(
        self, method: str, description: str = "Bad Request: test failure", *, status: int = 400
    ) -> None:
        def raise_error(_params: dict[str, Any]) -> Any:
            raise ApiError(description, status=status)

        self.respond(method, raise_error)

    # -- inspection --------------------------------------------------------
    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [c.params for c in self.calls if c.method == method]

    def sent_texts(self) -> list[str]:
        return [p["text"] for p in self.calls_to("sendMessage")]

    def clear(self) -> None:
        self.calls.clear()

    # -- BaseRequest -------------------------------------------------------
    @property
    @override
    def read_timeout(self) -> float | None:
        return None

    @override
    async def initialize(self) -> None:
        return None

    @override
    async def shutdown(self) -> None:
        return None

    @override
    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = None,
        write_timeout: Any = None,
        connect_timeout: Any = None,
        pool_timeout: Any = None,
    ) -> tuple[int, bytes]:
        endpoint = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append(ApiCall(endpoint, params))
        responder = self.responders.get(endpoint)
        try:
            if responder is not None:
                result: Any = responder(params)
            elif endpoint == "getUpdates":
                await asyncio.sleep(0.05)  # behave like an (empty) long poll
                result = []
            elif endpoint == "getMe":
                result = BOT_USER
            elif endpoint == "sendMessage":
                result = self._sent(params)
            else:
                result = True  # deleteMessage, setMyCommands, setWebhook…
        except ApiError as exc:
            body: dict[str, Any] = {
                "ok": False,
                "error_code": exc.status,
                "description": exc.description,
            }
            if exc.retry_after is not None:
                body["parameters"] = {"retry_after": exc.retry_after}
            return exc.status, json.dumps(body).encode()
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def _sent(self, params: dict[str, Any]) -> dict[str, Any]:
        """A plausible ``Message`` for send* methods."""
        message: dict[str, Any] = {
            "message_id": next(self._message_ids),
            "date": int(time.time()),
            "chat": {
                "id": params["chat_id"],
                "type": "private" if params["chat_id"] > 0 else "supergroup",
            },
            "from": BOT_USER,
        }
        if "text" in params:
            message["text"] = params["text"]
        return message


_ADMIN_RIGHTS = (
    "can_be_edited",
    "can_manage_chat",
    "can_delete_messages",
    "can_manage_video_chats",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_post_stories",
    "can_edit_stories",
    "can_delete_stories",
)


class UpdateFactory:
    """Builds updates bound to ``bot``. Ids increase on every call."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(1)

    # -- building blocks ---------------------------------------------------
    @staticmethod
    def user(
        user_id: int = 1, first_name: str = "Alice", *, is_bot: bool = False, **extra: Any
    ) -> dict[str, Any]:
        return {"id": user_id, "is_bot": is_bot, "first_name": first_name, **extra}

    @staticmethod
    def group(
        chat_id: int = -1001234567890, title: str = "Test Group", *, type: str = "supergroup"
    ) -> dict[str, Any]:
        return {"id": chat_id, "type": type, "title": title}

    @staticmethod
    def private(user: dict[str, Any]) -> dict[str, Any]:
        return {"id": user["id"], "type": "private", "first_name": user["first_name"]}

    @staticmethod
    def chat_member(user: dict[str, Any], status: str = "member", **fields: Any) -> dict[str, Any]:
        """A ``ChatMember``; administrators get full rights unless overridden."""
        member: dict[str, Any] = {"status": status, "user": user}
        if status == "administrator":
            member |= dict.fromkeys(_ADMIN_RIGHTS, True) | {"is_anonymous": False}
        elif status == "creator":
            member["is_anonymous"] = False
        return member | fields

    def message_dict(
        self,
        *,
        chat: dict[str, Any],
        from_user: dict[str, Any] | None,
        text: str | None = None,
        reply_to: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "message_id": next(self._message_ids),
            "date": int(time.time()),
            "chat": chat,
            **fields,
        }
        if from_user is not None:
            message["from"] = from_user
        if text is not None:
            message["text"] = text
            if text.startswith("/"):
                command = text.split(maxsplit=1)[0]
                message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        if reply_to is not None:
            message["reply_to_message"] = reply_to
        return message

    def _update(self, **payload: Any) -> Update:
        update = Update.de_json({"update_id": next(self._update_ids), **payload}, self.bot)
        assert update is not None  # noqa: S101
        return update

    # -- updates -----------------------------------------------------------
    def message(
        self,
        text: str | None = "hello",
        *,
        chat: dict[str, Any] | None = None,
        from_user: dict[str, Any] | None = None,
        reply_to: dict[str, Any] | None = None,
        **fields: Any,
    ) -> Update:
        """A message; defaults to a text message from user 1 in a supergroup."""
        sender = from_user if from_user is not None else self.user()
        return self._update(
            message=self.message_dict(
                chat=chat if chat is not None else self.group(),
                from_user=sender,
                text=text,
                reply_to=reply_to,
                **fields,
            )
        )

    def command(self, command: str, *args: str, **kwargs: Any) -> Update:
        """``/command arg…`` as a message (see :meth:`message` for kwargs)."""
        return self.message(" ".join([command, *args]), **kwargs)

    def service_message(
        self,
        *,
        chat: dict[str, Any] | None = None,
        from_user: dict[str, Any] | None = None,
        **service: Any,
    ) -> Update:
        """A status update, e.g. ``new_chat_members=[user]`` or ``left_chat_member=user``."""
        if not service:
            joined = from_user or self.user()
            service = {"new_chat_members": [joined]}
        return self.message(None, chat=chat, from_user=from_user, **service)

    def my_chat_member(
        self,
        *,
        chat: dict[str, Any] | None = None,
        old_status: str = "left",
        new_status: str = "member",
        by: dict[str, Any] | None = None,
    ) -> Update:
        """The bot's own membership in ``chat`` changed."""
        return self._update(
            my_chat_member={
                "chat": chat if chat is not None else self.group(),
                "from": by or self.user(),
                "date": int(time.time()),
                "old_chat_member": self.chat_member(BOT_USER, old_status),
                "new_chat_member": self.chat_member(BOT_USER, new_status),
            }
        )

    def callback_query(
        self,
        data: str,
        *,
        from_user: dict[str, Any] | None = None,
        message: dict[str, Any] | None = None,
    ) -> Update:
        """An inline button press; ``message`` defaults to a bot message in the user's private chat."""
        sender = from_user if from_user is not None else self.user()
        if message is None:
            message = self.message_dict(chat=self.private(sender), from_user=BOT_USER, text="menu")
        return self._update(
            callback_query={
                "id": str(next(self._update_ids)),
                "from": sender,
                "chat_instance": "test",
                "data": data,
                "message": message,
            }
        )


def member_responder(members: dict[int, dict[str, Any]]) -> Responder:
    """``getChatMember`` responder: ``{user_id: ChatMember dict}``; unknown users are ``left``."""

    def respond(params: dict[str, Any]) -> Any:
        user_id = int(params["user_id"])
        member = members.get(user_id)
        if member is None:
            return UpdateFactory.chat_member(UpdateFactory.user(user_id), "left")
        return copy.deepcopy(member)  # PTB pops keys while parsing

    return respond


def metric_value(name: str, **labels: str) -> float:
    """Current value of a sample in the app registry (0 when absent)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0
