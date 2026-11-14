"""Read-only Langfuse check for M5: was every spam classification traced?

Uses the Langfuse public API (``GET /api/public/v2/observations``; the v1
list endpoint answers 410 Gone on current Langfuse Cloud, HTTP basic
auth with the project's public / secret key). Only observation inputs are
inspected, to find the run-specific marker the scenarios put into every test
message; keys never appear in URLs, logs or the report.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx

from akkiabot_e2e.config import LangfuseCredentials
from akkiabot_e2e.errors import CheckFailed

GENERATION_NAME = "antispam.classify"
"""Observation name used by ``akkiabot_antispam`` (kept in sync by hand: black box)."""


class LangfuseProbe:
    def __init__(
        self,
        credentials: LangfuseCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=credentials.host,
            auth=(credentials.public_key.reveal(), credentials.secret_key.reveal()),
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generations(self, *, since: datetime, name: str = GENERATION_NAME) -> list[Any]:
        """All generation observations named ``name`` that started after ``since``."""
        found: list[Any] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "name": name,
                "type": "GENERATION",
                "fromStartTime": since.isoformat(),
                "fields": "core,io",
                "limit": 100,
            }
            if cursor:
                params["cursor"] = cursor
            response = await self._client.get("/api/public/v2/observations", params=params)
            if response.status_code != 200:
                raise CheckFailed(f"Langfuse 查询失败：HTTP {response.status_code}")
            body = response.json()
            found += body.get("data", [])
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor:
                return found

    async def wait_for_markers(
        self,
        markers: Sequence[str],
        *,
        since: datetime,
        max_wait: float = 120.0,
        poll_interval: float = 5.0,
    ) -> dict[str, int]:
        """Poll until every marker appears in some generation input (ingestion is
        asynchronous); returns how many generations mention each marker."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        while True:
            observations = await self.generations(since=since)
            counts = {
                marker: sum(
                    marker in json.dumps(o.get("input"), ensure_ascii=False) for o in observations
                )
                for marker in markers
            }
            if all(counts.values()) or loop.time() >= deadline:
                return counts
            await asyncio.sleep(poll_interval)
