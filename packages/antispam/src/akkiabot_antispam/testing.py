"""Test helpers: an in-memory OpenAI-compatible endpoint (used by test suites only).

The openai SDK talks through ``httpx2``, so ``respx`` (which patches ``httpx``)
cannot intercept it; :class:`FakeLlm` plugs an ``httpx2.MockTransport`` into
the client instead.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx2
from openai import AsyncOpenAI

from akkiabot_antispam.classifier import build_client

BASE_URL = "https://llm.example.test/v1"
# mock: 人工构造的不可用测试值，不是凭据。
MOCK_API_KEY = "fake-api-key-for-tests"

type Reply = httpx2.Response | Exception | Callable[[dict[str, Any]], httpx2.Response]


def completion(content: str, *, usage: bool = True) -> httpx2.Response:
    """A ``chat.completion`` response whose answer is ``content``."""
    body: dict[str, Any] = {
        "id": "cmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128}
    return httpx2.Response(200, json=body)


def verdict_json(is_spam: bool, category: str = "ad", confidence: float = 0.9) -> httpx2.Response:
    return completion(
        json.dumps(
            {
                "is_spam": is_spam,
                "category": category if is_spam else "none",
                "confidence": confidence,
            }
        )
    )


def error(status: int, message: str = "test error") -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"message": message, "type": "invalid_request_error"}}
    )


class FakeLlm:
    """Answers queued replies in order, then ``default`` (``0`` = not spam).

    ``requests`` holds the decoded JSON bodies that were sent.
    """

    def __init__(self, default: Reply | None = None) -> None:
        self.replies: deque[Reply] = deque()
        self.default: Reply = default if default is not None else completion("0")
        self.requests: list[dict[str, Any]] = []

    def queue(self, *replies: Reply) -> None:
        self.replies.extend(replies)

    def client(self) -> AsyncOpenAI:
        transport = httpx2.MockTransport(self._handle)
        return build_client(
            api_key=MOCK_API_KEY,
            base_url=BASE_URL,
            http_client=httpx2.AsyncClient(transport=transport),
        )

    def last(self) -> dict[str, Any]:
        return self.requests[-1]

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        reply = self.replies.popleft() if self.replies else self.default
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx2.Response):
            return reply
        return reply(body)
