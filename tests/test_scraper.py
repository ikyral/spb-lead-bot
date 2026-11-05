"""Tests for the 2GIS page reader: a mocked browser, responses from fixtures, and how refusals are handled."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select

from src.config import Settings
from src.database.models import ProviderState
from src.database.repo import (
    MAX_CONSECUTIVE_BLOCKS,
    enable_provider,
    get_provider_states,
    record_provider_block,
    record_provider_success,
)
from src.services import pipeline
from src.services.api_client import (
    ProviderBlockedError,
    ProviderBudgetExhausted,
    ProviderTemporaryError,
    ProviderUnavailableError,
)
from src.services.collector import collect_leads, load_collector_config, unavailable_reason
from src.services.providers import ScraperOptions, TwoGisScraperProvider
from src.services.providers.base import BaseMapProvider, RawPlace, SearchArea, SearchPage, SearchQuery
from src.services.providers.registry import build_providers
from src.services.scraper import twogis_site
from src.services.scraper.browser import PageSnapshot, PlaywrightSession
from src.services.scraper.robots import RobotsRules, RobotsUnavailableError, fetch_robots, parse_robots

FIXTURES = Path(__file__).parent / "fixtures"
QUERY = SearchQuery(text="стоматология", category="dentist_clinic", high_margin=True)
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Центральный")
NORMAL_URL = "https://2gis.ru/spb/search/x"


def _json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _ok_page(*bodies: dict) -> PageSnapshot:
    return PageSnapshot(
        status=200, final_url=NORMAL_URL, title="Стоматологии", html="<html></html>", json_bodies=list(bodies)
    )


CAPTCHA_PAGE = PageSnapshot(
    status=200,
    final_url="https://captcha.2gis.ru/form?return_url=https://2gis.ru/spb/search/x",
    title="2GIS Captcha",
    html=_text("twogis_captcha.html"),
)


class FakeSession:
    """The browser: returns prepared pages in order and records what happened."""

    def __init__(self, pages: list[PageSnapshot] | None = None) -> None:
        self.pages = list(pages or [])
        self.visited: list[str] = []
        self.started = 0
        self.closed = 0
        self.fail_start: Exception | None = None

    async def start(self) -> None:
        self.started += 1
        if self.fail_start is not None:
            raise self.fail_start

    async def visit(self, url: str, wants_body) -> PageSnapshot:
        self.visited.append(url)
        return self.pages.pop(0)

    async def close(self) -> None:
        self.closed += 1


class Pauses:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _provider(
    session: FakeSession,
    options: ScraperOptions | None = None,
    pauses: Pauses | None = None,
    rules: RobotsRules | None = None,
) -> TwoGisScraperProvider:
    async def load_rules() -> RobotsRules:
        return rules or RobotsRules()

    return TwoGisScraperProvider(
        options=options, session_factory=lambda: session, robots_loader=load_rules, sleep=pauses or Pauses()
    )


# ---------- URLs and page recognition ----------


def test_public_urls() -> None:
    assert twogis_site.build_search_url("spb", "кафе") == "https://2gis.ru/spb/search/%D0%BA%D0%B0%D1%84%D0%B5"
    assert twogis_site.build_search_url("spb", "кафе", 3).endswith("/page/3")
    assert twogis_site.build_search_url("spb", "кафе / бар") == (
        "https://2gis.ru/spb/search/%D0%BA%D0%B0%D1%84%D0%B5%20%2F%20%D0%B1%D0%B0%D1%80"
    )
    assert twogis_site.build_card_url("spb", "70000001_abc") == "https://2gis.ru/spb/firm/70000001"


@pytest.mark.parametrize(
    ("url", "search", "card"),
    [
        ("https://catalog.api.2gis.ru/3.0/items?q=x&key=k", True, False),
        ("https://catalog.api.2gis.com/3.0/items/", True, False),
        ("https://catalog.api.2gis.ru/3.0/items/byid?id=1", False, True),
        ("https://catalog.api.2gis.ru/2.0/region/list", False, False),
        ("https://evil.example/3.0/items", False, False),
        ("https://catalog.api.2gis.ru.evil.example/3.0/items", False, False),
    ],
)
def test_which_responses_are_read(url: str, search: bool, card: bool) -> None:
    assert twogis_site.is_search_response(url) is search
    assert twogis_site.is_card_response(url) is card


@pytest.mark.parametrize(
    ("status", "url", "title", "html", "expected"),
    [
        (200, "https://captcha.2gis.ru/form?return_url=x", "2GIS Captcha", _text("twogis_captcha.html"), "капч"),
        (200, NORMAL_URL, "Страница", _text("twogis_captcha.html"), "капч"),  # the real page, under another address
        (403, NORMAL_URL, "Forbidden", "", "403"),
        (429, NORMAL_URL, "Too Many Requests", "", "429"),
        (401, NORMAL_URL, "", "", "401"),
        (200, NORMAL_URL, "Стоматологии", "<html>обычная страница</html>", None),
        (500, NORMAL_URL, "", "", None),  # a server failure is not a refusal
    ],
)
def test_detect_block(status: int, url: str, title: str, html: str, expected: str | None) -> None:
    reason = twogis_site.detect_block(status, url, title, html)
    if expected is None:
        assert reason is None
    else:
        assert expected in reason


def test_reading_responses_tolerates_odd_data() -> None:
    assert twogis_site.read_search_bodies([]) is None
    assert twogis_site.read_search_bodies([{"unrelated": 1}, {"result": "строка"}]) is None
    assert twogis_site.read_search_bodies([{"meta": {"code": 404}}]).items == []
    data = twogis_site.read_search_bodies([{"result": {"items": ["мусор", {"id": "1"}], "total": "много"}}])
    assert (data.items, data.total) == ([{"id": "1"}], None)
    assert twogis_site.read_card_body([{"result": {"items": ["мусор"]}}], "1") is None
    assert twogis_site.refusal_in_bodies([{"meta": {"code": 403}}]) is not None
    assert twogis_site.refusal_in_bodies([{"meta": {"code": 200}}, {"meta": "?"}]) is None


# ---------- robots.txt ----------


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://2gis.ru/spb/search/%D0%BA%D0%B0%D1%84%D0%B5", True),
        ("https://2gis.ru/spb/search/%D0%BA%D0%B0%D1%84%D0%B5/page/2", True),
        ("https://2gis.ru/spb/firm/70000001018420390", True),
        ("https://2gis.ru/_/internal", False),
        ("https://2gis.ru/spb/user/profile", False),
        ("https://2gis.ru/spb/search/x?stat=1", False),
        ("https://2gis.ru/spb/my", False),
        ("https://2gis.ru/spb/myself", True),
        ("https://2gis.ru/spb/search/x?refHash=1", False),
    ],
)
def test_real_robots_file_of_2gis(url: str, allowed: bool) -> None:
    assert parse_robots(_text("twogis_robots.txt"), "spb-lead-bot").is_allowed(url) is allowed


def test_robots_group_selection_and_rule_priority() -> None:
    text = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: spb-lead-bot\nUser-agent: other\nDisallow: /private\nAllow: /private/open\n"
    )
    named = parse_robots(text, "SPB-Lead-Bot")
    assert named.is_allowed("https://x.example/public") is True
    assert named.is_allowed("https://x.example/private/a") is False
    assert named.is_allowed("https://x.example/private/open/a") is True  # the longer rule wins
    assert parse_robots(text, "someone").is_allowed("https://x.example/public") is False
    assert parse_robots("User-agent: *\nDisallow:\n", "a").is_allowed("https://x.example/a") is True
    assert parse_robots("# только комментарий\n\nбессмыслица\n", "a").is_allowed("https://x.example/a") is True


def _robots_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_missing_robots_means_allowed_but_unreadable_means_stop() -> None:
    rules = await fetch_robots("https://x.example", "a", _robots_client(lambda r: httpx.Response(404)))
    assert rules.is_allowed("https://x.example/anything")

    with pytest.raises(RobotsUnavailableError):
        await fetch_robots("https://x.example", "a", _robots_client(lambda r: httpx.Response(503)))

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(RobotsUnavailableError):
        await fetch_robots("https://x.example", "a", _robots_client(fail))


# ---------- reading pages ----------


async def test_page_is_read_from_json_and_cards_give_missing_contacts() -> None:
    session = FakeSession([_ok_page(_json("twogis_page_search.json")), _ok_page(_json("twogis_page_card.json"))])
    pauses = Pauses()
    provider = _provider(session, pauses=pauses)

    page = await provider.search(QUERY, AREA, page=1)

    assert [place.external_id for place in page.places] == ["2gis_70000001018420390_abc", "2gis_70000001018420391_def"]
    first, second = page.places
    assert (first.source, first.category, first.high_margin) == ("2gis", "dentist_clinic", True)
    assert first.district == "Центральный"
    assert first.phone == "+7 (812) 555-01-01"
    assert first.source_url == "https://2gis.ru/firm/70000001018420390"
    assert second.phone == "+7 921 000-00-02"
    assert second.email == "info@dentpro.example"
    assert second.raw_website == "https://dentpro.example/?utm_source=2gis"
    assert second.map_url  # coordinates come from the list, the card has none

    # The building and the malformed item are not opened; the search text includes the district.
    assert len(session.visited) == 2
    assert session.visited[0].startswith("https://2gis.ru/spb/search/")
    assert session.visited[1] == "https://2gis.ru/spb/firm/70000001018420391"
    assert page.has_more is False  # all 3 items of total=3 are shown
    assert len(pauses.delays) == 1


async def test_has_more_follows_the_total() -> None:
    body = {"result": {"total": 2, "items": [{"id": "1", "name": "A", "contact_groups": ["x"]}]}}
    provider = _provider(FakeSession([_ok_page(body), _ok_page(body)]))
    assert (await provider.search(QUERY, AREA, page=1)).has_more is True
    assert (await provider.search(QUERY, AREA, page=2)).has_more is False


async def test_empty_result_is_normal() -> None:
    provider = _provider(FakeSession([_ok_page({"meta": {"code": 404}})]))
    page = await provider.search(QUERY, AREA)
    assert (page.places, page.has_more) == ([], False)


async def test_pauses_are_random_within_limits_and_requests_go_one_by_one() -> None:
    pages = [_ok_page({"result": {"items": [{"id": str(i), "name": "A", "contact_groups": ["x"]}]}}) for i in range(30)]
    pauses = Pauses()
    provider = _provider(FakeSession(pages), pauses=pauses)
    for number in range(1, 31):
        await provider.search(QUERY, AREA, page=number)
    assert len(pauses.delays) == 29
    assert all(3.0 <= delay <= 8.0 for delay in pauses.delays)
    assert len(set(pauses.delays)) > 1


def test_too_short_pauses_cannot_be_configured() -> None:
    with pytest.raises(ValueError, match="SCRAPER_MIN_DELAY_SECONDS"):
        Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1, SCRAPER_MIN_DELAY_SECONDS=1)
    with pytest.raises(ValueError, match="SCRAPER_MIN_DELAY_SECONDS"):
        load_collector_config(
            Settings(
                _env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1, SCRAPER_MIN_DELAY_SECONDS=9, SCRAPER_MAX_DELAY_SECONDS=8
            )
        )


async def test_page_limit_per_run_stops_opening_pages_and_drops_uncontacted_organizations() -> None:
    body = {
        "result": {
            "items": [
                {"id": "1", "name": "С контактами", "contact_groups": ["x"]},
                {"id": "2", "name": "Без контактов"},
                {"id": "3", "name": "Без контактов 2"},
            ]
        }
    }
    session = FakeSession([_ok_page(body)])
    provider = _provider(session, ScraperOptions(max_pages_per_run=1))

    page = await provider.search(QUERY, AREA)

    assert [place.name for place in page.places] == ["С контактами"]  # others would look like having no website
    assert page.has_more is False
    assert len(session.visited) == 1
    with pytest.raises(ProviderBudgetExhausted):
        await provider.search(QUERY, AREA, page=2)


async def test_results_limit_per_run() -> None:
    items = [{"id": str(i), "name": "A", "contact_groups": ["x"]} for i in (1, 2)]
    body = {"result": {"items": items}}
    provider = _provider(FakeSession([_ok_page(body)]), ScraperOptions(max_results_per_run=2))
    await provider.search(QUERY, AREA)
    with pytest.raises(ProviderBudgetExhausted):
        await provider.search(QUERY, AREA, page=2)


async def test_limits_start_over_for_the_next_run() -> None:
    body = {"result": {"items": [{"id": "1", "name": "A", "contact_groups": ["x"]}]}}
    session = FakeSession([_ok_page(body), _ok_page(body)])
    provider = _provider(session, ScraperOptions(max_pages_per_run=1))
    await provider.search(QUERY, AREA)
    await provider.close()
    await provider.search(QUERY, AREA)
    assert session.started == 2


# ---------- refusals ----------


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (CAPTCHA_PAGE, "капч"),
        (PageSnapshot(status=403, final_url=NORMAL_URL, title="", html=""), "403"),
        (PageSnapshot(status=429, final_url=NORMAL_URL, title="", html=""), "429"),
        (_ok_page(), "не распознана"),  # a page without the expected data
        (_ok_page({"unexpected": True}), "не распознана"),
        (_ok_page({"meta": {"code": 403}}), "403"),
    ],
)
async def test_refusal_stops_reading_at_once(page: PageSnapshot, expected: str) -> None:
    session = FakeSession([page, _ok_page()])
    provider = _provider(session)
    with pytest.raises(ProviderBlockedError, match=expected):
        await provider.search(QUERY, AREA)
    assert len(session.visited) == 1


async def test_captcha_on_a_card_keeps_what_was_read() -> None:
    session = FakeSession([_ok_page(_json("twogis_page_search.json")), CAPTCHA_PAGE])
    with pytest.raises(ProviderBlockedError, match="капч") as error:
        await _provider(session).search(QUERY, AREA)
    assert [place.name for place in error.value.partial] == ["Улыбка, стоматология"]


async def test_unrecognized_card_counts_as_a_refusal() -> None:
    session = FakeSession([_ok_page(_json("twogis_page_search.json")), _ok_page()])
    with pytest.raises(ProviderBlockedError, match="карточка"):
        await _provider(session).search(QUERY, AREA)


async def test_robots_forbidding_stops_before_opening_anything() -> None:
    session = FakeSession([_ok_page()])
    rules = parse_robots("User-agent: *\nDisallow: /spb/search/\n", "a")
    with pytest.raises(ProviderUnavailableError, match=r"robots\.txt"):
        await _provider(session, rules=rules).search(QUERY, AREA)
    assert session.visited == []
    assert session.started == 0


async def test_unreadable_robots_stops_before_opening_anything() -> None:
    session = FakeSession()

    async def broken_loader() -> RobotsRules:
        raise RobotsUnavailableError("код 503")

    provider = TwoGisScraperProvider(session_factory=lambda: session, robots_loader=broken_loader, sleep=Pauses())
    with pytest.raises(ProviderUnavailableError, match=r"robots\.txt"):
        await provider.search(QUERY, AREA)
    assert session.started == 0


async def test_browser_is_closed_after_failures_and_close_is_safe_to_repeat() -> None:
    session = FakeSession([CAPTCHA_PAGE])
    provider = _provider(session)
    with pytest.raises(ProviderBlockedError):
        await provider.search(QUERY, AREA)
    await provider.close()
    await provider.close()
    assert session.closed == 1


async def test_browser_that_failed_to_start_is_still_closed() -> None:
    session = FakeSession()
    session.fail_start = ProviderUnavailableError("браузер не запустился")
    provider = _provider(session)
    with pytest.raises(ProviderUnavailableError):
        await provider.search(QUERY, AREA)
    await provider.close()
    assert session.closed == 1


# ---------- the browser wrapper with Playwright mocked ----------


def _playwright_mock() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    page = MagicMock()
    page.close = AsyncMock()
    page.url = NORMAL_URL
    context = MagicMock()
    context.add_init_script = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    playwright.stop = AsyncMock()
    return playwright, browser, context, page


def _start_returning(playwright: MagicMock) -> MagicMock:
    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)
    return MagicMock(return_value=starter)


async def test_browser_starts_with_stealth_and_everything_is_closed() -> None:
    from src.services.scraper.browser import DEFAULT_USER_AGENT, STEALTH_ARGS, STEALTH_INIT_SCRIPT

    playwright, browser, context, page = _playwright_mock()
    session = PlaywrightSession()
    with patch("playwright.async_api.async_playwright", _start_returning(playwright)):
        await session.start()

    playwright.chromium.launch.assert_awaited_once_with(headless=True, args=STEALTH_ARGS)
    browser.new_context.assert_awaited_once_with(
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        user_agent=DEFAULT_USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        extra_http_headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    )
    context.add_init_script.assert_awaited_once_with(STEALTH_INIT_SCRIPT)

    await session.close()
    page.close.assert_awaited_once()
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()

async def test_failure_of_one_close_step_does_not_skip_the_rest() -> None:
    playwright, browser, context, page = _playwright_mock()
    page.close.side_effect = RuntimeError("page is gone")
    browser.close.side_effect = RuntimeError("browser is gone")
    session = PlaywrightSession()
    with patch("playwright.async_api.async_playwright", _start_returning(playwright)):
        await session.start()
    await session.close()
    context.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()


async def test_missing_browser_gives_install_instructions_and_stops_the_driver() -> None:
    from playwright.async_api import Error as PlaywrightError

    playwright, _browser, _context, _page = _playwright_mock()
    playwright.chromium.launch.side_effect = PlaywrightError("Executable doesn't exist")
    session = PlaywrightSession()
    with (
        patch("playwright.async_api.async_playwright", _start_returning(playwright)),
        pytest.raises(ProviderUnavailableError, match="playwright install chromium"),
    ):
        await session.start()
    playwright.stop.assert_awaited_once()


async def test_browser_errors_do_not_leak_urls() -> None:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    playwright, _browser, _context, page = _playwright_mock()
    session = PlaywrightSession()
    with patch("playwright.async_api.async_playwright", _start_returning(playwright)):
        await session.start()

    page.goto = AsyncMock(side_effect=PlaywrightError("net::ERR at https://x.example/?key=SECRET"))
    with pytest.raises(ProviderTemporaryError) as error:
        await session.visit("https://x.example/", lambda url: False)
    assert "SECRET" not in str(error.value)

    page.goto = AsyncMock(side_effect=PlaywrightTimeoutError("Timeout at https://x.example/?key=SECRET"))
    with pytest.raises(ProviderTemporaryError) as error:
        await session.visit("https://x.example/", lambda url: False)
    assert "SECRET" not in str(error.value)
    assert page.remove_listener.call_count == 2


# ---------- how the collector treats a blocked provider ----------


CAPTCHA_ERROR = ProviderBlockedError("сайт показал капчу")


class BlockingProvider(BaseMapProvider):
    name = "2gis_scraper"
    min_hours_between_runs = 0.0

    def __init__(self, error: Exception | None = CAPTCHA_ERROR) -> None:
        self.error = error
        self.calls = 0
        self.closed = 0

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query, area, page=1, page_size=50) -> SearchPage:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SearchPage(places=[_place(self.calls)])

    async def close(self) -> None:
        self.closed += 1


class RecordingProvider(BaseMapProvider):
    def __init__(self, name: str = "osm", fallback_for: str | None = None) -> None:
        self.name = name
        self.fallback_for = fallback_for
        self.calls = 0

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query, area, page=1, page_size=50) -> SearchPage:
        self.calls += 1
        return SearchPage(places=[_place(100 + self.calls, source=self.name)])


def _place(number: int, source: str = "2gis") -> RawPlace:
    return RawPlace(
        external_id=f"{source}_{number}",
        source=source,
        name=f"Организация {source} {number}",
        category="dentist_clinic",
        phone=f"+7 812 555-{number % 100:02d}-00" if source == "2gis" else f"+7 921 555-{number % 100:02d}-00",
    )


@pytest.fixture(autouse=True)
def _no_site_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_analyze(url, client=None):
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)


def _config(**overrides):
    defaults = {"COLLECT_QUERIES": "dentist_clinic,beauty", "COLLECT_DISTRICTS": "Центральный"}
    return load_collector_config(Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1, **{**defaults, **overrides}))


async def _state(session_maker, name: str = "2gis_scraper") -> ProviderState | None:
    async with session_maker() as session:
        return (await get_provider_states(session)).get(name)


async def test_block_stops_the_provider_alerts_and_other_sources_keep_working(session_maker) -> None:
    blocked = BlockingProvider()
    other = RecordingProvider()

    result = await collect_leads(session_maker, [blocked, other], _config())

    assert blocked.calls == 1  # not asked again in this run, even for the second target
    assert other.calls == 2
    assert result.saved_new == 2
    assert blocked.closed == 1
    assert len(result.alerts) == 1
    assert "капч" in result.alerts[0]
    assert "повтор не раньше" in result.alerts[0]
    state = await _state(session_maker)
    assert state.consecutive_blocks == 1
    assert state.disabled_at is None


async def test_retry_is_hours_away_and_the_pause_grows_exponentially(session_maker) -> None:
    before = datetime.now(UTC)
    async with session_maker() as session:
        first = await record_provider_block(session, "p", "капча", backoff_hours=6)
        first_until = first.blocked_until
        second = await record_provider_block(session, "p", "капча", backoff_hours=6)
        second_until = second.blocked_until
    assert first_until - before >= timedelta(hours=6)
    assert first_until - before < timedelta(hours=7)
    assert second_until - before >= timedelta(hours=12)
    assert second.disabled_at is None


async def test_provider_is_not_used_during_the_pause(session_maker) -> None:
    blocked = BlockingProvider()
    await collect_leads(session_maker, [blocked], _config())
    assert blocked.calls == 1

    result = await collect_leads(session_maker, [blocked], _config())

    assert blocked.calls == 1
    assert "пауза" in result.skipped["2gis_scraper"]
    assert result.alerts == []  # nothing new happened


async def test_three_blocks_in_a_row_switch_the_provider_off_until_an_admin_turns_it_on(session_maker) -> None:
    blocked = BlockingProvider()
    for number in range(1, MAX_CONSECUTIVE_BLOCKS + 1):
        result = await collect_leads(session_maker, [blocked], _config())
        assert blocked.calls == number
        async with session_maker() as session:  # the pause has passed
            state = (await get_provider_states(session))["2gis_scraper"]
            state.blocked_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    assert "/provider_on 2gis_scraper" in result.alerts[0]
    state = await _state(session_maker)
    assert state.disabled_at is not None

    # The pause is over, but it stays off.
    result = await collect_leads(session_maker, [blocked], _config())
    assert blocked.calls == MAX_CONSECUTIVE_BLOCKS
    assert "ручное включение" in result.skipped["2gis_scraper"]

    async with session_maker() as session:
        await enable_provider(session, "2gis_scraper")
    state = await _state(session_maker)
    assert (state.disabled_at, state.blocked_until, state.consecutive_blocks) == (None, None, 0)

    blocked.error = None
    result = await collect_leads(session_maker, [blocked], _config())
    assert blocked.calls > MAX_CONSECUTIVE_BLOCKS
    assert result.saved_new > 0


async def test_success_means_later_blocks_are_not_in_a_row(session_maker) -> None:
    async with session_maker() as session:
        await record_provider_block(session, "2gis_scraper", "капча", backoff_hours=6)
        await record_provider_block(session, "2gis_scraper", "капча", backoff_hours=6)
    async with session_maker() as session:
        state = (await get_provider_states(session))["2gis_scraper"]
        state.blocked_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    await collect_leads(session_maker, [BlockingProvider(error=None)], _config())

    state = await _state(session_maker)
    assert (state.consecutive_blocks, state.blocked_until) == (0, None)
    async with session_maker() as session:
        again = await record_provider_block(session, "2gis_scraper", "капча", backoff_hours=6)
    assert again.consecutive_blocks == 1
    assert again.disabled_at is None


async def test_success_without_any_block_creates_no_state(session_maker) -> None:
    async with session_maker() as session:
        await record_provider_success(session, "никогда не блокировался")
        assert list((await session.scalars(select(ProviderState))).all()) == []


async def test_fallback_is_idle_while_the_main_source_works(session_maker) -> None:
    fallback = RecordingProvider("2gis", fallback_for="2gis_scraper")
    await collect_leads(session_maker, [BlockingProvider(error=None), fallback], _config())
    assert fallback.calls == 0


async def test_fallback_takes_over_right_after_a_block_and_during_the_pause(session_maker) -> None:
    fallback = RecordingProvider("2gis", fallback_for="2gis_scraper")
    blocked = BlockingProvider()
    await collect_leads(session_maker, [blocked, fallback], _config())
    assert fallback.calls == 2  # the blocked target and the next one

    await collect_leads(session_maker, [blocked, fallback], _config())
    assert fallback.calls == 4


async def test_fallback_works_when_the_main_source_is_not_configured(session_maker) -> None:
    fallback = RecordingProvider("2gis", fallback_for="2gis_scraper")
    await collect_leads(session_maker, [fallback], _config())
    assert fallback.calls == 2


async def test_scraper_runs_once_a_day_and_extra_runs_use_other_sources(session_maker) -> None:
    daily = BlockingProvider(error=None)
    daily.min_hours_between_runs = 20
    fallback = RecordingProvider("2gis", fallback_for="2gis_scraper")
    other = RecordingProvider()

    await collect_leads(session_maker, [daily, fallback, other], _config())
    calls = daily.calls
    assert calls == 2

    result = await collect_leads(session_maker, [daily, fallback, other], _config())
    assert daily.calls == calls
    assert "сегодня" in result.skipped["2gis_scraper"]
    assert other.calls == 4
    assert fallback.calls == 0  # resting is not a failure: a paid key is not spent for it

    async with session_maker() as session:
        state = (await get_provider_states(session))["2gis_scraper"]
        state.last_run_at = datetime.now(UTC) - timedelta(hours=21)
        await session.commit()
    await collect_leads(session_maker, [daily, fallback, other], _config())
    assert daily.calls == calls + 2


async def test_browser_is_closed_when_the_run_is_interrupted(session_maker) -> None:
    class Crashing(BlockingProvider):
        async def search(self, query, area, page=1, page_size=50) -> SearchPage:
            raise KeyboardInterrupt

    provider = Crashing()
    with pytest.raises(KeyboardInterrupt):
        await collect_leads(session_maker, [provider], _config())
    assert provider.closed == 1


async def test_failure_to_close_one_provider_does_not_affect_others(session_maker) -> None:
    class BadClose(BlockingProvider):
        async def close(self) -> None:
            raise RuntimeError("cannot close")

    good = BlockingProvider(error=None)
    good.name = "good"
    await collect_leads(session_maker, [BadClose(error=None), good], _config())
    assert good.closed == 1


async def test_unavailable_provider_alerts_without_counting_as_a_block(session_maker) -> None:
    provider = BlockingProvider(error=ProviderUnavailableError("robots.txt сайта запрещает открывать такие страницы"))
    result = await collect_leads(session_maker, [provider], _config())
    assert provider.calls == 1
    assert "robots.txt" in result.alerts[0]
    assert await _state(session_maker) is None or (await _state(session_maker)).consecutive_blocks == 0


async def test_budget_end_is_not_an_error(session_maker) -> None:
    result = await collect_leads(
        session_maker, [BlockingProvider(error=ProviderBudgetExhausted("лимит страниц"))], _config()
    )
    assert (result.errors, result.alerts) == (0, [])


def test_unavailable_reason_without_state() -> None:
    assert unavailable_reason(BlockingProvider(), None, datetime.now(UTC)) is None


def test_registry_knows_the_scraper_and_explains_what_is_off() -> None:
    result = build_providers(["2gis_scraper", "2gis", "osm"], twogis_api_key=None)
    assert [provider.name for provider in result.providers] == ["2gis_scraper", "osm"]
    assert result.disabled == {"2gis": "не задан DGIS_API_KEY"}

    with_key = build_providers(["2gis_scraper", "2gis"], twogis_api_key="key")
    assert with_key.providers[1].fallback_for == "2gis_scraper"


def test_registry_without_playwright_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    result = build_providers(["2gis_scraper"], twogis_api_key=None)
    assert result.providers == []
    assert "playwright" in result.disabled["2gis_scraper"]


@pytest.mark.parametrize("name", ["DGIS_API_KEY", "TWOGIS_API_KEY"])
def test_api_key_can_use_either_name(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.delenv("DGIS_API_KEY", raising=False)
    monkeypatch.delenv("TWOGIS_API_KEY", raising=False)
    monkeypatch.setenv(name, "ключ")
    settings = Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1)
    assert settings.TWOGIS_API_KEY.get_secret_value() == "ключ"
