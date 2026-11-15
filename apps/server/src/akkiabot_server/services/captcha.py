"""Join captcha: mute new members until they pass a Turnstile check in the Mini App.

Flow (docs/migration-plan.md M6)::

    member joins a group with captcha enabled
      → mute (``chat_member.restriction = captcha_mute``)
      → ``captcha_challenge`` (pending, expires after ``webapp.captcha_timeout``)
      → group prompt with a ``t.me/<bot>?start=<token>`` deep link
    /start <token> in a private chat (only the challenged user)
      → button opening ``<webapp.url>/captcha?challenge=<id>``
    POST /api/v1/captcha/verify
      → Turnstile → initData → challenge pending and not expired → same user
      → member still under *captcha* mute, both in ``chat_member`` and live in
        Telegram (``getChatMember``) → unmute → answerWebAppQuery receipt
    admin changes the member's restrictions in Telegram
      → ``chat_member`` becomes ``admin_mute`` (or ``none`` when lifted), so the
        captcha can no longer lift them; all restriction changes of one member
        are serialised by a per-member lock.
    timeout job
      → challenge ``expired`` (reported as ``timeout``), member **stays muted**,
        group prompt deleted; rejoining creates a new challenge.

The service is framework independent: Telegram is reached through
:class:`CaptchaApi` (implemented by the bot layer), Turnstile through
:class:`~akkiabot_server.services.turnstile.TurnstileVerifier`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from akkiabot_core.db import (
    CaptchaChallenge,
    CaptchaChallengeRepository,
    CaptchaStatus,
    ChatMemberRepository,
    Restriction,
    session_scope,
)
from akkiabot_core.db.types import utcnow
from akkiabot_core.observability import (
    count_captcha,
    count_captcha_started,
    count_moderation_action,
    count_telegram_api_error,
    log_context,
    observe_captcha_duration,
)
from akkiabot_core.telegram import InitDataError, WebAppInitData, parse_init_data, public_name
from akkiabot_server.services.chat_settings import ChatSettingsService
from akkiabot_server.services.moderation import ModerationApiError, RateLimitedError
from akkiabot_server.services.start_payload import StartPayloadStore
from akkiabot_server.services.turnstile import TurnstileVerifier

logger = logging.getLogger(__name__)

START_KIND = "captcha"

#: Restrictions lifting a mute in Telegram clears in ``chat_member``.
MUTE_RESTRICTIONS = frozenset(
    {Restriction.CAPTCHA_MUTE, Restriction.SPAM_MUTE, Restriction.ADMIN_MUTE}
)
#: Restrictions an unban by an admin clears in ``chat_member``.
BAN_RESTRICTIONS = frozenset({Restriction.WARN_BAN, Restriction.ADMIN_BAN})
#: Restrictions that do not prevent a new captcha on (re)join.
_CAPTCHA_ELIGIBLE = frozenset({Restriction.NONE, Restriction.CAPTCHA_MUTE})


class CaptchaApi(Protocol):
    """Telegram operations the captcha needs (implemented by the bot layer).

    Implementations raise :class:`RateLimitedError` or :class:`ModerationApiError`.
    """

    async def mute(self, chat_id: int, user_id: int) -> None: ...

    async def unmute(self, chat_id: int, user_id: int) -> None: ...

    async def is_captcha_muted(self, chat_id: int, user_id: int) -> bool:
        """Whether Telegram *currently* shows exactly the mute :meth:`mute` applies
        (still a member, every permission off, no end date)."""
        ...

    async def send_prompt(
        self, chat_id: int, user_id: int, display_name: str, start_token: str, ttl: int
    ) -> int:
        """Post the group prompt with the deep-link button; return its message id."""
        ...

    async def delete_message(self, chat_id: int, message_id: int) -> None: ...

    async def answer_web_app_query(self, query_id: str, text: str) -> None: ...


class JoinOutcome(StrEnum):
    DISABLED = "disabled"
    """The group did not enable captcha."""
    SKIPPED = "skipped"
    """The member carries a non-captcha restriction (spam mute, ban…); left alone."""
    MUTE_FAILED = "mute_failed"
    """Telegram refused the mute (bot lacks rights…); no challenge created."""
    CHALLENGED = "challenged"


@dataclass(frozen=True, slots=True)
class JoinResult:
    outcome: JoinOutcome
    challenge_id: uuid.UUID | None = None
    expires_at: datetime | None = None


class StartCheck(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    WRONG_USER = "wrong_user"
    EXPIRED = "expired"
    USED = "used"


class RejectReason(StrEnum):
    """Why ``/api/v1/captcha/verify`` refused a request (API error codes)."""

    UNAVAILABLE = "captcha_unavailable"
    TURNSTILE_FAILED = "turnstile_failed"
    INIT_DATA_INVALID = "init_data_invalid"
    INIT_DATA_EXPIRED = "init_data_expired"
    CHALLENGE_NOT_FOUND = "challenge_not_found"
    CHALLENGE_EXPIRED = "challenge_expired"
    CHALLENGE_USED = "challenge_used"
    WRONG_USER = "wrong_user"
    NOT_CAPTCHA_RESTRICTED = "not_captcha_restricted"
    UNMUTE_FAILED = "unmute_failed"


class CaptchaRejectedError(Exception):
    def __init__(self, reason: RejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.chat_id: int | None = None
        self.user_id: int | None = None
        """Group and member of the challenge, once it was found (for the log)."""


@dataclass(frozen=True, slots=True)
class VerifyResult:
    chat_id: int
    user_id: int
    duration: float
    """Seconds between the challenge being created and passed."""


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """Plain copy of a challenge row (safe to use after the session closed)."""

    id: uuid.UUID
    chat_id: int
    user_id: int
    status: CaptchaStatus
    created_at: datetime
    expires_at: datetime
    group_message_id: int | None

    @classmethod
    def of(cls, c: CaptchaChallenge) -> _Snapshot:
        return cls(
            c.id, c.chat_id, c.user_id, c.status, c.created_at, c.expires_at, c.group_message_id
        )


PASSED_RECEIPT = "验证成功✅ 你已被解除禁言，欢迎加入群聊🎉"


def parse_challenge_id(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class CaptchaService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        chat_settings: ChatSettingsService,
        start_payloads: StartPayloadStore,
        *,
        bot_token: str,
        timeout: int,
        init_data_max_age: int,
        turnstile: TurnstileVerifier | None,
    ) -> None:
        self._session_factory = session_factory
        self._chat_settings = chat_settings
        self._start_payloads = start_payloads
        self._bot_token = bot_token
        self.timeout = timeout
        self._init_data_max_age = timedelta(seconds=init_data_max_age)
        self._turnstile = turnstile
        self._locks: dict[tuple[int, int], tuple[asyncio.Lock, list[int]]] = {}

    @asynccontextmanager
    async def _member_lock(self, chat_id: int, user_id: int) -> AsyncIterator[None]:
        """Serialise every restriction change of one member (join, verify, admin
        sync) inside this process. The bot runs as a single instance."""
        key = (chat_id, user_id)
        lock, users = self._locks.setdefault(key, (asyncio.Lock(), [0]))
        users[0] += 1
        try:
            async with lock:
                yield
        finally:
            users[0] -= 1
            if users[0] == 0:
                del self._locks[key]

    # ------------------------------------------------------------- join
    async def handle_join(
        self, api: CaptchaApi, chat_id: int, user_id: int, first_name: str
    ) -> JoinResult:
        """A user became a member of ``chat_id``: mute and challenge them if enabled."""
        settings = await self._chat_settings.get(chat_id)
        if not settings.captcha:
            return JoinResult(JoinOutcome.DISABLED)
        async with self._member_lock(chat_id, user_id):
            return await self._handle_join(api, chat_id, user_id, first_name)

    async def _handle_join(
        self, api: CaptchaApi, chat_id: int, user_id: int, first_name: str
    ) -> JoinResult:
        async with session_scope(self._session_factory) as session:
            member = await ChatMemberRepository(session).get(chat_id, user_id)
            restriction = member.restriction if member else Restriction.NONE
        if restriction not in _CAPTCHA_ELIGIBLE:
            logger.info(
                "captcha skipped: member already restricted",
                extra={"target_user_id": user_id, "restriction": restriction.value},
            )
            return JoinResult(JoinOutcome.SKIPPED)

        try:
            await api.mute(chat_id, user_id)
        except (ModerationApiError, RateLimitedError) as exc:
            count_telegram_api_error("restrictChatMember")
            logger.warning(
                "captcha mute failed", extra={"target_user_id": user_id, "error": str(exc)}
            )
            return JoinResult(JoinOutcome.MUTE_FAILED)
        count_moderation_action("mute")

        async with session_scope(self._session_factory) as session:
            await ChatMemberRepository(session).set_restriction(
                chat_id, user_id, Restriction.CAPTCHA_MUTE
            )
            challenges = CaptchaChallengeRepository(session)
            # A rejoin replaces whatever challenge was still pending.
            superseded = [
                _Snapshot.of(c)
                for c in await challenges.list_pending(chat_id, user_id)
                if await challenges.resolve(c.id, CaptchaStatus.EXPIRED)
            ]
            challenge = _Snapshot.of(
                await challenges.create(chat_id, user_id, ttl=timedelta(seconds=self.timeout))
            )
        count_captcha_started()
        for old in superseded:
            await self._delete_prompt(api, old)

        token = await self._start_payloads.create(
            START_KIND, {"challenge_id": str(challenge.id)}, ttl=self.timeout
        )
        try:
            message_id = await api.send_prompt(
                chat_id, user_id, public_name(first_name, "新成员"), token, self.timeout
            )
        except (ModerationApiError, RateLimitedError) as exc:
            # The member stays muted; the timeout job closes the challenge.
            count_telegram_api_error("sendMessage")
            logger.warning(
                "captcha prompt could not be sent",
                extra={"target_user_id": user_id, "error": str(exc)},
            )
        else:
            async with session_scope(self._session_factory) as session:
                await CaptchaChallengeRepository(session).set_group_message(
                    challenge.id, message_id
                )
        logger.info(
            "captcha challenge created",
            extra={
                "target_user_id": user_id,
                "challenge_id": str(challenge.id),
                "superseded": len(superseded),
            },
        )
        return JoinResult(JoinOutcome.CHALLENGED, challenge.id, challenge.expires_at)

    # ------------------------------------------------------------ /start
    async def check_start(self, challenge_id: uuid.UUID | None, user_id: int) -> StartCheck:
        """May ``user_id`` open the Mini App for this challenge right now?"""
        if challenge_id is None:
            return StartCheck.NOT_FOUND
        challenge = await self._load(challenge_id)
        if challenge is None:
            return StartCheck.NOT_FOUND
        if challenge.user_id != user_id:
            logger.warning(
                "captcha link used by another user", extra={"challenge_id": str(challenge_id)}
            )
            return StartCheck.WRONG_USER
        if challenge.status is CaptchaStatus.EXPIRED or challenge.expires_at <= utcnow():
            return StartCheck.EXPIRED
        if challenge.status is not CaptchaStatus.PENDING:
            return StartCheck.USED
        return StartCheck.OK

    # ------------------------------------------------------------ verify
    async def verify(
        self,
        api: CaptchaApi,
        *,
        challenge_id: uuid.UUID,
        init_data: str,
        turnstile_token: str,
    ) -> VerifyResult:
        """Check a Mini App submission and lift the captcha mute.

        Raises :class:`CaptchaRejectedError`; every rejection counts as
        ``captcha_total{result="failed"}``.
        """
        try:
            return await self._verify(
                api,
                challenge_id=challenge_id,
                init_data=init_data,
                turnstile_token=turnstile_token,
            )
        except CaptchaRejectedError as exc:
            count_captcha("failed")
            if exc.chat_id is None:
                # Rejected before the challenge was loaded (Turnstile, initData):
                # look it up only to tag the log line with its group.
                with contextlib.suppress(Exception):
                    if (challenge := await self._load(challenge_id)) is not None:
                        exc.chat_id, exc.user_id = challenge.chat_id, challenge.user_id
            with log_context(chat_id=exc.chat_id, user_id=exc.user_id):
                logger.info(
                    "captcha verification rejected",
                    extra={"challenge_id": str(challenge_id), "reason": exc.reason.value},
                )
            raise

    async def _verify(
        self,
        api: CaptchaApi,
        *,
        challenge_id: uuid.UUID,
        init_data: str,
        turnstile_token: str,
    ) -> VerifyResult:
        if self._turnstile is None:
            raise CaptchaRejectedError(RejectReason.UNAVAILABLE)
        # 1. Turnstile
        turnstile = await self._turnstile.verify(turnstile_token)
        if not turnstile.success:
            logger.info(
                "turnstile rejected", extra={"turnstile_errors": list(turnstile.error_codes)}
            )
            raise CaptchaRejectedError(RejectReason.TURNSTILE_FAILED)
        # 2. initData (signature + auth_date)
        try:
            data = parse_init_data(init_data, self._bot_token, max_age=self._init_data_max_age)
        except InitDataError as exc:
            logger.info("initData rejected", extra={"reason": exc.reason})
            raise CaptchaRejectedError(
                RejectReason.INIT_DATA_EXPIRED
                if exc.reason == "expired"
                else RejectReason.INIT_DATA_INVALID
            ) from None
        if data.user is None:
            raise CaptchaRejectedError(RejectReason.INIT_DATA_INVALID)
        # 3. challenge exists, pending, not expired
        challenge = await self._load(challenge_id)
        if challenge is None:
            raise CaptchaRejectedError(RejectReason.CHALLENGE_NOT_FOUND)
        # The request comes from the Mini App, not a group update: bind the
        # group and the member so the flow can be followed by chat_id in Loki.
        with log_context(chat_id=challenge.chat_id, user_id=challenge.user_id):
            try:
                return await self._verify_challenge(api, challenge, data)
            except CaptchaRejectedError as exc:
                exc.chat_id, exc.user_id = challenge.chat_id, challenge.user_id
                raise

    async def _verify_challenge(
        self, api: CaptchaApi, challenge: _Snapshot, data: WebAppInitData
    ) -> VerifyResult:
        assert data.user is not None  # noqa: S101 - checked by _verify
        self._ensure_open(challenge)
        # 4. same user
        if challenge.user_id != data.user.id:
            raise CaptchaRejectedError(RejectReason.WRONG_USER)
        async with self._member_lock(challenge.chat_id, challenge.user_id):
            await self._lift_captcha_mute(api, challenge)

        duration = (utcnow() - challenge.created_at).total_seconds()
        count_captcha("passed")
        observe_captcha_duration(duration)
        logger.info(
            "captcha passed",
            extra={
                "target_user_id": challenge.user_id,
                "challenge_id": str(challenge.id),
                "duration_seconds": round(duration, 3),
            },
        )
        await self._delete_prompt(api, challenge)
        if data.query_id:
            try:
                await api.answer_web_app_query(data.query_id, PASSED_RECEIPT)
            except (ModerationApiError, RateLimitedError) as exc:
                count_telegram_api_error("answerWebAppQuery")
                logger.warning("captcha receipt failed", extra={"error": str(exc)})
        return VerifyResult(challenge.chat_id, challenge.user_id, duration)

    async def _lift_captcha_mute(self, api: CaptchaApi, challenge: _Snapshot) -> None:
        """Steps 5-6 of :meth:`verify`, run under the member lock so that no
        restriction change of this member is processed in between."""
        chat_id, user_id = challenge.chat_id, challenge.user_id
        # 5. only a captcha mute can be lifted here: our record says so…
        async with session_scope(self._session_factory) as session:
            member = await ChatMemberRepository(session).get(chat_id, user_id)
            restriction = member.restriction if member else Restriction.NONE
        if restriction is not Restriction.CAPTCHA_MUTE:
            raise CaptchaRejectedError(RejectReason.NOT_CAPTCHA_RESTRICTED)
        # …and so does Telegram right now (an admin may have changed the
        # restriction and its chat_member update has not been processed yet).
        # Residual race: an admin applying the very same permanent full mute
        # between this lookup and restrictChatMember (milliseconds) cannot be
        # told apart; the Bot API offers no conditional restrict.
        try:
            live = await api.is_captcha_muted(chat_id, user_id)
        except (ModerationApiError, RateLimitedError) as exc:
            count_telegram_api_error("getChatMember")
            logger.warning(
                "captcha member lookup failed", extra={"target_user_id": user_id, "error": str(exc)}
            )
            raise CaptchaRejectedError(RejectReason.UNMUTE_FAILED) from None
        if not live:
            logger.warning(
                "captcha refused: telegram restriction differs from the captcha mute",
                extra={"target_user_id": user_id, "challenge_id": str(challenge.id)},
            )
            raise CaptchaRejectedError(RejectReason.NOT_CAPTCHA_RESTRICTED)

        # Claim the challenge: concurrent or replayed requests lose here.
        async with session_scope(self._session_factory) as session:
            claimed = await CaptchaChallengeRepository(session).resolve(
                challenge.id, CaptchaStatus.PASSED
            )
        if not claimed:
            current = await self._load(challenge.id)
            if current is not None:
                self._ensure_open(current)
            raise CaptchaRejectedError(RejectReason.CHALLENGE_USED)

        # 6. unmute
        try:
            await api.unmute(chat_id, user_id)
        except (ModerationApiError, RateLimitedError) as exc:
            count_telegram_api_error("restrictChatMember")
            logger.warning(
                "captcha unmute failed", extra={"target_user_id": user_id, "error": str(exc)}
            )
            async with session_scope(self._session_factory) as session:
                released = await CaptchaChallengeRepository(session).release_claim(challenge.id)
            if released is CaptchaStatus.EXPIRED:
                # Claimed in time, but the deadline passed while Telegram failed:
                # finish it as a timeout (member stays muted, prompt removed).
                await self._timed_out(api, challenge)
                raise CaptchaRejectedError(RejectReason.CHALLENGE_EXPIRED) from None
            raise CaptchaRejectedError(RejectReason.UNMUTE_FAILED) from None

        async with session_scope(self._session_factory) as session:
            await ChatMemberRepository(session).set_restriction(chat_id, user_id, Restriction.NONE)

    @staticmethod
    def _ensure_open(challenge: _Snapshot) -> None:
        if challenge.status is CaptchaStatus.EXPIRED or challenge.expires_at <= utcnow():
            raise CaptchaRejectedError(RejectReason.CHALLENGE_EXPIRED)
        if challenge.status is not CaptchaStatus.PENDING:
            raise CaptchaRejectedError(RejectReason.CHALLENGE_USED)

    # ----------------------------------------------------------- timeout
    async def expire_due(self, api: CaptchaApi, *, now: datetime | None = None) -> int:
        """Time out overdue challenges: the member stays muted, the prompt goes away.

        Idempotent (compare-and-set per challenge); returns how many timed out.
        """
        async with session_scope(self._session_factory) as session:
            expired: Sequence[_Snapshot] = [
                _Snapshot.of(c) for c in await CaptchaChallengeRepository(session).expire_due(now)
            ]
        for challenge in expired:
            await self._timed_out(api, challenge)
        return len(expired)

    async def _timed_out(self, api: CaptchaApi, challenge: _Snapshot) -> None:
        count_captcha("timeout")
        # Runs in a job, not for an update: bind the group and the member.
        with log_context(chat_id=challenge.chat_id, user_id=challenge.user_id):
            logger.info(
                "captcha timed out; member stays muted",
                extra={"target_user_id": challenge.user_id, "challenge_id": str(challenge.id)},
            )
            await self._delete_prompt(api, challenge)

    # ------------------------------------------------- membership sync
    async def handle_unrestricted(
        self, chat_id: int, user_id: int, *, cleared: frozenset[Restriction]
    ) -> bool:
        """An admin lifted a mute (``cleared=MUTE_RESTRICTIONS``) or a ban
        (``BAN_RESTRICTIONS``) in Telegram: reset a matching recorded
        restriction to ``none``. Returns whether ``chat_member`` changed."""
        async with (
            self._member_lock(chat_id, user_id),
            session_scope(self._session_factory) as session,
        ):
            members = ChatMemberRepository(session)
            member = await members.get(chat_id, user_id)
            if member is None or member.restriction not in cleared:
                return False
            previous = member.restriction
            await members.set_restriction(chat_id, user_id, Restriction.NONE)
        logger.info(
            "member restriction lifted in telegram",
            extra={"target_user_id": user_id, "previous_restriction": previous.value},
        )
        return True

    async def handle_restricted(self, chat_id: int, user_id: int, *, by: int) -> bool:
        """Someone other than the bot (an admin) set restrictions on the member
        in Telegram. Record them as ``admin_mute`` unless a more specific
        non-captcha restriction is already recorded, so that the captcha can no
        longer lift them. Returns whether ``chat_member`` changed."""
        async with (
            self._member_lock(chat_id, user_id),
            session_scope(self._session_factory) as session,
        ):
            members = ChatMemberRepository(session)
            member = await members.get(chat_id, user_id)
            previous = member.restriction if member else Restriction.NONE
            if previous not in _CAPTCHA_ELIGIBLE:
                return False
            await members.set_restriction(
                chat_id, user_id, Restriction.ADMIN_MUTE, restricted_by=by
            )
        logger.info(
            "member restricted by an admin in telegram",
            extra={"target_user_id": user_id, "previous_restriction": previous.value},
        )
        return True

    # ----------------------------------------------------------- helpers
    async def _load(self, challenge_id: uuid.UUID) -> _Snapshot | None:
        async with session_scope(self._session_factory) as session:
            challenge = await CaptchaChallengeRepository(session).get(challenge_id)
            return _Snapshot.of(challenge) if challenge else None

    async def _delete_prompt(self, api: CaptchaApi, challenge: _Snapshot) -> None:
        if challenge.group_message_id is None:
            return
        try:
            await api.delete_message(challenge.chat_id, challenge.group_message_id)
        except (ModerationApiError, RateLimitedError) as exc:
            count_telegram_api_error("deleteMessage")
            logger.warning("captcha prompt could not be deleted", extra={"error": str(exc)})
