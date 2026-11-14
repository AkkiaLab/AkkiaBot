"""``POST /api/v1/captcha/verify``: the Mini App submits Turnstile + initData.

Checks run in this order (docs/migration-plan.md M6): Turnstile → initData
(signature and ``auth_date``) → challenge exists, pending, not expired → the
initData user is the challenged user → the member is still under a *captcha*
mute → unmute → ``answerWebAppQuery`` receipt. Every refusal is a
:class:`CaptchaError` with a stable ``error`` code.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from akkiabot_core.observability import log_context, new_trace_id
from akkiabot_server.api.deps import RuntimeDep
from akkiabot_server.bot.captcha import PtbCaptchaApi
from akkiabot_server.services.captcha import CaptchaRejectedError, RejectReason

router = APIRouter(prefix="/api/v1/captcha", tags=["captcha"])


class CaptchaVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: uuid.UUID = Field(description="来自 Mini App URL 的 `challenge` 参数")
    init_data: str = Field(
        min_length=1, max_length=8192, description="`Telegram.WebApp.initData` 原始字符串"
    )
    turnstile_token: str = Field(
        min_length=1, max_length=2048, description="Turnstile 组件返回的 token"
    )


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
            )
        except CaptchaRejectedError as exc:
            error = CaptchaError(error=exc.reason, message=_MESSAGES[exc.reason])
            return JSONResponse(error.model_dump(mode="json"), status_code=_STATUS[exc.reason])
    return JSONResponse(CaptchaVerifyResponse(message=PASSED_MESSAGE).model_dump(mode="json"))
