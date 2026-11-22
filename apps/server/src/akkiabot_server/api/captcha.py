"""``POST /api/v1/captcha/verify``: the Mini App submits Turnstile + initData.

``POST /api/v1/captcha/events`` receives the page's lifecycle events
(:mod:`akkiabot_server.services.captcha_telemetry`).

Checks run in this order (README, "入群验证"): Turnstile → initData
(signature and ``auth_date``) → challenge exists, pending, not expired → the
initData user is the challenged user → the member is still under a *captcha*
mute → unmute → ``answerWebAppQuery`` receipt. Every refusal is a
:class:`CaptchaError` with a stable ``error`` code.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Any, Literal

from fastapi import APIRouter, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from akkiabot_core.observability import (
    count_captcha_client_event_reject,
    log_context,
    new_trace_id,
)
from akkiabot_server.api.deps import RuntimeDep
from akkiabot_server.bot.captcha import PtbCaptchaApi
from akkiabot_server.services.captcha import CaptchaRejectedError, RejectReason
from akkiabot_server.services.captcha_telemetry import (
    PLATFORM_PATTERN,
    VERSION_PATTERN,
    ClientEvent,
    ClientInfo,
    ClientOutcome,
    EventRejectedError,
    EventRejectReason,
)

router = APIRouter(prefix="/api/v1/captcha", tags=["captcha"])


class _PageRequest(BaseModel):
    """Fields shared by verify and event requests. ``page_id``, the platform,
    the version and ``attempt`` are optional page metadata, only logged."""

    model_config = ConfigDict(extra="forbid")

    challenge_id: uuid.UUID = Field(description="来自 Mini App URL 的 `challenge` 参数")
    init_data: str = Field(
        min_length=1, max_length=8192, description="`Telegram.WebApp.initData` 原始字符串"
    )
    page_id: uuid.UUID | None = Field(
        default=None, description="页面每次加载时随机生成，用于区分同一页面重复提交和重新打开"
    )
    client_platform: str | None = Field(
        default=None, pattern=PLATFORM_PATTERN, description="`Telegram.WebApp.platform`"
    )
    client_version: str | None = Field(
        default=None, pattern=VERSION_PATTERN, description="`Telegram.WebApp.version`"
    )
    attempt: int | None = Field(
        default=None,
        ge=0,
        le=1000,
        description="本页面内的提交次数：验证请求为第几次提交（从 1 开始），事件为事件发生时已提交的次数",
    )

    def client(self) -> ClientInfo:
        return ClientInfo(self.page_id, self.attempt, self.client_platform, self.client_version)


class CaptchaVerifyRequest(_PageRequest):
    turnstile_token: str = Field(
        min_length=1, max_length=2048, description="Turnstile 组件返回的 token"
    )


class CaptchaEventRequest(_PageRequest):
    event: ClientEvent = Field(description="页面生命周期事件")
    outcome: ClientOutcome | None = Field(
        default=None, description="仅 `result` 事件携带：页面收到的验证结果"
    )


class CaptchaEventError(BaseModel):
    error: EventRejectReason
    message: str


class CaptchaVerifyResponse(BaseModel):
    status: Literal["passed"] = "passed"
    message: str


class CaptchaError(BaseModel):
    error: RejectReason
    message: str


_STATUS: dict[RejectReason, int] = {
    RejectReason.UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    RejectReason.TURNSTILE_FAILED: status.HTTP_400_BAD_REQUEST,
    RejectReason.INIT_DATA_INVALID: status.HTTP_401_UNAUTHORIZED,
    RejectReason.INIT_DATA_EXPIRED: status.HTTP_401_UNAUTHORIZED,
    RejectReason.CHALLENGE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    RejectReason.CHALLENGE_EXPIRED: status.HTTP_410_GONE,
    RejectReason.CHALLENGE_USED: status.HTTP_409_CONFLICT,
    RejectReason.WRONG_USER: status.HTTP_403_FORBIDDEN,
    RejectReason.NOT_CAPTCHA_RESTRICTED: status.HTTP_409_CONFLICT,
    RejectReason.UNMUTE_FAILED: status.HTTP_502_BAD_GATEWAY,
}

_MESSAGES: dict[RejectReason, str] = {
    RejectReason.UNAVAILABLE: "验证服务暂不可用，请稍后重试。",
    RejectReason.TURNSTILE_FAILED: "人机验证未通过，请重试。",
    RejectReason.INIT_DATA_INVALID: "无法确认你的 Telegram 身份，请从机器人私聊中重新打开验证页面。",
    RejectReason.INIT_DATA_EXPIRED: "页面已过期，请从机器人私聊中重新打开验证页面。",
    RejectReason.CHALLENGE_NOT_FOUND: "验证不存在，请回到群里重新获取验证链接。",
    RejectReason.CHALLENGE_EXPIRED: "验证已超时。请联系群管理员，或退群后重新加入以获取新的验证。",
    RejectReason.CHALLENGE_USED: "这次验证已经完成，无需重复提交。",
    RejectReason.WRONG_USER: "这个验证不属于你。",
    RejectReason.NOT_CAPTCHA_RESTRICTED: "你当前的限制不是入群验证造成的，请联系群管理员。",
    RejectReason.UNMUTE_FAILED: "解除禁言失败，请稍后重试。",
}

PASSED_MESSAGE = "验证成功，你已被解除禁言。"


_EVENT_STATUS: dict[EventRejectReason, int] = {
    EventRejectReason.INIT_DATA_INVALID: status.HTTP_401_UNAUTHORIZED,
    EventRejectReason.INIT_DATA_EXPIRED: status.HTTP_401_UNAUTHORIZED,
    EventRejectReason.CHALLENGE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    EventRejectReason.RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    EventRejectReason.INVALID_EVENT: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _responses() -> dict[int | str, dict[str, object]]:
    codes: dict[int, list[str]] = {}
    for reason, code in _STATUS.items():
        codes.setdefault(code, []).append(reason.value)
    return {
        code: {"model": CaptchaError, "description": "、".join(reasons)}
        for code, reasons in sorted(codes.items())
    }


@router.post(
    "/verify",
    response_model=CaptchaVerifyResponse,
    responses=_responses(),
    summary="提交入群验证",
    operation_id="verifyCaptcha",
)
async def verify_captcha(body: CaptchaVerifyRequest, runtime: RuntimeDep) -> JSONResponse:
    """Verify a captcha submission and lift the member's captcha mute."""
    with log_context(trace_id=new_trace_id()):
        try:
            await runtime.deps.captcha.verify(
                PtbCaptchaApi(runtime.application.bot),
                challenge_id=body.challenge_id,
                init_data=body.init_data,
                turnstile_token=body.turnstile_token,
                client=body.client(),
            )
        except CaptchaRejectedError as exc:
            error = CaptchaError(error=exc.reason, message=_MESSAGES[exc.reason])
            return JSONResponse(error.model_dump(mode="json"), status_code=_STATUS[exc.reason])
    return JSONResponse(CaptchaVerifyResponse(message=PASSED_MESSAGE).model_dump(mode="json"))


def _event_responses() -> dict[int | str, dict[str, object]]:
    codes: dict[int, list[str]] = {}
    for reason, code in _EVENT_STATUS.items():
        codes.setdefault(code, []).append(reason.value)
    return {
        code: {"model": CaptchaEventError, "description": "、".join(reasons)}
        for code, reasons in sorted(codes.items())
    }


_EVENT_MESSAGES: dict[EventRejectReason, str] = {
    EventRejectReason.INIT_DATA_INVALID: "initData 无效。",
    EventRejectReason.INIT_DATA_EXPIRED: "initData 已过期。",
    EventRejectReason.CHALLENGE_NOT_FOUND: "验证不存在。",
    EventRejectReason.RATE_LIMITED: "上报过于频繁。",
    EventRejectReason.INVALID_EVENT: "请求字段或取值无效。",
}


def _event_error(reason: EventRejectReason) -> JSONResponse:
    error = CaptchaEventError(error=reason, message=_EVENT_MESSAGES[reason])
    return JSONResponse(error.model_dump(mode="json"), status_code=_EVENT_STATUS[reason])


class _EventRoute(APIRoute):
    """Answers request validation failures with :class:`CaptchaEventError`
    (``invalid_event``), as declared in the OpenAPI document, instead of
    FastAPI's ``detail`` list, which would echo the submitted values back."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                count_captcha_client_event_reject(EventRejectReason.INVALID_EVENT.value)
                return _event_error(EventRejectReason.INVALID_EVENT)

        return route_handler


event_router = APIRouter(prefix="/api/v1/captcha", tags=["captcha"], route_class=_EventRoute)


@event_router.post(
    "/events",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_event_responses(),
    summary="上报入群验证页面事件",
    operation_id="reportCaptchaEvent",
)
async def report_captcha_event(body: CaptchaEventRequest, runtime: RuntimeDep) -> Response:
    """Record a lifecycle event of the captcha page (logs and metrics only)."""
    with log_context(trace_id=new_trace_id()):
        try:
            await runtime.deps.captcha.record_event(
                challenge_id=body.challenge_id,
                init_data=body.init_data,
                event=body.event,
                outcome=body.outcome,
                client=body.client(),
            )
        except EventRejectedError as exc:
            return _event_error(exc.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
