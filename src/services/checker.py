"""Website analysis service to detect no-site, dead-site, social-only, or active site."""

import httpx

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
        - ("has_site", str(response.url)): responsive standalone website
    """
    if not url or not url.strip():
        return ("no_site", None)

    cleaned_url = url.strip()
    if not cleaned_url.lower().startswith(("http://", "https://")):
        cleaned_url = f"https://{cleaned_url}"

    try:
        parsed_url = httpx.URL(cleaned_url)
        initial_host = parsed_url.host
    except (httpx.InvalidURL, ValueError):
        return ("dead_site", "HTTP/Connection error")

    if is_social_domain(initial_host):
        return ("social_only", cleaned_url)

    try:
        if client is None:
            async with httpx.AsyncClient(
                timeout=7.0,
                follow_redirects=True,
                headers=DEFAULT_HEADERS,
            ) as client_to_use:
                response = await client_to_use.get(cleaned_url)
        else:
            response = await client.get(
                cleaned_url,
                follow_redirects=True,
                headers=DEFAULT_HEADERS,
            )

        if response.status_code >= 400:
            return ("dead_site", "HTTP/Connection error")

        final_host = response.url.host
        if is_social_domain(final_host):
            return ("social_only", str(response.url))

        return ("has_site", str(response.url))

    except (httpx.HTTPError, httpx.InvalidURL):
        return ("dead_site", "HTTP/Connection error")
