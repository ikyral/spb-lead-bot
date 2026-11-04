"""Website analysis service to detect no-site, dead-site, social-only, or active site."""

import asyncio

import httpx

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

DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": DEFAULT_USER_AGENT,
}


def is_social_domain(host: str | None) -> bool:
    """Check if the given host belongs to any known social/aggregator domain."""
    if not host:
        return False
    normalized_host = host.lower().rstrip(".")
    return any(
        normalized_host == domain or normalized_host.endswith(f".{domain}")
        for domain in SOCIAL_DOMAINS
    )


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
        async with httpx.AsyncClient(
            timeout=CHECK_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
        ) as own_client:
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
        http_url = "http://" + https_url[len("https://"):]
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
