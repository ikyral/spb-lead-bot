"""Website analysis service to detect no-site, dead-site, social-only, or active site."""

import asyncio

import httpcore
import httpx

from src.services.safe_http import PublicOnlyTransport

# httpx applies this per network operation (connect / read / write / pool).
CHECK_TIMEOUT_SECONDS: float = 7.0
# Hard cap for the whole check incl. redirects, so a slowly trickling server
# cannot stall the harvest indefinitely.
TOTAL_CHECK_TIMEOUT_SECONDS: float = 20.0

# Error statuses returned by a live server that blocks bots (Cloudflare, DDoS-Guard),
# requires auth, or rate-limits us. The site exists, so it is not a dead_site lead.
ALIVE_ERROR_STATUSES: frozenset[int] = frozenset({401, 403, 429})

DEAD_SITE_RESULT: tuple[str, str | None] = ("dead_site", "HTTP/Connection error")

SOCIAL_DOMAINS: tuple[str, ...] = (
    "vk.com",
    "vk.ru",
    "t.me",
    "taplink.cc",
    "wa.me",
    "instagram.com",
    "yclients.com",
    "dikidi.net",
    "linktr.ee",
)

# Maps, catalogs, reviews, classifieds and registries: they list an organization, but are not its site.
# Only the search on Google uses this list; a site from a source is judged by SOCIAL_DOMAINS alone.
CATALOG_DOMAINS: tuple[str, ...] = (
    "2gis.ru",
    "2gis.com",
    "yandex.ru",
    "yandex.com",
    "ya.ru",
    "google.com",
    "google.ru",
    "zoon.ru",
    "flamp.ru",
    "otzovik.com",
    "irecommend.ru",
    "yell.ru",
    "orgpage.ru",
    "spravker.ru",
    "spr.ru",
    "cataloxy.ru",
    "prodoctorov.ru",
    "napopravku.ru",
    "docdoc.ru",
    "sberhealth.ru",
    "infodoctor.ru",
    "startsmile.ru",
    "restoclub.ru",
    "afisha.ru",
    "tripadvisor.ru",
    "tripadvisor.com",
    "ostrovok.ru",
    "booking.com",
    "rusprofile.ru",
    "list-org.com",
    "checko.ru",
    "sbis.ru",
    "zachestnyibiznes.ru",
    "audit-it.ru",
    "avito.ru",
    "hh.ru",
    "youla.ru",
    "wikipedia.org",
    "youtube.com",
    "dzen.ru",
    "ok.ru",
    "facebook.com",
)

DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": DEFAULT_USER_AGENT,
}


def _host_in(host: str | None, domains: tuple[str, ...]) -> bool:
    if not host:
        return False
    normalized_host = host.lower().rstrip(".")
    return any(normalized_host == domain or normalized_host.endswith(f".{domain}") for domain in domains)


def is_social_domain(host: str | None) -> bool:
    """Check if the given host belongs to any known social/aggregator domain."""
    return _host_in(host, SOCIAL_DOMAINS)


def is_catalog_domain(host: str | None) -> bool:
    """A map, a catalog or a registry: it lists the organization, it is not its site."""
    return _host_in(host, CATALOG_DOMAINS)


def create_check_client(
    network_backend: httpcore.AsyncNetworkBackend | None = None,
) -> httpx.AsyncClient:
    """
    HTTP client for website checks that refuses non-public addresses (SSRF protection).

    Env proxies are ignored on purpose: with a proxy only the proxy host would be validated.
    """
    return httpx.AsyncClient(
        timeout=CHECK_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=DEFAULT_HEADERS,
        transport=PublicOnlyTransport(network_backend=network_backend),
        trust_env=False,
    )


def url_host(value: str) -> str | None:
    """Return the host of a URL that may lack a scheme, or None if it cannot be parsed."""
    url_to_parse = value if value.startswith(("http://", "https://")) else f"https://{value}"
    try:
        return httpx.URL(url_to_parse).host
    except (httpx.InvalidURL, ValueError):  # ValueError: e.g. idna.IDNAError for bad IDN hosts
        return None


async def analyze_website(
    url: str | None,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str | None]:
    """
    Analyze website status for a lead.

    Returns:
        tuple[str, str | None]: (site_status, details)
        - ("no_site", None): URL is empty or whitespace
        - ("social_only", url): URL points or redirects to social networks
        - ("dead_site", "HTTP/Connection error"): connection failed, timeout, or 4xx/5xx
          (except ALIVE_ERROR_STATUSES)
        - ("has_site", str(response.url)): responsive standalone website

    A URL without scheme is tried over https first and, if that fails with a
    connection/SSL error or a dead status, over plain http.
    """
    if not url or not url.strip():
        return ("no_site", None)

    cleaned_url = url.strip()
    has_scheme = cleaned_url.lower().startswith(("http://", "https://"))
    if not has_scheme:
        cleaned_url = f"https://{cleaned_url}"

    try:
        parsed_url = httpx.URL(cleaned_url)
        initial_host = parsed_url.host
    except (httpx.InvalidURL, ValueError):
        return DEAD_SITE_RESULT

    if is_social_domain(initial_host):
        return ("social_only", cleaned_url)

    if client is None:
        async with create_check_client() as own_client:
            return await _check_with_http_fallback(own_client, cleaned_url, retry_http=not has_scheme)
    return await _check_with_http_fallback(client, cleaned_url, retry_http=not has_scheme)


async def _check_with_http_fallback(
    client: httpx.AsyncClient,
    https_url: str,
    retry_http: bool,
) -> tuple[str, str | None]:
    """Probe the https URL; for schemeless input retry over http when https looks dead."""
    result, retryable = await _probe(client, https_url)
    if retry_http and retryable:
        http_url = "http://" + https_url[len("https://") :]
        result, _ = await _probe(client, http_url)
    return result


async def _probe(client: httpx.AsyncClient, url: str) -> tuple[tuple[str, str | None], bool]:
    """
    Classify a single URL.

    Returns (result, retryable): retryable is True when the failure may be
    https-specific (connection/SSL error or dead status), False for timeouts,
    since an unreachable host would only double the check time.
    """
    try:
        status_code, final_url = await asyncio.wait_for(
            _fetch_status(client, url),
            timeout=TOTAL_CHECK_TIMEOUT_SECONDS,
        )
    except (httpx.TimeoutException, TimeoutError):
        return DEAD_SITE_RESULT, False
    except (httpx.HTTPError, httpx.InvalidURL):
        return DEAD_SITE_RESULT, True

    if status_code >= 400 and status_code not in ALIVE_ERROR_STATUSES:
        return DEAD_SITE_RESULT, True

    if is_social_domain(final_url.host):
        return ("social_only", str(final_url)), False

    return ("has_site", str(final_url)), False


async def _fetch_status(client: httpx.AsyncClient, url: str) -> tuple[int, httpx.URL]:
    """Request the URL following redirects; return status and final URL without reading the body."""
    async with client.stream(
        "GET",
        url,
        follow_redirects=True,
        headers=DEFAULT_HEADERS,
    ) as response:
        return response.status_code, response.url
