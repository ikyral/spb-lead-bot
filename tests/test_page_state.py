"""Tests for reading organizations from the state of a page, and for what is not a block: format and network."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from src import collector_cli
from src.config import PROJECT_ROOT, Settings
from src.database.models import ProviderState
from src.database.repo import get_collector_state, get_provider_states
from src.services import pipeline
from src.services.api_client import (
    NO_CONNECTION_TEXT,
    ProviderFormatError,
    ProviderNoConnectionError,
    ProviderTemporaryError,
)
from src.services.assisted_runs import EnrichRun, run_check, run_enrich
from src.services.collector import SearchTarget, collect_leads, load_collector_config
from src.services.lead_file import (
    KIND_ENRICH_RESULT,
    EnrichRequest,
    EnrichRequestItem,
    build_enrich_document,
    parse_enrich_result,
)
from src.services.providers.base import SearchArea, SearchQuery
from src.services.providers.twogis import parse_catalog_items
from src.services.providers.twogis_scraper import (
    MAX_UNRECOGNIZED_IN_A_ROW,
    ScraperOptions,
    TwoGisScraperProvider,
    read_card_contacts,
)
from src.services.scraper import twogis_site
from src.services.scraper.browser import PageSnapshot, PlaywrightSession
from src.services.scraper.diagnostics import state_outline
from src.services.scraper.robots import (
    ROBOTS_RETRY_PAUSES_SECONDS,
    RobotsNoConnectionError,
    RobotsRules,
    RobotsUnavailableError,
    fetch_robots,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIRM = "70000001018420391"
CARD_URL = f"https://2gis.ru/spb/firm/{FIRM}"
SEARCH_URL = "https://2gis.ru/spb/search/x"
QUERY = SearchQuery(text="стоматология", category="dentist_clinic", high_margin=True)
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Центральный")
TARGET = SearchTarget(QUERY, AREA)
OPTIONS = ScraperOptions(assisted=True, headless=False, profile_dir=Path("profile"))
FORMAT_TEXT = "Формат страницы не распознан: нужна правка парсера, это не блокировка"
CAPTCHA_HTML = (FIXTURES / "twogis_captcha.html").read_text(encoding="utf-8")
CAPTCHA = PageSnapshot(
    status=200, final_url="https://captcha.2gis.ru/form?return_url=x", title="2GIS Captcha", html=CAPTCHA_HTML
)


def _state(name: str = "card") -> dict:
    return json.loads((FIXTURES / f"twogis_state_{name}.json").read_text(encoding="utf-8"))


def _card_page(state: dict | None = None, firm: str = FIRM, **overrides) -> PageSnapshot:
    values = {
        "status": 200,
        "final_url": f"https://2gis.ru/spb/firm/{firm}",
        "title": "Улыбка, стоматология",
        "html": "<html></html>",
        "page_state": {"initialState": state if state is not None else _state()},
    }
    values.update(overrides)
    return PageSnapshot(**values)


def _no_data_page(**overrides) -> PageSnapshot:
    values = {"status": 200, "final_url": CARD_URL, "title": "Организация", "html": "<html></html>"}
    values.update(overrides)
    return PageSnapshot(**values)


def _contacts_state(*contacts: dict) -> dict:
    state = _state()
    state["data"]["entity"]["profile"][FIRM]["data"]["contact_groups"] = [{"contacts": list(contacts)}]
    return state


def _as_inline(state: dict) -> str:
    """As a server writes it: JSON inside a string of a script."""
    text = json.dumps(state, ensure_ascii=False).replace("\\", "\\\\").replace("'", "\\'")
    return f"<script>var initialState = JSON.parse('{text}'); var __REACT_QUERY_STATE__ = JSON.parse('{{}}');</script>"


class Session:
    def __init__(self, pages: list) -> None:
        self.pages = list(pages)
        self.visited: list[str] = []
        self.closed = 0

    async def start(self) -> None:
        return None

    async def visit(self, url: str, wants_body) -> PageSnapshot:
        self.visited.append(url)
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page

    async def read_page(self) -> PageSnapshot | None:
        return CAPTCHA

    async def bring_to_front(self) -> None:
        return None

    async def close(self) -> None:
        self.closed += 1


class Time:
    def __init__(self) -> None:
        self.now = 0.0

    async def sleep(self, delay: float) -> None:
        self.now += delay

    def clock(self) -> float:
        return self.now


def _provider(session: Session, options: ScraperOptions = OPTIONS, robots=None) -> TwoGisScraperProvider:
    async def rules() -> RobotsRules:
        return RobotsRules()

    time = Time()
    return TwoGisScraperProvider(
        options=options,
        session_factory=lambda: session,
        robots_loader=robots or rules,
        sleep=time.sleep,
        clock=time.clock,
        notify=lambda _text: None,
    )


def _item(number: int = 1) -> EnrichRequestItem:
    firm = f"7000{number}"
    return EnrichRequestItem("2gis", firm, f"https://2gis.ru/spb/firm/{firm}", firm)


def _firm_state(number: int = 1) -> dict:
    state = _state()
    profile = state["data"]["entity"]["profile"]
    entry = profile.pop(FIRM)
    entry["data"]["id"] = f"7000{number}"
    profile[f"7000{number}"] = entry
    return state


async def _enrich(session_maker, session: Session, items: list, export_dir: Path, **kwargs) -> EnrichRun:
    run = EnrichRun()
    request = EnrichRequest(total=len(items), items=items)
    await run_enrich(session_maker, _provider(session, **kwargs), request, export_dir, 6.0, run)
    return run


async def _scraper_state(session_maker) -> ProviderState | None:
    async with session_maker() as session:
        return (await get_provider_states(session)).get("2gis_scraper")


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


@pytest.fixture
def _no_site_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def analyze(url, client=None):
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", analyze)


# ---------- reading the state ----------


def test_a_card_is_found_in_the_state_of_its_page() -> None:
    card = twogis_site.read_card_page(_card_page(), FIRM)

    assert card is not None
    assert card["name"] == "Улыбка, стоматология"
    contacts = read_card_contacts(card)
    assert contacts.phones == ["+79210000001"]
    assert contacts.email == "info@smile.example"
    assert contacts.socials[0] == "https://vk.com/smile"
    assert len(contacts.socials) == 2


def test_the_fields_of_a_card_go_the_usual_way_into_a_place() -> None:
    card = twogis_site.read_card_page(_card_page(), FIRM)

    place = parse_catalog_items({"result": {"items": [card]}}, "Центральный", "dentist_clinic", True)[0]

    assert place.name == "Улыбка, стоматология"
    assert place.address == "Невский проспект, 10"
    assert place.source_url == CARD_URL
    assert place.phone == "+79210000001"
    assert place.raw_website == "smile.example"
    assert place.rubric == "Стоматологии"


def test_the_owner_of_the_browser_is_never_read() -> None:
    card = twogis_site.read_card_page(_card_page(), FIRM)

    assert "+70000000000" not in json.dumps(card, ensure_ascii=False)
    assert "Владелец" not in json.dumps(card, ensure_ascii=False)


def test_another_organization_in_the_state_is_not_this_card() -> None:
    assert twogis_site.read_card_page(_card_page(), "70000000000000000") is None


def test_a_site_comes_from_the_text_next_to_a_link_through_2gis() -> None:
    assert read_card_contacts(twogis_site.read_card_page(_card_page(), FIRM)).website == "smile.example"


def test_a_direct_address_of_a_site_is_taken_as_it_is() -> None:
    state = _contacts_state({"type": "website", "value": "https://direct.example/", "text": "Сайт"})

    assert read_card_contacts(twogis_site.read_card_page(_card_page(state), FIRM)).website == "https://direct.example/"


@pytest.mark.parametrize("text", ["Сайт компании", "", None, "https://2gis.ru/spb/firm/1"])
def test_a_site_known_only_through_2gis_is_not_a_card_without_a_site(text: str | None) -> None:
    state = _contacts_state(
        {"type": "phone", "value": "+79210000001"},
        {"type": "website", "value": "http://link.2gis.ru/1.2/A", "url": "http://link.2gis.ru/1.2/A", "text": text},
    )

    assert read_card_contacts(twogis_site.read_card_page(_card_page(state), FIRM)) is None


def test_a_link_through_2gis_never_becomes_a_site_of_a_place() -> None:
    state = _contacts_state({"type": "website", "value": "http://link.2gis.ru/1.2/AAAA", "text": "Сайт"})
    card = twogis_site.read_card_page(_card_page(state), FIRM)

    place = parse_catalog_items({"result": {"items": [card]}}, None, "dentist_clinic", False)[0]

    assert place.raw_website is None


def test_a_card_with_contacts_but_no_site_says_that() -> None:
    contacts = read_card_contacts(
        twogis_site.read_card_page(_card_page(_contacts_state({"type": "phone", "value": "+79210000001"})), FIRM)
    )

    assert contacts.website is None
    assert contacts.socials == []
    assert contacts.email is None
    assert contacts.phones == ["+79210000001"]


def test_a_card_in_the_state_without_contacts_is_not_a_card_without_a_site() -> None:
    state = _state()
    del state["data"]["entity"]["profile"][FIRM]["data"]["contact_groups"]

    assert read_card_contacts(twogis_site.read_card_page(_card_page(state), FIRM)) is None


def test_the_state_is_preferred_to_separate_responses() -> None:
    body = {"result": {"items": [{"id": FIRM, "name": "Из ответа", "contact_groups": []}]}}
    page = _card_page(json_bodies=[body], json_urls=["https://catalog.api.2gis.ru/3.0/items/byid"])

    assert twogis_site.read_card_page(page, FIRM)["name"] == "Улыбка, стоматология"


# ---------- the text of a page ----------


def test_the_state_is_read_from_the_text_of_a_page_if_the_page_does_not_give_it() -> None:
    state = _state()
    state["data"]["entity"]["profile"][FIRM]["data"]["name"] = 'О\'Хара "Улыбка" \\ \n ё'
    page = _no_data_page(html=_as_inline(state))

    card = twogis_site.read_card_page(page, FIRM)

    assert card["name"] == 'О\'Хара "Улыбка" \\ \n ё'
    assert read_card_contacts(card).phones == ["+79210000001"]


def test_escapes_of_a_script_are_understood() -> None:
    html = r"""<script>var initialState = JSON.parse('{"a":"\u0410\x42\'\\\\","b":"\\u0411"}');</script>"""

    assert twogis_site.inline_state(html) == {"a": "АB'\\", "b": "Б"}


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<script>var other = 1;</script>",
        "var initialState = JSON.parse('{broken');",
        "var initialState = JSON.parse('{",
    ],
)
def test_a_text_without_a_usable_state_gives_nothing(html: str) -> None:
    assert twogis_site.inline_state(html) is None


# ---------- the links of a page ----------


def _links_page(*hrefs: str, url: str = CARD_URL) -> PageSnapshot:
    return _no_data_page(final_url=url, html="".join(f'<a class="x" href="{href}">y</a>' for href in hrefs))


def test_links_give_a_phone_an_email_socials_and_a_site() -> None:
    page = _links_page(
        "tel:+79210000001",
        "mailto:info@smile.example?subject=x",
        "https://vk.com/smile",
        "https://smile.example/",
        "https://link.2gis.ru/1.2/AAAA",
        "https://law.2gis.ru/x",
        "https://2gis.onelink.me/x",
        "https://otello.ru/",
    )

    contacts = read_card_contacts(twogis_site.read_card_page(page, FIRM))

    assert contacts.phones == ["+79210000001"]
    assert contacts.email == "info@smile.example"
    assert contacts.socials == ["https://vk.com/smile"]
    assert contacts.website == "https://smile.example/"


def test_links_cannot_say_that_there_is_no_site() -> None:
    page = _links_page("tel:+79210000001", "https://law.2gis.ru/x")

    assert read_card_contacts(twogis_site.read_card_page(page, FIRM)) is None


def test_several_outside_links_are_not_guessed_between() -> None:
    page = _links_page("tel:+79210000001", "https://one.example/", "https://two.example/")

    assert read_card_contacts(twogis_site.read_card_page(page, FIRM)) is None


def test_links_of_another_page_are_not_a_card() -> None:
    assert twogis_site.read_card_page(_links_page("tel:+79210000001", url=SEARCH_URL), FIRM) is None


# ---------- a search ----------


def _search_of_one() -> dict:
    state = _state("search")
    state["data"]["search"]["pagination"]["search-1"]["1"]["data"] = [FIRM]
    return state


def _search_page(state: dict | None = None) -> PageSnapshot:
    state = state if state is not None else _state("search")
    return PageSnapshot(status=200, final_url=SEARCH_URL, title="Поиск", html="", page_state={"initialState": state})


def test_a_search_gives_what_it_found_in_its_order_and_nothing_else() -> None:
    data = twogis_site.read_search_page(_search_page())

    assert [item["name"] for item in data.items] == ["Дент Про", "Улыбка, стоматология"]
    assert data.total == 25


def test_a_search_with_nothing_found_is_a_normal_result() -> None:
    state = _state("search")
    state["data"]["search"]["profile"]["search-1"]["data"]["total"] = 0
    state["data"]["search"]["pagination"] = {}

    data = twogis_site.read_search_page(_search_page(state))

    assert data.items == []
    assert data.total == 0


def test_a_page_without_a_search_is_not_read_as_one() -> None:
    assert twogis_site.read_search_page(_search_page(_state())) is None


@pytest.mark.usefixtures("_no_site_checks")
async def test_collection_opens_a_card_of_each_organization_and_gets_links_to_them() -> None:
    second = copy.deepcopy(_state())
    profile = second["data"]["entity"]["profile"]
    entry = profile.pop(FIRM)
    entry["data"]["id"] = "70000001018420392"
    entry["data"]["name"] = "Дент Про"
    profile["70000001018420392"] = entry
    session = Session([_search_page(), _card_page(second, firm="70000001018420392"), _card_page()])

    page = await _provider(session).search(QUERY, AREA)

    assert session.visited[1:] == ["https://2gis.ru/spb/firm/70000001018420392", CARD_URL]
    assert [place.source_url for place in page.places] == ["https://2gis.ru/spb/firm/70000001018420392", CARD_URL]
    assert all(place.phone == "+79210000001" and place.contacts_known for place in page.places)
    assert page.has_more is True


# ---------- not a block: a format the code does not understand ----------


@pytest.mark.usefixtures("_no_site_checks")
async def test_an_unknown_search_page_is_not_a_block_and_keeps_the_interval_free(session_maker) -> None:
    session = Session([_no_data_page(final_url=SEARCH_URL)])

    result = await collect_leads(session_maker, [_provider(session)], _config())

    assert FORMAT_TEXT in result.alerts
    state = await _scraper_state(session_maker)
    assert state.consecutive_blocks == 0
    assert state.blocked_until is None
    assert state.last_run_at is None
    assert session.closed == 1


@pytest.mark.usefixtures("_no_site_checks")
async def test_the_previous_time_of_a_run_comes_back(session_maker) -> None:
    earlier = datetime.now(UTC) - timedelta(days=2)
    async with session_maker() as session_db:
        session_db.add(ProviderState(name="2gis_scraper", consecutive_blocks=1, last_run_at=earlier))
        await session_db.commit()

    await collect_leads(session_maker, [_provider(Session([_no_data_page(final_url=SEARCH_URL)]))], _config())

    state = await _scraper_state(session_maker)
    assert state.last_run_at == earlier
    assert state.consecutive_blocks == 1  # and it does not reset the counter either


async def test_unknown_cards_in_a_list_are_skipped_then_stop_the_run() -> None:
    state = _state("search")
    ids = [str(number) for number in range(1, MAX_UNRECOGNIZED_IN_A_ROW + 3)]
    state["data"]["search"]["pagination"]["search-1"]["1"]["data"] = ids
    for firm in ids:
        state["data"]["entity"]["profile"][firm] = {"data": {"id": firm, "type": "branch", "name": f"Орг {firm}"}}
    session = Session([_search_page(state), *[_no_data_page() for _ in ids]])

    with pytest.raises(ProviderFormatError, match="не блокировка"):
        await _provider(session).search(QUERY, AREA)

    assert len(session.visited) == 1 + MAX_UNRECOGNIZED_IN_A_ROW


async def test_a_recognized_card_interrupts_a_row_of_unknown_ones(session_maker, tmp_path: Path) -> None:
    session = Session(
        [_no_data_page(), _no_data_page(), _card_page(_firm_state(3), firm="70003"), _no_data_page(), _no_data_page()]
    )

    run = await _enrich(session_maker, session, [_item(number) for number in range(1, 6)], tmp_path)

    assert len(session.visited) == 5
    assert run.failed is None
    assert run.read == 1
    assert (await _scraper_state(session_maker)).last_run_at is not None


async def test_the_interval_is_taken_once_a_card_is_read(session_maker, tmp_path: Path) -> None:
    run = await _enrich(session_maker, Session([_card_page(_firm_state(), firm="70001")]), [_item()], tmp_path)

    assert run.read == 1
    assert (await _scraper_state(session_maker)).last_run_at is not None


async def test_a_run_killed_on_the_way_stays_counted(session_maker, tmp_path: Path) -> None:
    import asyncio

    run = EnrichRun()
    session = Session([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await run_enrich(session_maker, _provider(session), EnrichRequest(1, [_item()]), tmp_path, 6.0, run)

    assert (await _scraper_state(session_maker)).last_run_at is not None


async def test_a_format_stop_from_the_command_line_is_an_error_with_the_message(session_maker, tmp_path: Path) -> None:
    items = [_item(number) for number in range(1, 5)]
    run = await _enrich(session_maker, Session([_no_data_page() for _ in items]), items, tmp_path)

    collector_cli._print_enrich(run)

    assert run.failed == FORMAT_TEXT


# ---------- still a block ----------


async def test_a_real_captcha_nobody_passes_is_a_block(session_maker, tmp_path: Path) -> None:
    run = await _enrich(session_maker, Session([CAPTCHA]), [_item()], tmp_path)

    assert "капча" in run.blocked
    state = await _scraper_state(session_maker)
    assert state.consecutive_blocks == 1
    assert state.blocked_until is not None
    assert state.last_run_at is not None


@pytest.mark.parametrize("status", [403, 429, 401])
async def test_a_refusal_is_a_block(session_maker, tmp_path: Path, status: int) -> None:
    run = await _enrich(session_maker, Session([_no_data_page(status=status)]), [_item()], tmp_path)

    assert str(status) in run.blocked
    state = await _scraper_state(session_maker)
    assert state.consecutive_blocks == 1
    assert state.last_run_at is not None


async def test_an_empty_page_is_a_block_and_not_a_format(session_maker, tmp_path: Path) -> None:
    run = await _enrich(session_maker, Session([_no_data_page(title="  ")]), [_item()], tmp_path)

    assert "пустой" in run.blocked
    assert (await _scraper_state(session_maker)).consecutive_blocks == 1


async def test_a_server_error_is_not_a_format_and_takes_the_interval(session_maker, tmp_path: Path) -> None:
    run = await _enrich(session_maker, Session([_no_data_page(status=502)]), [_item()], tmp_path)

    assert run.records[0]["parsed"] is False
    state = await _scraper_state(session_maker)
    assert state.consecutive_blocks == 0
    assert state.last_run_at is not None


# ---------- not a block: no connection ----------


def _robots_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_robots_is_tried_three_times_with_pauses() -> None:
    calls: list[int] = []
    pauses: list[float] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("no")
        return httpx.Response(200, text="User-agent: *\nDisallow: /x\n")

    async def pause(delay: float) -> None:
        pauses.append(delay)

    rules = await fetch_robots("https://x.example", "a", _robots_client(flaky), sleep=pause)

    assert not rules.is_allowed("https://x.example/x")
    assert len(calls) == 3
    assert pauses == [5.0, 15.0] == list(ROBOTS_RETRY_PAUSES_SECONDS)


async def test_robots_gives_up_after_three_attempts() -> None:
    calls: list[int] = []

    def fail(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("no")

    with pytest.raises(RobotsNoConnectionError):
        await fetch_robots("https://x.example", "a", _robots_client(fail), sleep=AsyncMock())

    assert len(calls) == 3


async def test_an_answer_of_the_site_is_not_tried_again() -> None:
    calls: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(RobotsUnavailableError) as error:
        await fetch_robots("https://x.example", "a", _robots_client(answer), sleep=AsyncMock())

    assert len(calls) == 1
    assert not isinstance(error.value, RobotsNoConnectionError)


async def _no_robots() -> RobotsRules:
    raise RobotsNoConnectionError("сетевая ошибка (ConnectTimeout)")


async def test_no_connection_to_robots_leaves_no_visit_during_enrich(session_maker, tmp_path: Path) -> None:
    session = Session([])

    run = await _enrich(session_maker, session, [_item()], tmp_path, robots=_no_robots)

    assert run.failed == NO_CONNECTION_TEXT
    assert run.blocked is None
    assert session.visited == []
    state = await _scraper_state(session_maker)
    assert state.last_run_at is None
    assert state.consecutive_blocks == 0
    assert state.blocked_until is None


async def test_no_answer_to_a_page_leaves_no_visit_during_enrich(session_maker, tmp_path: Path) -> None:
    session = Session([ProviderNoConnectionError(NO_CONNECTION_TEXT)])

    run = await _enrich(session_maker, session, [_item(), _item(2)], tmp_path)

    assert run.failed == NO_CONNECTION_TEXT
    assert len(session.visited) == 1
    assert (await _scraper_state(session_maker)).last_run_at is None


async def test_losing_the_connection_after_a_read_card_keeps_it_and_the_visit(session_maker, tmp_path: Path) -> None:
    pages = [_card_page(_firm_state(), firm="70001"), ProviderNoConnectionError(NO_CONNECTION_TEXT)]

    run = await _enrich(session_maker, Session(pages), [_item(), _item(2)], tmp_path)

    assert run.read == 1
    assert run.path is not None
    assert (await _scraper_state(session_maker)).last_run_at is not None


@pytest.mark.usefixtures("_no_site_checks")
async def test_no_connection_leaves_no_visit_during_collection(session_maker) -> None:
    result = await collect_leads(session_maker, [_provider(Session([]), robots=_no_robots)], _config())

    assert NO_CONNECTION_TEXT in result.alerts
    state = await _scraper_state(session_maker)
    assert state.last_run_at is None
    assert state.consecutive_blocks == 0


async def test_a_check_without_connection_gives_its_run_back(session_maker, tmp_path: Path) -> None:
    report = await run_check(
        session_maker, _provider(Session([]), robots=_no_robots), TARGET, tmp_path / "c.json", 6.0, "spb"
    )

    assert report.failed == NO_CONNECTION_TEXT
    assert report.runs_left == 3
    async with session_maker() as session:
        assert (await get_collector_state(session)).scraper_check_runs is None


def _browser_page(goto) -> tuple[PlaywrightSession, dict]:
    page = MagicMock()
    handlers: dict = {}
    page.on = lambda event, handler: handlers.setdefault(event, handler)
    page.remove_listener = lambda event, handler: handlers.pop(event)
    page.goto = goto
    session = PlaywrightSession()
    session._page = page
    return session, handlers


@pytest.mark.parametrize("message", ["net::ERR_NAME_NOT_RESOLVED at https://x/?key=SECRET", "net::ERR_TIMED_OUT"])
async def test_a_network_error_with_no_answer_is_no_connection(message: str) -> None:
    from playwright.async_api import Error as PlaywrightError

    session, _ = _browser_page(AsyncMock(side_effect=PlaywrightError(message)))

    with pytest.raises(ProviderNoConnectionError) as error:
        await session.visit("https://x/", lambda _url: False)

    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("message", ["Target page has been closed", "net::ERR_ABORTED"])
async def test_a_closed_window_is_not_a_problem_of_the_network(message: str) -> None:
    from playwright.async_api import Error as PlaywrightError

    session, _ = _browser_page(AsyncMock(side_effect=PlaywrightError(message)))

    with pytest.raises(ProviderTemporaryError):
        await session.visit("https://x/", lambda _url: False)


async def test_a_timeout_after_an_answer_is_not_no_connection() -> None:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    holder: dict = {}

    async def goto(url, **kwargs):
        response = MagicMock(url="https://x/style.css")
        holder["handlers"]["response"](response)
        raise PlaywrightTimeoutError("timeout")

    session, holder["handlers"] = _browser_page(goto)

    with pytest.raises(ProviderTemporaryError):
        await session.visit("https://x/", lambda _url: False)


# ---------- reading the variables ----------


def _reading_page(answer) -> PlaywrightSession:
    async def goto(url, **kwargs):
        return MagicMock(status=200)

    session, _ = _browser_page(goto)
    page = session._page
    page.url = "https://x/"
    page.title = AsyncMock(return_value="t")
    page.content = AsyncMock(return_value="<html></html>")
    page.wait_for_load_state = AsyncMock()
    page.evaluate = answer
    return session


async def test_a_page_gives_its_variables() -> None:
    answers = [{"json": '{"a": 1}'}, {"problem": "значение не сериализуется в JSON"}]
    session = _reading_page(AsyncMock(return_value=answers))

    snapshot = await session.visit("https://x/", lambda _url: False)

    assert snapshot.page_state == {"initialState": {"a": 1}}
    assert snapshot.page_state_problems == {"__REACT_QUERY_STATE__": "значение не сериализуется в JSON"}


async def test_a_page_that_does_not_give_its_variables_is_still_a_page() -> None:
    from playwright.async_api import Error as PlaywrightError

    session = _reading_page(AsyncMock(side_effect=PlaywrightError("Execution context was destroyed")))

    snapshot = await session.visit("https://x/", lambda _url: False)

    assert snapshot.page_state == {}
    assert "Error" in snapshot.page_state_problems["initialState"]
    assert snapshot.html == "<html></html>"


# ---------- --save-sample and the map of the state ----------


def test_the_map_has_paths_and_counts_but_no_values() -> None:
    outline = state_outline(_state())
    dumped = json.dumps(outline, ensure_ascii=False)

    for secret in ("Улыбка", "+7921", "smile.example", "+70000000000", "Невский", FIRM):
        assert secret not in dumped
    paths = {entry["path"]: entry for entry in outline["paths"]}
    groups = paths["$.data.entity.profile.<ключ>.data.contact_groups[].contacts"]
    assert groups["types"] == ["array"]
    assert groups["max_length"] == 5
    hits = {hit["path"]: hit["count"] for hit in outline["key_hits"]}
    assert hits["$.data.entity.profile.<ключ>.data.contact_groups"] == 1
    assert hits["$.data.entity.profile.<ключ>.data.contact_groups[].contacts"] == 1
    assert hits["$.data.entity.profile.<ключ>.data.contact_groups[].contacts[].url"] == 3
    assert hits["$.data.user.phone"] == 1


def test_the_map_is_bounded() -> None:
    deep: dict = {}
    current = deep
    for _ in range(100):
        current["next"] = {}
        current = current["next"]
    wide = {f"field{'a' * (number % 50)}{chr(97 + number % 26)}": {"x": number} for number in range(20000)}

    assert state_outline(deep)["truncated"] is True
    assert len(state_outline(deep)["paths"]) < 30
    assert len(state_outline(wide)["paths"]) <= 5000


async def test_a_sample_is_saved_only_when_asked(session_maker, tmp_path: Path) -> None:
    pages = [_search_page(_search_of_one()), _card_page(html="<html>карточка</html>")]

    report = await run_check(
        session_maker,
        _provider(Session(pages), ScraperOptions(assisted=True, min_hours_between_runs=0.0, max_pages_per_run=2)),
        TARGET,
        tmp_path / "logs" / "c.json",
        6.0,
        "spb",
        sample_dir=tmp_path / "samples",
    )

    names = sorted(path.name for path in (tmp_path / "samples").iterdir())
    assert len(names) == 4
    kinds = [name.split("_", 2)[-1] for name in names]
    assert kinds == ["card.html", "card_state.json", "search.html", "search_state.json"]
    assert report.sample_files
    card_state = next(path for path in report.sample_files if path.name.endswith("card_state.json"))
    assert json.loads(card_state.read_text(encoding="utf-8"))["values"]["initialState"]["data"]["entity"]
    saved = json.loads((tmp_path / "logs" / "c.json").read_text(encoding="utf-8"))
    assert report.recognized["name"] == "Улыбка, стоматология"
    assert "+7921" not in json.dumps(saved["pages"], ensure_ascii=False)
    assert saved["pages"][1]["page_state"]["initialState"]["key_hits"]


async def test_no_sample_without_asking(session_maker, tmp_path: Path) -> None:
    await run_check(
        session_maker,
        _provider(Session([_search_page(), _card_page()]), ScraperOptions(min_hours_between_runs=0.0)),
        TARGET,
        tmp_path / "c.json",
        6.0,
        "spb",
    )

    assert not (tmp_path / "samples").exists()


def test_save_sample_needs_check(capsys) -> None:
    with pytest.raises(SystemExit):
        collector_cli.main(["--assisted", "--save-sample"])

    assert "--check" in capsys.readouterr().err


@pytest.mark.parametrize("file_name", [".gitignore", ".dockerignore"])
def test_samples_stay_out_of_git_and_images(file_name: str) -> None:
    assert "data/samples/" in (PROJECT_ROOT / file_name).read_text(encoding="utf-8").splitlines()


# ---------- the limit of checks ----------


def test_the_limit_of_checks_is_a_bounded_setting() -> None:
    assert Settings(_env_file=None).SCRAPER_CHECK_MAX_PER_DAY == 3
    assert Settings(_env_file=None, SCRAPER_CHECK_MAX_PER_DAY=10).SCRAPER_CHECK_MAX_PER_DAY == 10
    for wrong in (0, 11):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, SCRAPER_CHECK_MAX_PER_DAY=wrong)


async def test_the_limit_of_checks_comes_from_the_setting(session_maker, tmp_path: Path) -> None:
    options = ScraperOptions(min_hours_between_runs=0.0)

    async def check():
        session = Session([_no_data_page(final_url=SEARCH_URL)])
        return await run_check(session_maker, _provider(session, options), TARGET, tmp_path / "c.json", 6.0, "spb", 1)

    assert (await check()).refused is None
    second = await check()

    assert "не больше 1" in second.refused
    assert second.retry_at is not None


def test_the_command_line_passes_the_limit_and_the_two_pages(tmp_path: Path, monkeypatch, capsys) -> None:
    settings = Settings(
        _env_file=None,
        COLLECTOR_DB_PATH=str(tmp_path / "c.db"),
        COLLECT_QUERIES="dentist_clinic",
        COLLECT_DISTRICTS="Центральный",
        SCRAPER_CHECK_MAX_PER_DAY=1,
    )
    monkeypatch.setattr(collector_cli, "settings", settings)
    monkeypatch.setattr(collector_cli, "settings_error", None)
    monkeypatch.setattr(collector_cli, "CHECK_LOG_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(collector_cli, "SAMPLES_DIR", str(tmp_path / "samples"))
    pages: list[int] = []

    def provider(options: ScraperOptions) -> TwoGisScraperProvider:
        pages.append(options.max_pages_per_run)
        return _provider(Session([_search_page(_search_of_one()), _card_page()]), options)

    monkeypatch.setattr(collector_cli, "_scraper_provider", provider)

    assert collector_cli.main(["--assisted", "--check", "--save-sample"]) == collector_cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Название: Улыбка, стоматология" in out
    assert "Телефоны: +79210000001" in out
    assert "Сайт: smile.example" in out
    assert "Образец:" in out
    assert collector_cli.main(["--assisted", "--check"]) == collector_cli.EXIT_BLOCKED
    assert pages == [2, 2]


# ---------- what goes to the bot ----------


async def test_a_card_from_the_state_becomes_a_record_the_bot_accepts(session_maker, tmp_path: Path) -> None:
    run = await _enrich(session_maker, Session([_card_page(_firm_state(), firm="70001")]), [_item()], tmp_path)

    record = run.records[0]
    assert record["parsed"] is True
    assert record["phones"] == ["+79210000001"]
    assert record["website"] == "smile.example"
    assert record["socials"] == ["https://vk.com/smile", "https://wa.me/79210000001"]  # without the ready text
    assert record["email"] == "info@smile.example"
    data = run.path.read_bytes()
    parsed = parse_enrich_result(data, 1024 * 1024, 100)
    assert parsed.records[0].website == "smile.example"
    assert not parsed.rejected


async def test_a_card_with_a_lot_of_networks_is_still_read(session_maker, tmp_path: Path) -> None:
    networks = [{"type": "social", "value": f"https://vk.com/{number}" + "x" * 90} for number in range(9)]
    state = _firm_state()
    state["data"]["entity"]["profile"]["70001"]["data"]["contact_groups"] = [{"contacts": networks}]

    run = await _enrich(session_maker, Session([_card_page(state, firm="70001")]), [_item()], tmp_path)

    assert run.records[0]["parsed"] is True
    assert 0 < len(run.records[0]["socials"]) <= 5
    document = build_enrich_document(KIND_ENRICH_RESULT, run.records, datetime.now(UTC))
    assert not parse_enrich_result(json.dumps(document).encode(), 1024 * 1024, 100).rejected
