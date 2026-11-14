"""HTTP side of the Mini App acceptance (M7): fetch the page, call the API.

Only public URLs of the environment under test are requested. The initData
sent to the API is the signed payload Telegram issued to the test account for
this run; it is never logged (the report records its length only).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from akkiabot_e2e.scenarios import HttpResult


class HttpWebProbe:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=20, follow_redirects=True)

    @staticmethod
    def _result(response: httpx.Response) -> HttpResult:
        headers: Mapping[str, str] = {k.lower(): v for k, v in response.headers.items()}
        return HttpResult(response.status_code, response.text, dict(headers))

    async def get(self, url: str) -> HttpResult:
        return self._result(await self._client.get(url))

    async def post_json(self, url: str, body: Mapping[str, Any], *, origin: str) -> HttpResult:
        response = await self._client.post(url, json=dict(body), headers={"Origin": origin})
        return self._result(response)

    async def aclose(self) -> None:
        await self._client.aclose()
