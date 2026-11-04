"""Unit tests for checker and scorer services."""

import asyncio

import httpx
import pytest

from src.services import checker
from src.services.checker import SOCIAL_DOMAINS, analyze_website, is_social_domain
from src.services.scorer import calculate_lead_score


# --- Scorer Tests ---

def test_score_has_site_returns_zero() -> None:
    """If site_status is has_site, lead score must always be 0."""
    assert calculate_lead_score("dentist_clinic", "has_site", "+79991112233", "vk.com/group", True) == 0
    assert calculate_lead_score("horeca", "has_site", None, None, False) == 0
    assert calculate_lead_score("auto_repair", "has_site", "+78121112233", None, True) == 0


def test_score_dead_site_with_phone_and_high_margin() -> None:
    """dead_site (45) + phone (25) + high_margin (15) = 85."""
    score = calculate_lead_score("dentist_clinic", "dead_site", "+7 (812) 555-01-01", None, True)
    assert score == 85


def test_score_no_site_without_contacts_clamped_to_zero() -> None:
    """no_site (25) - neither phone nor socials (30) = -5 -> clamped to 0."""
    assert calculate_lead_score("horeca", "no_site", None, None, False) == 0
    assert calculate_lead_score("unknown", "no_site", "   ", "", False) == 0


def test_score_dead_site_full_contacts_and_margin() -> None:
    """dead_site (45) + phone (25) + socials (15) + high_margin (15) = 100."""
    score = calculate_lead_score(
        category_key="auto_repair",
        site_status="dead_site",
        phone="+79991112233",
        socials="https://vk.com/auto",
        high_margin=True,
    )
    assert score == 100


def test_score_social_only_combinations() -> None:
    """Verify various combinations for social_only."""
    # social_only (35) + phone (25) + socials (15) + high_margin (15) = 90
    assert calculate_lead_score("beauty", "social_only", "+7999", "vk.com/b", True) == 90
    # social_only (35) + phone (25) = 60
    assert calculate_lead_score("horeca", "social_only", "+7999", None, False) == 60
    # social_only (35) + socials (15) = 50
    assert calculate_lead_score("horeca", "social_only", None, "t.me/cafe", False) == 50
    # social_only (35) - 30 (no contacts) = 5
    assert calculate_lead_score("horeca", "social_only", None, None, False) == 5


def test_score_dead_site_no_contacts_low_margin() -> None:
    """dead_site (45) - 30 (no contacts) = 15."""
    assert calculate_lead_score("horeca", "dead_site", None, None, False) == 15


@pytest.mark.parametrize(
    "category",
    [
        "Стоматологическая клиника",
        "Автосервис и шиномонтаж",
        "Студия детейлинга",
        "Салон красоты",
        "Косметолог СПБ",
        "Юрист по недвижимости",
        "Бухучет и аудит",
        "Мини-отель",
        "База отдыха у озера",
        "Мебель на заказ",
        "Ремонт под ключ",
    ],
)
def test_score_high_margin_keywords_detection(category: str) -> None:
    """High margin keywords in category name should grant +15 bonus."""
    # dead_site (45) + phone (25) + margin keyword (15) = 85
    score = calculate_lead_score(
        category_key=category,
        site_status="dead_site",
        phone="+79991112233",
        socials=None,
        high_margin=False,
    )
    assert score == 85


# --- Checker Tests ---

def test_is_social_domain() -> None:
    """Verify social domain matcher for direct domains and subdomains."""
    for domain in SOCIAL_DOMAINS:
        assert is_social_domain(domain) is True
        assert is_social_domain(f"sub.{domain}") is True
        assert is_social_domain(f"www.{domain}") is True

    assert is_social_domain("example.com") is False
    assert is_social_domain("my-vk.com") is False
    assert is_social_domain("") is False
    assert is_social_domain(None) is False


@pytest.mark.asyncio
async def test_analyze_website_empty_or_whitespace() -> None:
    """Empty or whitespace URL must immediately return ('no_site', None)."""
    assert await analyze_website(None) == ("no_site", None)
    assert await analyze_website("") == ("no_site", None)
    assert await analyze_website("   ") == ("no_site", None)
    assert await analyze_website("\t \n") == ("no_site", None)


@pytest.mark.asyncio
async def test_analyze_website_direct_social_no_network() -> None:
    """URLs with social domains must return ('social_only', url) without making network calls."""
    # Provide a client whose handler would raise if any request is executed
    def unreached_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Network request should not have been made!")

    transport = httpx.MockTransport(unreached_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res1 = await analyze_website("vk.com/spb_group", client=client)
        assert res1 == ("social_only", "https://vk.com/spb_group")

        res2 = await analyze_website("https://t.me/my_business", client=client)
        assert res2 == ("social_only", "https://t.me/my_business")

        res3 = await analyze_website("http://taplink.cc/client", client=client)
        assert res3 == ("social_only", "http://taplink.cc/client")

        res4 = await analyze_website("https://spb.yclients.com/book", client=client)
        assert res4 == ("social_only", "https://spb.yclients.com/book")

        res5 = await analyze_website("dikidi.net/12345", client=client)
        assert res5 == ("social_only", "https://dikidi.net/12345")


@pytest.mark.asyncio
async def test_analyze_website_has_site_success() -> None:
    """Active website returning status 200 should return ('has_site', final_url)."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Welcome", request=request)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        status, details = await analyze_website("company.spb.ru", client=client)
        assert status == "has_site"
        assert details == "https://company.spb.ru"


@pytest.mark.asyncio
async def test_analyze_website_redirects_to_social() -> None:
    """Website redirecting to a social domain should be classified as ('social_only', redirected_url)."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "short-link.ru":
            return httpx.Response(302, headers={"Location": "https://vk.com/company_page"}, request=request)
        if request.url.host == "vk.com":
            return httpx.Response(200, text="VK Page", request=request)
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        status, details = await analyze_website("https://short-link.ru/to-vk", client=client)
        assert status == "social_only"
        assert details == "https://vk.com/company_page"


@pytest.mark.asyncio
async def test_analyze_website_dead_site_status_error() -> None:
    """HTTP 4xx/5xx responses must return ('dead_site', 'HTTP/Connection error')."""
    def mock_handler_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(mock_handler_404)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await analyze_website("https://broken-shop-404.ru", client=client)
        assert res == ("dead_site", "HTTP/Connection error")

    def mock_handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    transport_500 = httpx.MockTransport(mock_handler_500)
    async with httpx.AsyncClient(transport=transport_500) as client:
        res = await analyze_website("https://server-error-500.ru", client=client)
        assert res == ("dead_site", "HTTP/Connection error")


@pytest.mark.asyncio
async def test_analyze_website_dead_site_exceptions() -> None:
    """Network timeouts, connection errors, and invalid URLs must return ('dead_site', 'HTTP/Connection error')."""
    def mock_handler_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Connection timed out", request=request)

    transport_timeout = httpx.MockTransport(mock_handler_timeout)
    async with httpx.AsyncClient(transport=transport_timeout) as client:
        res = await analyze_website("https://timeout-site.ru", client=client)
        assert res == ("dead_site", "HTTP/Connection error")

    def mock_handler_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Failed to resolve host", request=request)

    transport_connect = httpx.MockTransport(mock_handler_connect_error)
    async with httpx.AsyncClient(transport=transport_connect) as client:
        res = await analyze_website("https://unreachable-host.ru", client=client)
        assert res == ("dead_site", "HTTP/Connection error")

    # Malformed URL
    res_malformed = await analyze_website("https://::invalid::url")
    assert res_malformed == ("dead_site", "HTTP/Connection error")


@pytest.mark.asyncio
async def test_analyze_website_total_timeout_marks_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """A site that never finishes responding must not stall the check beyond the total deadline."""
    monkeypatch.setattr(checker, "TOTAL_CHECK_TIMEOUT_SECONDS", 0.05)

    async def hanging_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(hanging_handler)) as client:
        res = await asyncio.wait_for(analyze_website("https://slow-site.ru", client=client), timeout=2)
    assert res == ("dead_site", "HTTP/Connection error")


@pytest.mark.asyncio
async def test_analyze_website_does_not_download_body() -> None:
    """Only status and final URL are needed, so the response body must not be read."""

    class ExplodingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("Response body should not be downloaded")
            yield b""  # pragma: no cover

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ExplodingStream(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await analyze_website("https://big-file-site.ru", client=client)
    assert res == ("has_site", "https://big-file-site.ru")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429])
async def test_analyze_website_bot_protection_statuses_mean_site_is_alive(status_code: int) -> None:
    """401/403/429 come from a live server that blocks bots or rate-limits us, not from a dead site."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await analyze_website("https://cloudflare-protected.ru", client=client)
    assert res == ("has_site", "https://cloudflare-protected.ru")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 410, 500, 502, 503])
async def test_analyze_website_real_error_statuses_stay_dead(status_code: int) -> None:
    """Other 4xx/5xx statuses still mean the site is broken."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await analyze_website("https://broken.ru", client=client)
    assert res == ("dead_site", "HTTP/Connection error")


@pytest.mark.asyncio
async def test_analyze_website_bot_protection_redirect_to_social_is_social_only() -> None:
    """A protected social page reached via redirect is still classified as social_only."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "short-link.ru":
            return httpx.Response(302, headers={"Location": "https://vk.com/page"}, request=request)
        return httpx.Response(403, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await analyze_website("https://short-link.ru", client=client)
    assert res == ("social_only", "https://vk.com/page")


@pytest.mark.asyncio
async def test_analyze_website_schemeless_falls_back_to_http_on_ssl_error() -> None:
    """A schemeless URL whose site only works over plain http must not be reported as dead."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.scheme == "https":
            raise httpx.ConnectError("SSL: WRONG_VERSION_NUMBER", request=request)
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await analyze_website("legacy-site.ru", client=client)
    assert res == ("has_site", "http://legacy-site.ru")
    assert requested == ["https://legacy-site.ru", "http://legacy-site.ru"]


@pytest.mark.asyncio
async def test_analyze_website_schemeless_falls_back_to_http_on_https_error_status() -> None:
    """https served by a wrong default vhost (404) while http works must not be reported as dead."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await analyze_website("www.legacy-site.ru/contacts", client=client)
    assert res == ("has_site", "http://www.legacy-site.ru/contacts")


@pytest.mark.asyncio
async def test_analyze_website_schemeless_dead_on_both_schemes() -> None:
    """If both https and http fail, the site is dead."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.scheme)
        raise httpx.ConnectError("Failed to resolve host", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await analyze_website("no-such-domain-spb.ru", client=client)
    assert res == ("dead_site", "HTTP/Connection error")
    assert requested == ["https", "http"]


@pytest.mark.asyncio
async def test_analyze_website_no_http_fallback_after_timeout_or_explicit_https() -> None:
    """Timeouts are not retried over http (host is unreachable), nor are URLs with an explicit scheme."""
    requested: list[str] = []

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        raise httpx.ConnectTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
        res = await analyze_website("slow-host.ru", client=client)
    assert res == ("dead_site", "HTTP/Connection error")
    assert requested == ["https://slow-host.ru"]

    requested.clear()

    def ssl_error_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        raise httpx.ConnectError("SSL error", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(ssl_error_handler)) as client:
        res = await analyze_website("https://explicit-https.ru", client=client)
    assert res == ("dead_site", "HTTP/Connection error")
    assert requested == ["https://explicit-https.ru"]
