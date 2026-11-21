"""Pending join requests: the applicant is told to wait for an admin."""

from __future__ import annotations

import logging

import pytest

from akkiabot_server.bot.handlers.join_request import PENDING_APPROVAL_TEXT
from akkiabot_server.runtime import Runtime
from akkiabot_server.testing import FakeBotApi, UpdateFactory, metric_value

GROUP_ID = -1001234567890
APPLICANT_ID = 4242

#: Approving or declining is left to the chat's admins.
_DECISIONS = ("approveChatJoinRequest", "declineChatJoinRequest")


def test_pending_text_is_fixed() -> None:
    # Documented in README and checked word for word by the e2e acceptance.
    assert PENDING_APPROVAL_TEXT == "Hello, please wait for the admin to approve your request."


async def test_join_request_sends_private_notice(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, caplog: pytest.LogCaptureFixture
) -> None:
    applicant = updates.user(APPLICANT_ID, "Applicant")
    with caplog.at_level(logging.INFO):
        await runtime.application.process_update(
            updates.chat_join_request(applicant, chat=updates.group(GROUP_ID))
        )

    (sent,) = api.calls_to("sendMessage")
    assert sent["chat_id"] == APPLICANT_ID  # the applicant's private chat, not the group
    assert sent["text"] == PENDING_APPROVAL_TEXT
    assert "parse_mode" not in sent
    for method in _DECISIONS:
        assert api.calls_to(method) == []
    (record,) = [r for r in caplog.records if r.message == "join request notice sent"]
    assert record.__dict__["chat_type"] == "supergroup"


async def test_join_request_for_a_channel_is_answered_too(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    # The chat type is not checked: channels with join requests get the notice too.
    channel = updates.group(-1009876543210, "Channel", type="channel")
    await runtime.application.process_update(
        updates.chat_join_request(updates.user(APPLICANT_ID), chat=channel)
    )
    assert api.sent_texts() == [PENDING_APPROVAL_TEXT]


async def test_send_failure_is_logged_and_counted(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, caplog: pytest.LogCaptureFixture
) -> None:
    api.fail("sendMessage", "Forbidden: bot was blocked by the user", status=403)
    errors = metric_value("akkiabot_telegram_api_errors_total", method="sendMessage")
    handler_errors = metric_value("akkiabot_handler_errors_total", handler="chat_join_request")

    with caplog.at_level(logging.INFO):
        await runtime.application.process_update(
            updates.chat_join_request(updates.user(APPLICANT_ID), chat=updates.group(GROUP_ID))
        )

    assert len(api.calls_to("sendMessage")) == 1  # no retry
    for method in _DECISIONS:
        assert api.calls_to(method) == []
    assert metric_value("akkiabot_telegram_api_errors_total", method="sendMessage") == errors + 1
    # Handled, not an unexpected handler error.
    assert (
        metric_value("akkiabot_handler_errors_total", handler="chat_join_request") == handler_errors
    )
    (record,) = [r for r in caplog.records if r.message == "join request notice failed"]
    assert record.levelno == logging.WARNING
    assert "blocked" in record.__dict__["error"]
    assert not any(r.message == "join request notice sent" for r in caplog.records)
