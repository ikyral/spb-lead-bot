"""Tests for the assisted mode of the 2GIS page reader: a person passes a captcha, the code only waits."""

import asyncio
import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import PROJECT_ROOT, Settings
from src.database.models import ProviderState
from src.database.repo import MAX_CONSECUTIVE_BLOCKS, get_provider_states
from src.services import pipeline
from src.services.api_client import ProviderBlockedError, ProviderBudgetExhausted, ProviderUnavailableError
from src.services.collector import REST_REASON, collect_leads, load_collector_config
from src.services.providers import ScraperOptions, TwoGisScraperProvider
from src.services.providers.base import SearchArea, SearchQuery
from src.services.providers.twogis_scraper import (
    ASSISTED_MAX_PAGES_PER_RUN,
    ASSISTED_MAX_RESULTS_PER_RUN,
    CAPTCHA_POLL_SECONDS,
)
from src.services.scraper import browser as browser_module
from src.services.scraper.browser import PageSnapshot, PlaywrightSession
from src.services.scraper.robots import RobotsRules

FIXTURES = Path(__file__).parent / "fixtures"
QUERY = SearchQuery(text="стоматология", category="dentist_clinic", high_margin=True)
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Центральный")
NORMAL_URL = "https://2gis.ru/spb/search/x"
BODY = {"result": {"total": 100, "items": [{"id": "1", "name": "Клиника", "contact_groups": ["x"]}]}}
OPTIONS = ScraperOptions(assisted=True, headless=False, profile_dir=Path("profile"))

CAPTCHA = PageSnapshot(
    status=200,
    final_url="https://captcha.2gis.ru/form?return_url=https://2gis.ru/spb/search/x",
    title="2GIS Captcha",
    html=(FIXTURES / "twogis_captcha.html").read_text(encoding="utf-8"),
)
OK = PageSnapshot(status=200, final_url=NORMAL_URL, title="Стоматологии", html="<html></html>", json_bodies=[BODY])
PASSED = PageSnapshot(status=None, final_url=NORMAL_URL, title="Стоматологии", html="<html></html>")


class Session:
    """The browser: `pages` answer visits, `shown` answers each look at the window, then the last one stays."""

    def __init__(self, pages: list[PageSnapshot], shown: list[PageSnapshot | None] | None = None) -> None:
        self.pages = list(pages)
        self.shown = list(shown or [PASSED])
        self.visited: list[str] = []
        self.reads = 0
        self.fronted = 0
        self.closed = 0
        self.window_gone = False

    async def start(self) -> None:
        return None

    async def visit(self, url: str, wants_body) -> PageSnapshot:
        self.visited.append(url)
        return self.pages.pop(0)

    async def read_page(self) -> PageSnapshot | None:
        self.reads += 1
        if self.window_gone:
            raise ProviderUnavailableError("окно браузера закрыто")
        return self.shown.pop(0) if len(self.shown) > 1 else self.shown[0]

    async def bring_to_front(self) -> None:
        self.fronted += 1

    async def close(self) -> None:
        self.closed += 1


class Time:
    """Sleeping moves the clock; nothing really waits."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay

    def clock(self) -> float:
        return self.now

    @property
    def page_pauses(self) -> list[float]:
        return [delay for delay in self.delays if delay != CAPTCHA_POLL_SECONDS]


def _provider(
    session: Session, options: ScraperOptions = OPTIONS, time: Time | None = None, told: list[str] | None = None
) -> TwoGisScraperProvider:
    async def rules() -> RobotsRules:
        return RobotsRules()

    time = time or Time()
    return TwoGisScraperProvider(
        options=options,
        session_factory=lambda: session,
        robots_loader=rules,
        sleep=time.sleep,
        clock=time.clock,
        notify=(told if told is not None else []).append,
    )


# ---------- turning the mode on ----------


def test_off_by_default() -> None:
    defaults = Settings(_env_file=None)
    assert defaults.SCRAPER_ASSISTED is False
    assert defaults.SCRAPER_PROFILE_DIR == "data/browser_profile"
    assert defaults.SCRAPER_ASSISTED_WAIT_MINUTES == 5
    assert defaults.SCRAPER_ASSISTED_MAX_CAPTCHAS == 2
    assert ScraperOptions.from_settings(defaults).assisted is False


def test_the_setting_alone_changes_nothing_for_the_bot() -> None:
    """The bot calls from_settings(settings): on the server there is no window and nobody to pass a captcha."""
    options = ScraperOptions.from_settings(Settings(_env_file=None, SCRAPER_ASSISTED=True))
    assert options.assisted is False
    assert options.headless is True
    assert options.profile_dir is None
    assert "assisted=" not in (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")


def test_the_mode_shows_the_window_and_keeps_every_limit() -> None:
    current = Settings(_env_file=None, SCRAPER_MIN_DELAY_SECONDS=4, SCRAPER_ASSISTED_WAIT_MINUTES=2)
    normal = ScraperOptions.from_settings(current)
    assisted = ScraperOptions.from_settings(current, assisted=True)

    assert (assisted.assisted, assisted.headless) == (True, False)
    assert assisted.profile_dir == PROJECT_ROOT / "data" / "browser_profile"
    assert assisted.assisted_wait_minutes == 2
    assert assisted.assisted_max_captchas == 2
    assert assisted.min_delay_seconds == normal.min_delay_seconds == 4
    assert assisted.max_delay_seconds == normal.max_delay_seconds
    assert assisted.min_hours_between_runs == normal.min_hours_between_runs
    assert assisted.page_timeout_seconds == normal.page_timeout_seconds


def test_a_run_stays_small_and_settings_can_only_lower_the_limits() -> None:
    assisted = ScraperOptions.from_settings(Settings(_env_file=None), assisted=True)
    assert assisted.max_results_per_run == ASSISTED_MAX_RESULTS_PER_RUN == 60
    assert assisted.max_pages_per_run == ASSISTED_MAX_PAGES_PER_RUN

    high = Settings(_env_file=None, SCRAPER_MAX_RESULTS_PER_RUN=500, SCRAPER_MAX_PAGES_PER_RUN=500)
    assert ScraperOptions.from_settings(high, assisted=True).max_results_per_run == 60
    assert ScraperOptions.from_settings(high, assisted=True).max_pages_per_run == ASSISTED_MAX_PAGES_PER_RUN

    low = Settings(_env_file=None, SCRAPER_MAX_RESULTS_PER_RUN=10, SCRAPER_MAX_PAGES_PER_RUN=15)
    options = ScraperOptions.from_settings(low, assisted=True)
    assert (options.max_results_per_run, options.max_pages_per_run) == (10, 15)


@pytest.mark.parametrize(
    "bad",
    [
        {"SCRAPER_ASSISTED_WAIT_MINUTES": 0},
        {"SCRAPER_ASSISTED_WAIT_MINUTES": 31},
        {"SCRAPER_ASSISTED_MAX_CAPTCHAS": 0},
        {"SCRAPER_ASSISTED_MAX_CAPTCHAS": 6},
    ],
)
def test_limits_of_the_mode_cannot_be_configured_away(bad: dict) -> None:
    with pytest.raises(ValueError, match="SCRAPER_ASSISTED"):
        Settings(_env_file=None, **bad)


def test_the_profile_folder_is_not_committed() -> None:
    assert "data/browser_profile/" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/browser_profile/" in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")


# ---------- waiting for a person ----------


async def test_waits_for_the_person_then_opens_the_page_again_and_goes_on() -> None:
    session, time, told = Session([CAPTCHA, OK, OK], [CAPTCHA, None, PASSED]), Time(), []
    provider = _provider(session, time=time, told=told)

    page = await provider.search(QUERY, AREA)

    assert [place.name for place in page.places] == ["Клиника"]
    assert session.visited == [session.visited[0]] * 2  # the same page, opened again
    assert session.reads == 3  # looks until it is gone
    assert session.fronted == 1
    assert "Пройдите проверку в окне браузера, жду до 5 мин" in told[0]
    assert time.delays.count(CAPTCHA_POLL_SECONDS) == 3
    assert 1 <= CAPTCHA_POLL_SECONDS <= 2

    # The ordinary pause before opening it again, and before the next page as usual.
    assert len(time.page_pauses) == 1
    await provider.search(QUERY, AREA, page=2)
    assert len(time.page_pauses) == 2
    assert all(3 <= delay <= 8 for delay in time.page_pauses)


async def test_a_blank_window_during_navigation_is_not_a_passed_check() -> None:
    blank = PageSnapshot(status=None, final_url="about:blank", title="", html="")
    session = Session([CAPTCHA, OK], [blank, PASSED])
    await _provider(session).search(QUERY, AREA)
    assert session.reads == 2


async def test_timeout_is_a_block_and_the_page_is_not_opened_again() -> None:
    session, time = Session([CAPTCHA], [CAPTCHA]), Time()
    options = replace(OPTIONS, assisted_wait_minutes=1)
    with pytest.raises(ProviderBlockedError, match="не пройдена за 1 мин"):
        await _provider(session, options, time).search(QUERY, AREA)
    assert len(session.visited) == 1
    assert sum(time.delays) == pytest.approx(60, abs=CAPTCHA_POLL_SECONDS)


async def test_closed_window_is_a_block_at_once() -> None:
    session, time = Session([CAPTCHA]), Time()
    session.window_gone = True
    with pytest.raises(ProviderBlockedError, match="окно браузера закрыто"):
        await _provider(session, time=time).search(QUERY, AREA)
    assert session.reads == 1


async def test_captcha_again_right_after_passing_is_a_block_without_asking_again() -> None:
    session, told = Session([CAPTCHA, CAPTCHA, OK]), []
    with pytest.raises(ProviderBlockedError, match="сразу после прохождения"):
        await _provider(session, told=told).search(QUERY, AREA)
    assert session.fronted == 1
    assert len(session.visited) == 2


async def test_no_more_than_the_allowed_captchas_per_run() -> None:
    session, told = Session([CAPTCHA, OK, CAPTCHA, OK, CAPTCHA, OK]), []
    provider = _provider(session, told=told)

    await provider.search(QUERY, AREA, page=1)
    await provider.search(QUERY, AREA, page=2)
    assert session.fronted == 2
    with pytest.raises(ProviderBlockedError, match="запросов к человеку за запуск уже 2"):
        await provider.search(QUERY, AREA, page=3)
    assert session.fronted == 2  # nobody was asked the third time
    assert session.reads == 2


async def test_limit_of_captchas_can_be_lowered_and_starts_over_next_run() -> None:
    options = replace(OPTIONS, assisted_max_captchas=1)
    session = Session([CAPTCHA, OK, CAPTCHA, CAPTCHA, OK])
    provider = _provider(session, options)
    await provider.search(QUERY, AREA, page=1)
    with pytest.raises(ProviderBlockedError):
        await provider.search(QUERY, AREA, page=2)
    await provider.close()
    await provider.search(QUERY, AREA, page=1)
    assert session.fronted == 2


async def test_opening_the_page_again_counts_against_the_page_limit() -> None:
    session = Session([CAPTCHA, OK, OK, OK])
    provider = _provider(session, replace(OPTIONS, max_pages_per_run=3))
    await provider.search(QUERY, AREA, page=1)  # 2 pages
    await provider.search(QUERY, AREA, page=2)  # 3 pages
    with pytest.raises(ProviderBudgetExhausted):
        await provider.search(QUERY, AREA, page=3)
    assert len(session.visited) == 3


async def test_page_limit_reached_at_the_captcha_stops_without_opening_more() -> None:
    session = Session([OK, CAPTCHA, OK])
    provider = _provider(session, replace(OPTIONS, max_pages_per_run=2))
    await provider.search(QUERY, AREA, page=1)
    with pytest.raises(ProviderBudgetExhausted):
        await provider.search(QUERY, AREA, page=2)
    assert len(session.visited) == 2


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (PageSnapshot(status=403, final_url=NORMAL_URL, title="Forbidden", html=""), "403"),
        (PageSnapshot(status=429, final_url=NORMAL_URL, title="Too Many", html=""), "429"),
        (replace(OK, json_bodies=[{"meta": {"code": 403}}]), "отказал"),
    ],
)
async def test_other_refusals_stop_at_once_without_calling_a_person(page: PageSnapshot, expected: str) -> None:
    session, told = Session([page]), []
    with pytest.raises(ProviderBlockedError, match=expected):
        await _provider(session, told=told).search(QUERY, AREA)
    assert told == []
    assert session.reads == 0


async def test_refusal_after_passing_the_captcha_is_still_a_refusal() -> None:
    session = Session([CAPTCHA, PageSnapshot(status=429, final_url=NORMAL_URL, title="", html="")])
    with pytest.raises(ProviderBlockedError, match="429"):
        await _provider(session).search(QUERY, AREA)


async def test_without_the_mode_a_captcha_stops_at_once() -> None:
    session, told = Session([CAPTCHA]), []
    with pytest.raises(ProviderBlockedError, match="капч"):
        await _provider(session, ScraperOptions(), told=told).search(QUERY, AREA)
    assert (session.reads, session.fronted, told) == (0, 0, [])


async def test_captcha_on_a_card_keeps_what_was_read() -> None:
    listing = {
        "result": {
            "total": 2,
            "items": [{"id": "1", "name": "С контактами", "contact_groups": ["x"]}, {"id": "2", "name": "Без"}],
        }
    }
    session = Session([replace(OK, json_bodies=[listing]), CAPTCHA], [CAPTCHA])
    with pytest.raises(ProviderBlockedError, match="не пройдена") as caught:
        await _provider(session, replace(OPTIONS, assisted_wait_minutes=0.1)).search(QUERY, AREA)
    assert [place.name for place in caught.value.partial] == ["С контактами"]


# ---------- the block counter and the pauses ----------


@pytest.fixture
def _no_site_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_analyze(url, client=None):
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)


def _config():
    return load_collector_config(
        Settings(
            _env_file=None,
            BOT_TOKEN="t",
            ADMIN_TG_ID=1,
            COLLECT_PROVIDERS="2gis_scraper",
            COLLECT_QUERIES="dentist_clinic",
            COLLECT_DISTRICTS="Центральный",
            COLLECT_MAX_PAGES=1,
        )
    )


async def _set_state(session_maker, **values) -> None:
    async with session_maker() as session:
        session.add(ProviderState(name="2gis_scraper", **values))
        await session.commit()


async def _state(session_maker) -> ProviderState | None:
    async with session_maker() as session:
        return (await get_provider_states(session)).get("2gis_scraper")


@pytest.mark.usefixtures("_no_site_checks")
async def test_passed_captcha_neither_adds_a_block_nor_clears_earlier_ones(session_maker) -> None:
    await _set_state(session_maker, consecutive_blocks=2, last_block_reason="капча")
    session = Session([CAPTCHA, OK])

    result = await collect_leads(session_maker, [_provider(session)], _config())

    assert result.errors == 0
    assert result.found == 1
    assert session.closed == 1
    state = await _state(session_maker)
    assert state.consecutive_blocks == 2
    assert state.disabled_at is None


@pytest.mark.usefixtures("_no_site_checks")
async def test_passed_captcha_with_no_earlier_blocks_leaves_the_counter_at_zero(session_maker) -> None:
    await collect_leads(session_maker, [_provider(Session([CAPTCHA, OK]))], _config())
    assert (await _state(session_maker)).consecutive_blocks == 0


@pytest.mark.usefixtures("_no_site_checks")
async def test_run_without_a_captcha_clears_the_counter_as_usual(session_maker) -> None:
    await _set_state(session_maker, consecutive_blocks=2)
    await collect_leads(session_maker, [_provider(Session([OK]))], _config())
    assert (await _state(session_maker)).consecutive_blocks == 0


@pytest.mark.usefixtures("_no_site_checks")
@pytest.mark.parametrize(
    ("pages", "shown", "reason"),
    [
        ([CAPTCHA], [CAPTCHA], "не пройдена"),
        ([CAPTCHA, CAPTCHA], [PASSED], "сразу после прохождения"),
    ],
)
async def test_failure_adds_a_block_and_a_pause(session_maker, pages, shown, reason) -> None:
    await _set_state(session_maker, consecutive_blocks=1)
    result = await collect_leads(session_maker, [_provider(Session(pages, shown))], _config())

    state = await _state(session_maker)
    assert state.consecutive_blocks == 2
    assert reason in state.last_block_reason
    assert state.blocked_until > datetime.now(UTC) + timedelta(hours=11)  # 6 hours, doubled
    assert result.alerts


@pytest.mark.usefixtures("_no_site_checks")
async def test_third_block_in_a_row_switches_the_source_off(session_maker) -> None:
    await _set_state(session_maker, consecutive_blocks=MAX_CONSECUTIVE_BLOCKS - 1)
    await collect_leads(session_maker, [_provider(Session([CAPTCHA], [CAPTCHA]))], _config())
    assert (await _state(session_maker)).disabled_at is not None


@pytest.mark.usefixtures("_no_site_checks")
async def test_the_mode_does_not_get_around_a_pause_in_force(session_maker) -> None:
    until = datetime.now(UTC) + timedelta(hours=3)
    await _set_state(session_maker, consecutive_blocks=1, blocked_until=until, last_block_reason="капча")
    session = Session([OK])

    result = await collect_leads(session_maker, [_provider(session)], _config())

    assert session.visited == []
    assert "пауза после блокировки" in result.skipped["2gis_scraper"]
    state = await _state(session_maker)
    assert state.consecutive_blocks == 1
    assert state.blocked_until is not None


@pytest.mark.usefixtures("_no_site_checks")
async def test_the_mode_does_not_get_around_a_switched_off_source(session_maker) -> None:
    await _set_state(session_maker, consecutive_blocks=3, disabled_at=datetime.now(UTC), last_block_reason="капча")
    session = Session([OK])
    result = await collect_leads(session_maker, [_provider(session)], _config())
    assert session.visited == []
    assert "отключён" in result.skipped["2gis_scraper"]


@pytest.mark.usefixtures("_no_site_checks")
async def test_the_mode_keeps_the_interval_between_runs(session_maker) -> None:
    await _set_state(session_maker, last_run_at=datetime.now(UTC) - timedelta(hours=1))
    session = Session([OK])
    result = await collect_leads(session_maker, [_provider(session)], _config())
    assert session.visited == []
    assert result.skipped["2gis_scraper"] == REST_REASON


# ---------- the real session class with Playwright mocked ----------


def _playwright_mock(existing_pages: int = 1) -> tuple[MagicMock, MagicMock, MagicMock]:
    page = MagicMock()
    page.close = AsyncMock()
    page.bring_to_front = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    context = MagicMock()
    context.pages = [page] * existing_pages
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock()
    playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
    playwright.stop = AsyncMock()
    return playwright, context, page


def _start_returning(playwright: MagicMock) -> MagicMock:
    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)
    return MagicMock(return_value=starter)


async def test_browser_gets_a_window_a_profile_and_nothing_else(tmp_path: Path) -> None:
    playwright, context, page = _playwright_mock()
    profile = tmp_path / "data" / "browser_profile"
    session = PlaywrightSession(headless=False, profile_dir=profile)
    with patch("playwright.async_api.async_playwright", _start_returning(playwright)):
        await session.start()

    # The whole list of parameters: no args, no ignore_default_args, no user_agent, no extra headers.
    playwright.chromium.launch_persistent_context.assert_awaited_once_with(str(profile), headless=False, locale="ru-RU")
    playwright.chromium.launch.assert_not_called()
    context.add_init_script.assert_not_called()
    context.set_extra_http_headers.assert_not_called()
    assert profile.is_dir()
    context.new_page.assert_not_awaited()  # uses the window the browser opened

    await session.close()
    context.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    page.close.assert_awaited_once()


async def test_new_page_is_opened_if_the_browser_opened_none(tmp_path: Path) -> None:
    playwright, context, _page = _playwright_mock(existing_pages=0)
    session = PlaywrightSession(headless=False, profile_dir=tmp_path / "profile")
    with patch("playwright.async_api.async_playwright", _start_returning(playwright)):
        await session.start()
    context.new_page.assert_awaited_once()


async def test_provider_passes_the_profile_to_the_browser(tmp_path: Path) -> None:
    provider = TwoGisScraperProvider(options=replace(OPTIONS, profile_dir=tmp_path))
    session = provider._session_factory()
    assert isinstance(session, PlaywrightSession)
    assert (session._headless, session._profile_dir) == (False, tmp_path)


async def test_browser_that_cannot_use_the_profile_explains_why(tmp_path: Path) -> None:
    from playwright.async_api import Error as PlaywrightError

    playwright, _context, _page = _playwright_mock()
    playwright.chromium.launch_persistent_context.side_effect = PlaywrightError("profile is locked")
    session = PlaywrightSession(headless=False, profile_dir=tmp_path)
    with (
        patch("playwright.async_api.async_playwright", _start_returning(playwright)),
        pytest.raises(ProviderUnavailableError, match="другой запущенный сборщик"),
    ):
        await session.start()
    playwright.stop.assert_awaited_once()


async def _started(tmp_path: Path) -> tuple[PlaywrightSession, MagicMock]:
    playwright, _context, page = _playwright_mock()
    session = PlaywrightSession(headless=False, profile_dir=tmp_path)
    with patch("playwright.async_api.async_playwright", _start_returning(playwright)):
        await session.start()
    return session, page


async def test_reading_the_window_only_reads(tmp_path: Path) -> None:
    session, page = await _started(tmp_path)
    page.url = NORMAL_URL
    page.title = AsyncMock(return_value="Заголовок")
    page.content = AsyncMock(return_value="<html></html>")
    page.goto = AsyncMock()

    shown = await session.read_page()

    assert (shown.final_url, shown.title, shown.html) == (NORMAL_URL, "Заголовок", "<html></html>")
    page.goto.assert_not_called()
    page.click.assert_not_called()
    page.mouse.move.assert_not_called()


async def test_window_being_navigated_is_read_later(tmp_path: Path) -> None:
    from playwright.async_api import Error as PlaywrightError

    session, page = await _started(tmp_path)
    page.title = AsyncMock(side_effect=PlaywrightError("Execution context was destroyed"))
    assert await session.read_page() is None


async def test_stuck_window_does_not_hang_the_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_module, "READ_PAGE_TIMEOUT_SECONDS", 0.01)
    session, page = await _started(tmp_path)

    async def forever() -> str:
        await asyncio.sleep(3600)
        return ""

    page.title = forever
    assert await session.read_page() is None


async def test_closed_window_is_reported(tmp_path: Path) -> None:
    session, page = await _started(tmp_path)
    page.is_closed.return_value = True
    with pytest.raises(ProviderUnavailableError):
        await session.read_page()


async def test_window_that_cannot_come_forward_is_not_a_failure(tmp_path: Path) -> None:
    from playwright.async_api import Error as PlaywrightError

    session, page = await _started(tmp_path)
    await session.bring_to_front()
    page.bring_to_front.assert_awaited_once()
    page.bring_to_front.side_effect = PlaywrightError("no")
    await session.bring_to_front()


@pytest.mark.parametrize("module", [browser_module, TwoGisScraperProvider])
def test_nothing_touches_the_captcha_or_hides_automation(module: object) -> None:
    source = inspect.getsource(inspect.getmodule(module)).lower()
    for marker in (
        ".click(",
        ".mouse",
        ".keyboard",
        ".frame_locator",
        ".locator(",
        "add_init_script",
        "automationcontrolled",
        "ignore_default_args",
        "set_extra_http_headers",
        "user_agent",
        "stealth",
        "navigator",
        "window.chrome",
        "anticaptcha",
        "2captcha",
        "args=",
    ):
        assert marker not in source, marker
    # The one exception: the browser reads the variables of a page, and the script does nothing else.
    assert source.count(".evaluate(") == (1 if module is browser_module else 0)
    for action in ("dispatchevent", "fetch(", "document.", "xmlhttprequest", "localstorage", "cookie", "=>  {"):
        assert action not in browser_module._READ_STATE_SCRIPT.lower()
