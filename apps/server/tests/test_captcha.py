"""Join captcha end to end: chat_member update → /start → POST /api/v1/captcha/verify → timeout.

Runs the real PTB application and FastAPI app on SQLite; the Bot API is
:class:`FakeBotApi`, Turnstile's ``siteverify`` is mocked with respx.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
import respx
from sqlalchemy import update

from akkiabot_core.config import Settings
from akkiabot_core.db import (
    CaptchaChallenge,
    CaptchaChallengeRepository,
    CaptchaStatus,
    ChatMemberRepository,
    Restriction,
    session_scope,
)
from akkiabot_core.db.types import utcnow
from akkiabot_core.telegram import sign_init_data
from akkiabot_server.api import create_app
from akkiabot_server.bot.captcha import (
    START_EXPIRED,
    START_TEXT,
    START_WRONG_USER,
    SWEEP_JOB,
    TIMEOUT_JOB,
    PtbCaptchaApi,
    prompt_text,
)
from akkiabot_server.runtime import Runtime
from akkiabot_server.services.captcha import CaptchaRejectedError, RejectReason, redact_name
from akkiabot_server.services.chat_settings import Toggle
from akkiabot_server.testing import FakeBotApi, UpdateFactory, metric_value

from .conftest import MOCK_BOT_TOKEN, MakeSettings

CHAT = -1001234567890
NEWBIE, STRANGER, ADMIN = 4242, 777, 1
WEBAPP = "https://bot.example.com"
SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
VERIFY = "/api/v1/captcha/verify"
# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_TURNSTILE_SECRET = "fake-turnstile-secret-for-tests"
# mock: 人工构造的不可用测试值，不是真实 Turnstile token。
MOCK_TURNSTILE_TOKEN = "fake-turnstile-token-for-tests"


@pytest.fixture
def settings(make_settings: MakeSettings) -> Settings:
    return make_settings(
        webapp={"url": WEBAPP},
        turnstile={"secret_key": MOCK_TURNSTILE_SECRET},
        cors={"allow_origins": WEBAPP},
    )


@pytest.fixture
def turnstile() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock.post(SITEVERIFY).mock(
            return_value=httpx.Response(200, json={"success": True, "error-codes": []})
        )


@pytest.fixture
async def client(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(runtime)  # lifespan not run: the runtime fixture is already initialised
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


@pytest.fixture(autouse=True)
def _answer_web_app_query(api: FakeBotApi) -> None:
    api.respond("answerWebAppQuery", lambda _p: {"inline_message_id": "fake-inline-message"})


class TelegramMembers:
    """Remembers restrictChatMember calls so getChatMember answers like Telegram."""

    def __init__(self, api: FakeBotApi) -> None:
        self.api = api
        self.members: dict[int, dict[str, Any]] = {}
        api.respond("restrictChatMember", self.restrict)
        api.respond("getChatMember", self.get)

    def restrict(self, params: dict[str, Any]) -> bool:
        user_id = int(params["user_id"])
        permissions = _json(params["permissions"])
        user = UpdateFactory.user(user_id, "Newbie")
        if all(permissions.values()):
            self.members[user_id] = UpdateFactory.chat_member(user, "member")
        else:
            self.set(user_id, **permissions)
        return True

    def set(self, user_id: int, **fields: Any) -> None:
        """Restrict ``user_id`` directly, as an admin would in the Telegram UI."""
        user = UpdateFactory.user(user_id, "Newbie")
        self.members[user_id] = UpdateFactory.chat_member(user, "restricted", **fields)

    def get(self, params: dict[str, Any]) -> dict[str, Any]:
        user_id = int(params["user_id"])
        member = self.members.get(user_id)
        if member is None:
            return UpdateFactory.chat_member(UpdateFactory.user(user_id, "Newbie"), "member")
        return json.loads(json.dumps(member))

    def restore(self) -> None:
        self.api.respond("restrictChatMember", self.restrict)


@pytest.fixture
def telegram(api: FakeBotApi) -> TelegramMembers:
    return TelegramMembers(api)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def init_data(
    user_id: int = NEWBIE, *, age: int = 0, query_id: str | None = "fake-query-id"
) -> str:
    fields = {
        "auth_date": str(int(time.time()) - age),
        "user": json.dumps({"id": user_id, "first_name": "Newbie"}),
    }
    if query_id is not None:
        fields["query_id"] = query_id
    fields["hash"] = sign_init_data(fields, MOCK_BOT_TOKEN)
    return urlencode(fields)


def body(challenge_id: uuid.UUID | str, **overrides: Any) -> dict[str, Any]:
    return {
        "challenge_id": str(challenge_id),
        "init_data": init_data(),
        "turnstile_token": MOCK_TURNSTILE_TOKEN,
    } | overrides


class Flow:
    """Drives the captcha through the real handlers."""

    def __init__(self, runtime: Runtime, api: FakeBotApi, updates: UpdateFactory) -> None:
        self.runtime, self.api, self.updates = runtime, api, updates
        self.chat = updates.group(CHAT, "Group")

    async def enable(self, enabled: bool = True) -> None:
        deps = self.runtime.deps
        await deps.chats.refresh(CHAT, type="supergroup", title="Group")
        await deps.chat_settings.set_toggle(CHAT, Toggle.CAPTCHA, enabled)

    async def feed(self, update: Any) -> None:
        await self.runtime.application.process_update(update)

    async def join(
        self, user_id: int = NEWBIE, name: str = "Newbie Person", *, old: Any = "left"
    ) -> None:
        user = self.updates.user(user_id, name)
        await self.feed(
            self.updates.chat_member_update(user, old=old, new="member", chat=self.chat)
        )

    async def leave(self, user_id: int = NEWBIE) -> None:
        user = self.updates.user(user_id, "Newbie")
        muted = UpdateFactory.chat_member(user, "restricted")
        await self.feed(
            self.updates.chat_member_update(user, old=muted, new="left", chat=self.chat)
        )

    async def rejoin_muted(self, user_id: int = NEWBIE) -> None:
        """Telegram keeps the restriction: left → restricted(is_member=True)."""
        user = self.updates.user(user_id, "Newbie")
        muted = UpdateFactory.chat_member(user, "restricted")
        await self.feed(
            self.updates.chat_member_update(user, old="left", new=muted, chat=self.chat)
        )

    async def start(self, token: str, user_id: int = NEWBIE) -> dict[str, Any]:
        user = self.updates.user(user_id, "Someone")
        await self.feed(
            self.updates.command("/start", token, chat=self.updates.private(user), from_user=user)
        )
        return self.api.calls_to("sendMessage")[-1]

    async def challenges(self, user_id: int = NEWBIE) -> list[CaptchaChallenge]:
        async with session_scope(self.runtime.session_factory) as session:
            repo = CaptchaChallengeRepository(session)
            pending = list(await repo.list_pending(CHAT, user_id))
            return pending

    async def pending(self, user_id: int = NEWBIE) -> CaptchaChallenge:
        (challenge,) = await self.challenges(user_id)
        return challenge

    async def status(self, challenge_id: uuid.UUID) -> CaptchaStatus:
        async with session_scope(self.runtime.session_factory) as session:
            challenge = await CaptchaChallengeRepository(session).get(challenge_id)
            assert challenge is not None
            return challenge.status

    async def restriction(self, user_id: int = NEWBIE) -> Restriction:
        async with session_scope(self.runtime.session_factory) as session:
            member = await ChatMemberRepository(session).get(CHAT, user_id)
            return member.restriction if member else Restriction.NONE

    async def set_restriction(self, restriction: Restriction, user_id: int = NEWBIE) -> None:
        async with session_scope(self.runtime.session_factory) as session:
            await ChatMemberRepository(session).set_restriction(CHAT, user_id, restriction)

    def restricts(self) -> list[tuple[int, bool]]:
        """``(user_id, can_send_messages)`` of every restrictChatMember call."""
        return [
            (int(p["user_id"]), _json(p["permissions"])["can_send_messages"])
            for p in self.api.calls_to("restrictChatMember")
        ]

    def prompt_token(self) -> str:
        prompt = self.api.calls_to("sendMessage")[-1]
        (row,) = _json(prompt["reply_markup"])["inline_keyboard"]
        url = row[0]["url"]
        assert url.startswith("https://t.me/akkiabot_test_bot?start=")
        return url.rsplit("=", 1)[1]

    async def expire_now(self) -> int:
        later = utcnow() + timedelta(seconds=self.runtime.settings.webapp.captcha_timeout + 1)
        return await self.runtime.deps.captcha.expire_due(
            PtbCaptchaApi(self.runtime.application.bot), now=later
        )


@pytest.fixture
async def flow(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, telegram: TelegramMembers
) -> Flow:
    f = Flow(runtime, api, updates)
    await f.enable()
    return f


# ------------------------------------------------------------------ join


async def test_join_mutes_and_posts_redacted_prompt(flow: Flow, api: FakeBotApi) -> None:
    started = metric_value("akkiabot_captcha_started_total")
    await flow.join(name="Newbie Person")

    assert flow.restricts() == [(NEWBIE, False)]
    challenge = await flow.pending()
    assert challenge.expires_at - challenge.created_at == timedelta(minutes=5)
    assert await flow.restriction() is Restriction.CAPTCHA_MUTE

    prompt = api.calls_to("sendMessage")[-1]
    assert int(prompt["chat_id"]) == CHAT
    assert prompt["parse_mode"] == "HTML"
    assert prompt["text"] == prompt_text(NEWBIE, "Ne…on", 300)
    assert "Newbie Person" not in prompt["text"]
    assert challenge.group_message_id is not None
    # Timeout job for this challenge, plus the periodic sweep.
    job_queue = flow.runtime.application.job_queue
    assert job_queue is not None
    assert job_queue.get_jobs_by_name(f"{TIMEOUT_JOB}:{challenge.id}")
    assert job_queue.get_jobs_by_name(SWEEP_JOB)
    assert metric_value("akkiabot_captcha_started_total") == started + 1


@pytest.mark.parametrize(
    ("name", "shown"), [("Bob", "Bob"), ("Alice", "Al…ce"), ("  ", "新成员"), ("<b>x</b>", "<b…b>")]
)
def test_redact_name(name: str, shown: str) -> None:
    assert redact_name(name) == shown
    assert "&lt;b" in prompt_text(1, redact_name("<b>x</b>"), 300)


async def test_join_ignored_when_captcha_disabled(flow: Flow, api: FakeBotApi) -> None:
    started = metric_value("akkiabot_captcha_started_total")
    await flow.enable(False)
    await flow.join()
    assert api.calls_to("restrictChatMember") == []
    assert await flow.challenges() == []
    assert metric_value("akkiabot_captcha_started_total") == started


async def test_bots_admins_and_non_joins_are_ignored(
    flow: Flow, api: FakeBotApi, updates: UpdateFactory
) -> None:
    bot = updates.user(555, "SomeBot", is_bot=True)
    await flow.feed(updates.chat_member_update(bot, chat=flow.chat))
    admin = updates.user(ADMIN, "Admin")
    await flow.feed(updates.chat_member_update(admin, new="administrator", chat=flow.chat))
    # member → member (e.g. a title change) is not a join
    user = updates.user(NEWBIE, "Newbie")
    await flow.feed(updates.chat_member_update(user, old="member", new="member", chat=flow.chat))
    # private chats are not groups
    await flow.feed(updates.chat_member_update(user, chat=updates.private(user)))
    assert api.calls_to("restrictChatMember") == []


async def test_member_with_other_restriction_is_left_alone(flow: Flow, api: FakeBotApi) -> None:
    await flow.set_restriction(Restriction.SPAM_MUTE)
    await flow.rejoin_muted()
    assert api.calls_to("restrictChatMember") == []
    assert await flow.challenges() == []
    assert await flow.restriction() is Restriction.SPAM_MUTE


async def test_mute_failure_creates_no_challenge(flow: Flow, api: FakeBotApi) -> None:
    api.fail(
        "restrictChatMember", "Bad Request: not enough rights to restrict/unrestrict chat member"
    )
    before = metric_value("akkiabot_telegram_api_errors_total", method="restrictChatMember")
    await flow.join()
    assert await flow.challenges() == []
    assert api.calls_to("sendMessage") == []
    assert (
        metric_value("akkiabot_telegram_api_errors_total", method="restrictChatMember")
        == before + 1
    )


# ---------------------------------------------------------------- /start


async def test_start_by_challenged_user_opens_mini_app(flow: Flow) -> None:
    await flow.join()
    challenge = await flow.pending()
    reply = await flow.start(flow.prompt_token())
    assert reply["text"] == START_TEXT
    (row,) = _json(reply["reply_markup"])["inline_keyboard"]
    # Only the challenge id travels in the URL.
    assert row[0]["web_app"] == {"url": f"{WEBAPP}/captcha?challenge={challenge.id}"}


async def test_start_by_someone_else_is_refused_and_keeps_the_link(flow: Flow) -> None:
    await flow.join()
    token = flow.prompt_token()
    stranger = await flow.start(token, user_id=STRANGER)
    assert stranger["text"] == START_WRONG_USER
    assert "reply_markup" not in stranger
    # The stranger did not burn the link.
    assert (await flow.start(token))["text"] == START_TEXT


async def test_start_after_timeout(flow: Flow) -> None:
    await flow.join()
    token = flow.prompt_token()
    await flow.expire_now()
    assert (await flow.start(token))["text"] == START_EXPIRED


# ---------------------------------------------------------------- verify


async def test_verify_passes_and_unmutes(
    flow: Flow, api: FakeBotApi, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    passed = metric_value("akkiabot_captcha_total", result="passed")
    durations = metric_value("akkiabot_captcha_duration_seconds_count")
    api.clear()

    response = await client.post(VERIFY, json=body(challenge.id))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "passed"
    # Turnstile got the secret and the token.
    sent = parse_qs(turnstile.calls.last.request.content.decode())
    assert sent == {"secret": [MOCK_TURNSTILE_SECRET], "response": [MOCK_TURNSTILE_TOKEN]}
    assert flow.restricts() == [(NEWBIE, True)]
    assert await flow.restriction() is Restriction.NONE
    assert await flow.status(challenge.id) is CaptchaStatus.PASSED
    assert [int(p["message_id"]) for p in api.calls_to("deleteMessage")] == [
        challenge.group_message_id
    ]
    (receipt,) = api.calls_to("answerWebAppQuery")
    assert receipt["web_app_query_id"] == "fake-query-id"
    assert "验证成功" in _json(receipt["result"])["input_message_content"]["message_text"]
    assert metric_value("akkiabot_captcha_total", result="passed") == passed + 1
    assert metric_value("akkiabot_captcha_duration_seconds_count") == durations + 1


async def test_verify_without_query_id_skips_receipt(
    flow: Flow, api: FakeBotApi, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    response = await client.post(
        VERIFY, json=body(challenge.id, init_data=init_data(query_id=None))
    )
    assert response.status_code == 200
    assert api.calls_to("answerWebAppQuery") == []


async def test_replayed_challenge_is_rejected(
    flow: Flow, api: FakeBotApi, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    assert (await client.post(VERIFY, json=body(challenge.id))).status_code == 200
    failed = metric_value("akkiabot_captcha_total", result="failed")

    replay = await client.post(VERIFY, json=body(challenge.id))

    assert replay.status_code == 409
    assert replay.json()["error"] == "challenge_used"
    assert [r for r in flow.restricts() if r[1]] == [(NEWBIE, True)]  # unmuted once
    assert metric_value("akkiabot_captcha_total", result="failed") == failed + 1


async def test_verify_as_someone_else_is_rejected(
    flow: Flow, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    response = await client.post(VERIFY, json=body(challenge.id, init_data=init_data(STRANGER)))
    assert response.status_code == 403
    assert response.json()["error"] == "wrong_user"
    assert flow.restricts() == [(NEWBIE, False)]
    # The real user can still pass.
    assert await flow.status(challenge.id) is CaptchaStatus.PENDING
    assert (await client.post(VERIFY, json=body(challenge.id))).status_code == 200


async def test_expired_init_data_is_rejected(
    flow: Flow, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    max_age = flow.runtime.settings.webapp.init_data_max_age
    stale = init_data(age=max_age + 60)
    response = await client.post(VERIFY, json=body(challenge.id, init_data=stale))
    assert response.status_code == 401
    assert response.json()["error"] == "init_data_expired"
    assert flow.restricts() == [(NEWBIE, False)]
    assert await flow.status(challenge.id) is CaptchaStatus.PENDING


async def test_forged_init_data_is_rejected(
    flow: Flow, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    forged = init_data().replace("Newbie", "Hacker")
    response = await client.post(VERIFY, json=body(challenge.id, init_data=forged))
    assert response.status_code == 401
    assert response.json()["error"] == "init_data_invalid"


async def test_turnstile_failure_is_rejected_first(
    flow: Flow, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    turnstile.mock(
        return_value=httpx.Response(
            200, json={"success": False, "error-codes": ["invalid-input-response"]}
        )
    )
    # Even a forged initData is not looked at before Turnstile passed.
    response = await client.post(VERIFY, json=body(challenge.id, init_data="garbage"))
    assert response.status_code == 400
    assert response.json()["error"] == "turnstile_failed"
    assert flow.restricts() == [(NEWBIE, False)]
    assert await flow.status(challenge.id) is CaptchaStatus.PENDING


async def test_turnstile_unreachable_is_a_failure(
    flow: Flow, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    turnstile.mock(side_effect=httpx.ConnectTimeout("timeout"))
    response = await client.post(VERIFY, json=body(challenge.id))
    assert response.status_code == 400
    assert response.json()["error"] == "turnstile_failed"


async def test_non_captcha_mute_cannot_be_lifted(
    flow: Flow, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    # An admin muted the member for spam while the captcha was pending.
    await flow.set_restriction(Restriction.SPAM_MUTE)
    response = await client.post(VERIFY, json=body(challenge.id))
    assert response.status_code == 409
    assert response.json()["error"] == "not_captcha_restricted"
    assert flow.restricts() == [(NEWBIE, False)]
    assert await flow.restriction() is Restriction.SPAM_MUTE


async def test_unknown_challenge(client: httpx.AsyncClient, turnstile: respx.Route) -> None:
    response = await client.post(VERIFY, json=body(uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()["error"] == "challenge_not_found"


async def test_unmute_failure_lets_the_user_retry(
    flow: Flow,
    api: FakeBotApi,
    telegram: TelegramMembers,
    client: httpx.AsyncClient,
    turnstile: respx.Route,
) -> None:
    await flow.join()
    challenge = await flow.pending()
    api.fail("restrictChatMember", "Bad Request: not enough rights")
    response = await client.post(VERIFY, json=body(challenge.id))
    assert response.status_code == 502
    assert response.json()["error"] == "unmute_failed"
    assert await flow.status(challenge.id) is CaptchaStatus.PENDING
    assert await flow.restriction() is Restriction.CAPTCHA_MUTE

    telegram.restore()
    assert (await client.post(VERIFY, json=body(challenge.id))).status_code == 200


async def test_verify_unavailable_without_turnstile_secret(
    make_settings: MakeSettings, api: FakeBotApi
) -> None:
    from akkiabot_core.cache import MemoryCache

    runtime = Runtime.build(
        make_settings(), cache=MemoryCache(), request=api, get_updates_request=api
    )
    app = create_app(runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.post(VERIFY, json=body(uuid.uuid4()))
    finally:
        await runtime.http.aclose()
        await runtime.engine.dispose()
    assert response.status_code == 503
    assert response.json()["error"] == "captcha_unavailable"


async def test_malformed_request_is_422(client: httpx.AsyncClient) -> None:
    response = await client.post(VERIFY, json={"challenge_id": "nope", "init_data": ""})
    assert response.status_code == 422


class GatedApi(PtbCaptchaApi):
    """PtbCaptchaApi whose live check / unmute can be paused or sabotaged."""

    def __init__(self, runtime: Runtime) -> None:
        super().__init__(runtime.application.bot)
        self.runtime = runtime
        self.checking = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()
        self.before_unmute: Callable[[], Awaitable[None]] | None = None

    async def is_captcha_muted(self, chat_id: int, user_id: int) -> bool:
        self.checking.set()
        await self.proceed.wait()
        return await super().is_captcha_muted(chat_id, user_id)

    async def unmute(self, chat_id: int, user_id: int) -> None:
        if self.before_unmute is not None:
            await self.before_unmute()
        await super().unmute(chat_id, user_id)


async def _expire_in_db(runtime: Runtime, challenge_id: uuid.UUID) -> None:
    async with session_scope(runtime.session_factory) as session:
        await session.execute(
            update(CaptchaChallenge)
            .where(CaptchaChallenge.id == challenge_id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )


async def test_admin_restricting_a_pending_member_is_not_lifted(
    flow: Flow,
    updates: UpdateFactory,
    telegram: TelegramMembers,
    client: httpx.AsyncClient,
    turnstile: respx.Route,
) -> None:
    await flow.join()
    challenge = await flow.pending()
    # The admin re-restricts the muted member in Telegram (restricted → restricted).
    user = updates.user(NEWBIE, "Newbie")
    old = UpdateFactory.chat_member(user, "restricted")
    telegram.set(NEWBIE, until_date=int(time.time()) + 86_400)
    new = telegram.get({"user_id": NEWBIE})
    admin = updates.user(ADMIN, "Admin")
    await flow.feed(updates.chat_member_update(user, old=old, new=new, chat=flow.chat, by=admin))
    assert await flow.restriction() is Restriction.ADMIN_MUTE

    response = await client.post(VERIFY, json=body(challenge.id))
    assert response.status_code == 409
    assert response.json()["error"] == "not_captcha_restricted"
    assert flow.restricts() == [(NEWBIE, False)]  # the admin's restriction stays
    assert await flow.status(challenge.id) is CaptchaStatus.PENDING


async def test_admin_restricting_a_member_is_recorded_but_own_mute_is_not(
    flow: Flow, updates: UpdateFactory
) -> None:
    user = updates.user(NEWBIE, "Newbie")
    muted = UpdateFactory.chat_member(user, "restricted")
    bot = updates.user(100_000, "AkkiaBot", is_bot=True)
    # Our own restrictChatMember echoes back from the bot: ignored.
    await flow.feed(
        updates.chat_member_update(user, old="member", new=muted, chat=flow.chat, by=bot)
    )
    assert await flow.restriction() is Restriction.NONE
    admin = updates.user(ADMIN, "Admin")
    await flow.feed(
        updates.chat_member_update(user, old="member", new=muted, chat=flow.chat, by=admin)
    )
    assert await flow.restriction() is Restriction.ADMIN_MUTE
    # A recorded spam mute is more specific and is kept.
    await flow.set_restriction(Restriction.SPAM_MUTE)
    await flow.feed(
        updates.chat_member_update(user, old=muted, new=muted, chat=flow.chat, by=admin)
    )
    assert await flow.restriction() is Restriction.SPAM_MUTE


async def test_live_restriction_change_not_yet_synced_is_not_lifted(
    flow: Flow, telegram: TelegramMembers, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    # Telegram already shows the admin's restriction; its update is still in flight.
    telegram.set(NEWBIE, can_send_messages=True)
    response = await client.post(VERIFY, json=body(challenge.id))
    assert response.status_code == 409
    assert response.json()["error"] == "not_captcha_restricted"
    assert flow.restricts() == [(NEWBIE, False)]
    assert await flow.status(challenge.id) is CaptchaStatus.PENDING


async def test_member_lookup_failure_does_not_unmute(
    flow: Flow, api: FakeBotApi, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    api.fail("getChatMember", "Bad Request: user not found")
    response = await client.post(VERIFY, json=body(challenge.id))
    assert response.status_code == 502
    assert flow.restricts() == [(NEWBIE, False)]
    assert await flow.status(challenge.id) is CaptchaStatus.PENDING


async def test_restriction_sync_waits_for_an_inflight_verification(
    flow: Flow, runtime: Runtime, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    gated = GatedApi(runtime)
    gated.proceed.clear()
    service = runtime.deps.captcha
    verify = asyncio.create_task(
        service.verify(
            gated,
            challenge_id=challenge.id,
            init_data=init_data(),
            turnstile_token=MOCK_TURNSTILE_TOKEN,
        )
    )
    await gated.checking.wait()  # verify holds the member lock
    sync = asyncio.create_task(service.handle_restricted(CHAT, NEWBIE, by=ADMIN))
    await asyncio.sleep(0.05)
    assert not sync.done()  # serialised behind the verification

    gated.proceed.set()
    await verify
    assert await sync is True
    # The later admin restriction wins over the verification's "none".
    assert await flow.restriction() is Restriction.ADMIN_MUTE


async def test_verify_waits_for_an_inflight_restriction_sync(
    flow: Flow, runtime: Runtime, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    service = runtime.deps.captcha
    async with service._member_lock(CHAT, NEWBIE):  # pyright: ignore[reportPrivateUsage]
        verify = asyncio.create_task(
            service.verify(
                GatedApi(runtime),
                challenge_id=challenge.id,
                init_data=init_data(),
                turnstile_token=MOCK_TURNSTILE_TOKEN,
            )
        )
        await asyncio.sleep(0.05)
        assert not verify.done()
        # The admin restriction lands while verify waits for the lock.
        async with session_scope(runtime.session_factory) as session:
            await ChatMemberRepository(session).set_restriction(
                CHAT, NEWBIE, Restriction.ADMIN_MUTE
            )
    with pytest.raises(CaptchaRejectedError) as exc_info:
        await verify
    assert exc_info.value.reason is RejectReason.NOT_CAPTCHA_RESTRICTED
    assert flow.restricts() == [(NEWBIE, False)]


async def test_unmute_failing_after_the_deadline_finishes_as_timeout(
    flow: Flow, runtime: Runtime, api: FakeBotApi, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    gated = GatedApi(runtime)

    async def deadline_passes_then_telegram_fails() -> None:
        await _expire_in_db(runtime, challenge.id)  # claimed in time, now overdue
        api.fail("restrictChatMember", "Bad Request: not enough rights")

    gated.before_unmute = deadline_passes_then_telegram_fails
    timeouts = metric_value("akkiabot_captcha_total", result="timeout")

    with pytest.raises(CaptchaRejectedError) as exc_info:
        await runtime.deps.captcha.verify(
            gated,
            challenge_id=challenge.id,
            init_data=init_data(),
            turnstile_token=MOCK_TURNSTILE_TOKEN,
        )

    assert exc_info.value.reason is RejectReason.CHALLENGE_EXPIRED
    assert await flow.status(challenge.id) is CaptchaStatus.EXPIRED  # not left as passed
    assert await flow.restriction() is Restriction.CAPTCHA_MUTE  # still muted
    assert [int(p["message_id"]) for p in api.calls_to("deleteMessage")] == [
        challenge.group_message_id
    ]
    assert metric_value("akkiabot_captcha_total", result="timeout") == timeouts + 1
    assert await flow.expire_now() == 0  # nothing left for the sweep


# --------------------------------------------------------------- timeout


async def test_timeout_keeps_member_muted_and_refuses_verification(
    flow: Flow, api: FakeBotApi, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    timeouts = metric_value("akkiabot_captcha_total", result="timeout")

    assert await flow.expire_now() == 1
    assert await flow.expire_now() == 0  # idempotent

    assert await flow.status(challenge.id) is CaptchaStatus.EXPIRED
    assert await flow.restriction() is Restriction.CAPTCHA_MUTE
    assert flow.restricts() == [(NEWBIE, False)]  # never unmuted
    assert [int(p["message_id"]) for p in api.calls_to("deleteMessage")] == [
        challenge.group_message_id
    ]
    assert metric_value("akkiabot_captcha_total", result="timeout") == timeouts + 1

    response = await client.post(VERIFY, json=body(challenge.id))
    assert response.status_code == 410
    assert response.json()["error"] == "challenge_expired"
    assert flow.restricts() == [(NEWBIE, False)]


async def test_overdue_challenge_is_refused_before_the_sweep(
    flow: Flow, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    challenge = await flow.pending()
    async with session_scope(flow.runtime.session_factory) as session:
        await session.execute(
            update(CaptchaChallenge)
            .where(CaptchaChallenge.id == challenge.id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
    response = await client.post(VERIFY, json=body(challenge.id))
    assert response.status_code == 410
    assert flow.restricts() == [(NEWBIE, False)]


async def test_timeout_job_expires_overdue_challenges(flow: Flow, api: FakeBotApi) -> None:
    await flow.join()
    challenge = await flow.pending()
    async with session_scope(flow.runtime.session_factory) as session:
        await session.execute(
            update(CaptchaChallenge)
            .where(CaptchaChallenge.id == challenge.id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
    job_queue = flow.runtime.application.job_queue
    assert job_queue is not None
    (job,) = job_queue.get_jobs_by_name(f"{TIMEOUT_JOB}:{challenge.id}")
    await job.run(flow.runtime.application)
    assert await flow.status(challenge.id) is CaptchaStatus.EXPIRED
    assert api.calls_to("deleteMessage")


async def test_rejoin_after_timeout_gets_a_new_challenge(
    flow: Flow, api: FakeBotApi, client: httpx.AsyncClient, turnstile: respx.Route
) -> None:
    await flow.join()
    first = await flow.pending()
    await flow.expire_now()
    await flow.leave()
    assert await flow.restriction() is Restriction.CAPTCHA_MUTE  # leaving lifts nothing

    await flow.rejoin_muted()
    second = await flow.pending()
    assert second.id != first.id
    assert (await client.post(VERIFY, json=body(first.id))).status_code == 410
    response = await client.post(VERIFY, json=body(second.id))
    assert response.status_code == 200
    assert await flow.restriction() is Restriction.NONE


async def test_rejoin_while_pending_replaces_the_challenge(flow: Flow, api: FakeBotApi) -> None:
    await flow.join()
    first = await flow.pending()
    await flow.leave()
    await flow.rejoin_muted()
    second = await flow.pending()
    assert second.id != first.id
    assert await flow.status(first.id) is CaptchaStatus.EXPIRED
    assert first.group_message_id in [int(p["message_id"]) for p in api.calls_to("deleteMessage")]


# ------------------------------------------------------- manual unmute


async def test_admin_lifting_the_restriction_syncs_chat_member(
    flow: Flow, updates: UpdateFactory
) -> None:
    await flow.join()
    await flow.expire_now()
    user = updates.user(NEWBIE, "Newbie")
    muted = UpdateFactory.chat_member(user, "restricted")
    admin = updates.user(ADMIN, "Admin")
    await flow.feed(
        updates.chat_member_update(user, old=muted, new="member", chat=flow.chat, by=admin)
    )
    assert await flow.restriction() is Restriction.NONE


async def test_lifting_a_mute_does_not_touch_bans(flow: Flow, updates: UpdateFactory) -> None:
    await flow.set_restriction(Restriction.ADMIN_BAN)
    user = updates.user(NEWBIE, "Newbie")
    muted = UpdateFactory.chat_member(user, "restricted")
    await flow.feed(updates.chat_member_update(user, old=muted, new="member", chat=flow.chat))
    assert await flow.restriction() is Restriction.ADMIN_BAN


async def test_unban_syncs_chat_member_and_rejoin_is_challenged(
    flow: Flow, updates: UpdateFactory
) -> None:
    await flow.set_restriction(Restriction.ADMIN_BAN)
    user = updates.user(NEWBIE, "Newbie")
    await flow.feed(updates.chat_member_update(user, old="kicked", new="left", chat=flow.chat))
    assert await flow.restriction() is Restriction.NONE
    await flow.join()
    assert await flow.challenges()


# ------------------------------------------------------------------ CORS


async def test_cors_allows_only_configured_origin(client: httpx.AsyncClient) -> None:
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }
    allowed = await client.options(VERIFY, headers={"Origin": WEBAPP, **preflight})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == WEBAPP

    denied = await client.options(VERIFY, headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in denied.headers
