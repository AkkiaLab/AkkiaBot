"""Online IP data providers.

Each provider answers with an :class:`IpInfo` or raises :class:`ProviderError`;
it never returns partial garbage. Providers share the caller's
``httpx.AsyncClient`` (connection pool, timeouts) and never log secrets: the
ipinfo token travels in a header, not in the URL.
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from akkiabot_whois.errors import ProviderError
from akkiabot_whois.models import IpAddress, IpInfo, join_location


@runtime_checkable
class IpProvider(Protocol):
    name: str
    """Stable, low-cardinality name; used as the ``provider`` metric label."""

    async def lookup(self, ip: IpAddress) -> IpInfo:
        """Look ``ip`` up; raises :class:`ProviderError` on any failure."""
        ...


async def _get_json(
    provider: str,
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    try:
        response = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderError(provider, f"请求失败：{type(exc).__name__}") from exc
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        raise ProviderError(provider, "触发限流（HTTP 429）")
    if response.status_code != httpx.codes.OK:
        raise ProviderError(provider, f"HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(provider, "响应不是合法 JSON") from exc


def _split_as(value: str) -> tuple[str, str]:
    """``"AS13335 Cloudflare, Inc."`` → ``("AS13335", "Cloudflare, Inc.")``."""
    number, _, name = value.strip().partition(" ")
    if not number.upper().startswith("AS"):
        return "", value.strip()
    return number, name.strip()


# ---------------------------------------------------------------- ipinfo.io
class _IpinfoAsn(BaseModel):
    asn: str = ""
    name: str = ""


class _IpinfoCompany(BaseModel):
    name: str = ""


class _IpinfoResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ip: str
    bogon: bool = False
    city: str = ""
    region: str = ""
    country: str = ""
    org: str = ""
    """Free plan: ``"AS13335 Cloudflare, Inc."``."""
    asn: _IpinfoAsn | None = None
    """Paid plans only."""
    company: _IpinfoCompany | None = None
    """Paid plans only."""


class IpinfoProvider:
    """https://ipinfo.io (needs a token).

    The free plan only reports ``org`` (``AS<number> <name>``), so the AS name
    doubles as Org and ISP; paid plans fill them from ``asn`` / ``company``.
    """

    name: str = "ipinfo"
    base_url: ClassVar[str] = "https://ipinfo.io"

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        if not token:
            raise ValueError("ipinfo 需要 token")
        self._client = client
        self._token = token

    async def lookup(self, ip: IpAddress) -> IpInfo:
        payload = await _get_json(
            self.name,
            self._client,
            f"{self.base_url}/{ip}",
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        try:
            data = _IpinfoResponse.model_validate(payload)
        except ValidationError as exc:
            raise ProviderError(self.name, "响应格式不符合预期") from exc
        if data.bogon:
            raise ProviderError(self.name, "保留地址（bogon）")

        as_number, as_name = _split_as(data.org)
        if data.asn is not None and data.asn.asn:
            as_number, as_name = data.asn.asn, data.asn.name or as_name
        org = data.company.name if data.company and data.company.name else as_name
        return IpInfo(
            ip=data.ip,
            location=join_location(data.city, data.region, data.country),
            asn=" ".join(p for p in (as_number, as_name) if p),
            org=org,
            isp=as_name,
            provider=self.name,
        )


# ---------------------------------------------------------------- ip-api.com
class _IpApiResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str
    message: str = ""
    query: str = ""
    country: str = ""
    region_name: str = Field(default="", alias="regionName")
    city: str = ""
    isp: str = ""
    org: str = ""
    as_: str = Field(default="", alias="as")


class IpApiProvider:
    """http://ip-api.com (free endpoint: HTTP only, 45 requests/minute, no key)."""

    name: str = "ip-api"
    base_url: ClassVar[str] = "http://ip-api.com/json"
    fields: ClassVar[str] = "status,message,query,country,regionName,city,isp,org,as"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def lookup(self, ip: IpAddress) -> IpInfo:
        payload = await _get_json(
            self.name,
            self._client,
            f"{self.base_url}/{ip}",
            params={"fields": self.fields, "lang": "en"},
        )
        try:
            data = _IpApiResponse.model_validate(payload)
        except ValidationError as exc:
            raise ProviderError(self.name, "响应格式不符合预期") from exc
        if data.status != "success":
            raise ProviderError(self.name, data.message or data.status)
        return IpInfo(
            ip=data.query or str(ip),
            location=join_location(data.city, data.region_name, data.country),
            asn=data.as_.strip(),
            org=data.org,
            isp=data.isp,
            provider=self.name,
        )
