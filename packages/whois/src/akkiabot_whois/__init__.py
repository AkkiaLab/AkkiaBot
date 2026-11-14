"""IP whois lookup with a provider chain (pure library, no core / framework dependency).

Typical wiring::

    client = httpx.AsyncClient(timeout=5)
    whois = WhoisService(
        [IpinfoProvider(client, token), IpApiProvider(client)],
        TTLCache(ttl=3600),
    )
    info = await whois.lookup("1.1.1.1")
"""

from importlib.metadata import version

from akkiabot_whois.cache import TTLCache
from akkiabot_whois.errors import InvalidIpError, ProviderError, WhoisError
from akkiabot_whois.models import IpAddress, IpInfo
from akkiabot_whois.parsing import ensure_public, extract_ip, parse_ip
from akkiabot_whois.providers import IpApiProvider, IpinfoProvider, IpProvider
from akkiabot_whois.service import RESULTS, LookupObserver, WhoisService

__version__ = version("akkiabot-whois")

__all__ = [
    "RESULTS",
    "InvalidIpError",
    "IpAddress",
    "IpApiProvider",
    "IpInfo",
    "IpProvider",
    "IpinfoProvider",
    "LookupObserver",
    "ProviderError",
    "TTLCache",
    "WhoisError",
    "WhoisService",
    "__version__",
    "ensure_public",
    "extract_ip",
    "parse_ip",
]
