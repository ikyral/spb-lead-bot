"""Tests for reading cards of leads with a person at the computer: the diagnostics, the files and updating leads."""

import asyncio
import inspect
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Dispatcher
from aiogram.types import Chat, Message, Update, User
from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src import collector_cli
from src.bot.formatters import format_import_report, format_issued_lead
from src.bot.handlers import imports
from src.bot.middlewares import AdminOnlyMiddleware
from src.config import PROJECT_ROOT, Settings
from src.database.models import (
    CONTACT_REJECTED,
    STATUS_ISSUED,
    STATUS_NEW,
    STATUS_REJECTED,
    ImportLog,
    Lead,
    ProviderState,
)
from src.database.repo import (
    MAX_CONSECUTIVE_BLOCKS,
    claim_scraper_check,
    count_new_leads,
    get_collector_state,
    get_provider_states,
    get_unknown_site_leads,
    init_db,
    issue_next_leads,
    record_provider_block,
    set_site_verdict,
)
from src.services import assisted_runs, lead_enrich, lead_export, pipeline
from src.services.api_client import ProviderBlockedError, ProviderTemporaryError, ProviderUnavailableError
from src.services.assisted_runs import (
    CHECK_MAX_PAGES,
    CHECK_MAX_RUNS,
    CHECK_WINDOW,
    ENRICH_MAX_CARDS,
    ENRICH_MAX_UNRECOGNIZED,
    EnrichRun,
    run_check,
    run_enrich,
)
from src.services.collector import SearchTarget
from src.services.lead_enrich import (
    REASON_DUPLICATE_PHONE,
    REASON_ISSUED,
    REASON_MANUAL,
    REASON_NOT_NEWER,
    REASON_REJECTED,
    REASON_REPEATED,
    REASON_UNKNOWN_LEAD,
)
from src.services.lead_file import (
    ENRICH_REQUEST_MAX_LEADS,
    ENRICH_SCHEMA_VERSION,
    KIND_ENRICH_REQUEST,
    KIND_ENRICH_RESULT,
    REASON_BAD_CARD_LINK,
    REASON_BAD_DATE,
    REASON_BAD_WEBSITE,
    REASON_NO_PARSED_FLAG,
    EnrichRequest,
    EnrichRequestItem,
    LeadFileError,
    build_enrich_document,
    compute_checksum,
    enrich_result_record,
    parse_enrich_request,
    parse_enrich_result,
)
from src.services.lead_import import ImportResult, ImportService
from src.services.pipeline import save_collected_places
from src.services.providers.base import RawPlace, SearchArea, SearchQuery
from src.services.providers.twogis_scraper import (
    CAPTCHA_POLL_SECONDS,
    ScraperOptions,
    TwoGisScraperProvider,
    read_card_contacts,
)
from src.services.scorer import STATUS_POINTS
from src.services.scraper.browser import PageSnapshot, PlaywrightSession
from src.services.scraper.diagnostics import inline_state_names, json_shape, masked_url
from src.services.scraper.robots import RobotsRules, parse_robots
from src.services.site_status import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    SITE_DEAD,
    SITE_FOUND,
    SITE_NO_SITE,
    SITE_SOCIAL_ONLY,
    SITE_UNKNOWN,
    SOURCE_2GIS_CARD,
    SOURCE_MANUAL,
)
from src.services.twogis_links import readable_card_number

MAX_BYTES = 1024 * 1024
MAX_RECORDS = 100
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"

SEARCH_API = "https://catalog.api.2gis.ru/3.0/items?key=SECRETKEY&q=Иванов"
CARD_API = "https://catalog.api.2gis.ru/3.0/items/byid?key=SECRETKEY&id=1"
QUERY = SearchQuery(text="стоматология", category="dentist_clinic", high_margin=True)
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Центральный")
TARGET = SearchTarget(QUERY, AREA)
OPTIONS = ScraperOptions(assisted=True, headless=False, profile_dir=Path("profile"))

CONTACTS = [
    {
        "contacts": [
            {"type": "phone", "value": "8 (921) 000-00-02"},
            {"type": "email", "value": "Info@Dent.example"},
            {"type": "website", "value": "https://dent.example/?utm_source=2gis"},
            {"type": "vkontakte", "value": "https://vk.com/dent"},
        ]
    }
]
CARD_ITEM = {
    "id": "70000001_abc",
    "type": "branch",
    "name": "Дент Про",
    "address_name": "Невский проспект, 1",
    "contact_groups": CONTACTS,
}
SEARCH_BODY = {"meta": {"code": 200}, "result": {"total": 1, "items": [{"id": "70000001_abc", "name": "Дент Про"}]}}
CARD_BODY = {"meta": {"code": 200}, "result": {"items": [CARD_ITEM]}}

CAPTCHA = PageSnapshot(
    status=200,
    final_url="https://captcha.2gis.ru/form?return_url=https://2gis.ru/spb/search/x",
    title="2GIS Captcha",
    html=(FIXTURES / "twogis_captcha.html").read_text(encoding="utf-8"),
)


def _page(*bodies: tuple[str, dict], url: str = "https://2gis.ru/spb/x", html: str = "<html></html>") -> PageSnapshot:
    return PageSnapshot(
        status=200,
        final_url=url,
        title="2ГИС",
        html=html,
        json_bodies=[body for _, body in bodies],
        json_urls=[source for source, _ in bodies],
    )


def _card_page(number: str = "70000001", groups: list | None = CONTACTS) -> PageSnapshot:
    item = {"id": f"{number}_abc", "type": "branch", "name": "Дент Про"}
    if groups is not None:
        item["contact_groups"] = groups
    return _page((CARD_API, {"meta": {"code": 200}, "result": {"items": [item]}}))


class Session:
    """The browser: answers come from a list, a passed captcha is what the window shows after it."""

    def __init__(self, pages: list[PageSnapshot | Exception]) -> None:
        self.pages = list(pages)
        self.visited: list[str] = []
        self.wants: list = []
        self.closed = 0

    async def start(self) -> None:
        return None

    async def visit(self, url: str, wants_body) -> PageSnapshot:
        self.visited.append(url)
        self.wants.append(wants_body)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    async def read_page(self) -> PageSnapshot | None:
        return _page()

    async def bring_to_front(self) -> None:
        return None

    async def close(self) -> None:
        self.closed += 1


class Time:
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
    session: Session, options: ScraperOptions = OPTIONS, robots: RobotsRules | None = None, time: Time | None = None
) -> TwoGisScraperProvider:
    async def rules() -> RobotsRules:
        return robots or RobotsRules()

    time = time or Time()
    return TwoGisScraperProvider(
        options=options,
        session_factory=lambda: session,
        robots_loader=rules,
        sleep=time.sleep,
        clock=time.clock,
        notify=lambda _text: None,
    )


def _check_provider(session: Session, **kwargs) -> TwoGisScraperProvider:
    return _provider(session, replace(OPTIONS, max_pages_per_run=CHECK_MAX_PAGES, min_hours_between_runs=0.0), **kwargs)


def _lead(number: int = 1, **overrides) -> Lead:
    values = {
        "external_id": f"2gis_7000{number}_abc",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": "dentist_clinic",
        "district": "Центральный",
        "address": f"Невский проспект, {number}",
        "site_status": SITE_UNKNOWN,
        "score": 40,
        "crm_status": STATUS_NEW,
        "source_url": f"https://2gis.ru/spb/firm/7000{number}",
        "created_at": datetime(2026, 11, 5, 10, 0, tzinfo=UTC) + timedelta(minutes=number),
    }
    values.update(overrides)
    return Lead(**values)


async def _add(session_maker, *leads: Lead) -> None:
    async with session_maker() as session:
        session.add_all(leads)
        await session.commit()


async def _get(session_maker, number: int = 1) -> Lead:
    async with session_maker() as session:
        return (await session.scalars(select(Lead).where(Lead.external_id == f"2gis_7000{number}_abc"))).one()


async def _lead_count(session_maker) -> int:
    async with session_maker() as session:
        return int((await session.execute(select(func.count(Lead.id)))).scalar_one())


def _item(number: int = 1) -> EnrichRequestItem:
    return EnrichRequestItem("2gis", f"7000{number}_abc", f"https://2gis.ru/spb/firm/7000{number}", f"7000{number}")


def _request_record(number: int = 1, **overrides) -> dict:
    record = {
        "source": "2gis",
        "source_id": f"7000{number}_abc",
        "source_url": f"https://2gis.ru/spb/firm/7000{number}",
        "name": f"Организация {number}",
        "district": "Центральный",
    }
    record.update(overrides)
    return record


def _result_record(number: int = 1, parsed: bool = True, fetched_at: datetime = NOW, **fields) -> dict:
    return enrich_result_record("2gis", f"7000{number}_abc", fetched_at, parsed=parsed, **fields)


def _file(kind: str, records: list, /, **overrides) -> bytes:
    document = build_enrich_document(kind, records, NOW)
    document.update(overrides)
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _result_file(records: list, **overrides) -> bytes:
    return _file(KIND_ENRICH_RESULT, records, **overrides)


@pytest.fixture(autouse=True)
def checked_sites(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """No network: the address says what the checker answers."""
    checked: list[str | None] = []

    async def fake_analyze(url, client=None):
        checked.append(url)
        if not url:
            return SITE_NO_SITE, None
        if url and "dead" in url:
            return SITE_DEAD, "HTTP/Connection error"
        if url and ("vk.com" in url or "yclients" in url):
            return SITE_SOCIAL_ONLY, url
        return "has_site", url

    monkeypatch.setattr(lead_enrich, "analyze_website", fake_analyze)
    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)
    return checked


# ---------- what the diagnostics keep ----------


def test_the_shape_has_no_values_and_no_keys_that_look_like_data() -> None:
    secret_body = {
        "name": "Иванов Иван",
        "phone": "+79210000000",
        "count": 3,
        "rate": 4.5,
        "open": True,
        "none": None,
        "79210000000": {"value": "x"},
        "Иванов": "y",
        "a b c": "z",
        "items": [{"id": "1", "text": "секрет"}, {"id": 2, "extra": "ещё секрет"}],
        "empty": [],
    }

    shape = json_shape(secret_body)

    assert shape["name"] == "str"
    assert shape["count"] == "int"
    assert shape["rate"] == "float"
    assert shape["open"] == "bool"
    assert shape["none"] == "null"
    assert shape["empty"] == []
    assert shape["items"] == [{"id": "int|str", "text": "str", "extra": "str"}]
    dumped = json.dumps(shape, ensure_ascii=False)
    for secret in ("Иванов", "79210000000", "секрет", "a b c", "+7"):
        assert secret not in dumped


def test_the_shape_is_bounded() -> None:
    deep: dict = {}
    current = deep
    for _ in range(100):
        current["next"] = {}
        current = current["next"]
    wide = {f"field_{number}": number for number in range(500)}

    assert len(json.dumps(json_shape(deep))) < 400
    assert len(json_shape(wide)) < 100


def test_a_masked_url_has_no_query_and_no_ids() -> None:
    masked = masked_url("https://catalog.api.2gis.ru/3.0/items/70000001/%D0%98?key=SECRET&q=Иванов#frag")

    assert masked.startswith("https://catalog.api.2gis.ru/3.0/items/")
    for secret in ("SECRET", "Иванов", "70000001", "frag", "%D0"):
        assert secret not in masked


def test_inline_state_is_reported_by_name_only() -> None:
    html = '<script>var initialState = JSON.parse(\'{"phone":"+7921"}\'); window.__data = {"a":1};</script>'

    assert inline_state_names(html) == ["initialState", "__data"]


# ---------- --check ----------


async def test_check_opens_one_search_and_one_card_and_saves_only_the_structure(session_maker, tmp_path: Path) -> None:
    session = Session(
        [
            _page((SEARCH_API, SEARCH_BODY), ("https://stat.2gis.ru/x", {"token": "SECRETTOKEN"})),
            _page((CARD_API, CARD_BODY)),
        ]
    )
    log = tmp_path / "logs" / "scraper_check.json"

    report = await run_check(session_maker, _check_provider(session), TARGET, log, 6.0, "spb")

    assert len(session.visited) == 2
    assert "/search/" in session.visited[0]
    assert session.visited[1] == "https://2gis.ru/spb/firm/70000001"
    assert session.closed == 1
    assert report.search_found == 1
    assert report.recognized["name"] == "Дент Про"
    assert report.recognized["address"] == "Невский проспект, 1"
    assert report.recognized["phones"] == ["8 (921) 000-00-02"]
    assert report.recognized["website"] == "https://dent.example/?utm_source=2gis"
    assert report.recognized["socials"] == ["https://vk.com/dent"]
    assert report.recognized["source_url"] == "https://2gis.ru/spb/firm/70000001"

    saved = json.loads(log.read_text(encoding="utf-8"))
    assert saved["recognized"]["name"] == "Дент Про"
    structure = json.dumps(saved["pages"], ensure_ascii=False)
    assert '"contact_groups"' in structure
    # In the part about responses there are only names of keys and types.
    for secret in ("Дент Про", "Невский", "921", "SECRETKEY", "SECRETTOKEN", "Иванов", "dent.example", "70000001"):
        assert secret not in structure
    assert saved["pages"][0]["responses"][0]["expected_by_parser"] is True
    assert saved["pages"][0]["responses"][1]["expected_by_parser"] is False


async def test_check_writes_nothing_else(session_maker, tmp_path: Path) -> None:
    session = Session([_page((SEARCH_API, SEARCH_BODY)), _page((CARD_API, CARD_BODY))])

    await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "check.json", 6.0, "spb")

    assert await _lead_count(session_maker) == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == ["check.json", "test.db"]
    async with session_maker() as session_db:
        assert await get_provider_states(session_db) == {}


async def test_check_neither_resets_blocks_nor_counts_as_a_run(session_maker, tmp_path: Path) -> None:
    async with session_maker() as session_db:
        await record_provider_block(session_db, "2gis_scraper", "капча", 0.0001)
        state = (await get_provider_states(session_db))["2gis_scraper"]
        state.blocked_until = NOW - timedelta(days=1)
        await session_db.commit()
    session = Session([_page((SEARCH_API, SEARCH_BODY)), _page((CARD_API, CARD_BODY))])

    await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "check.json", 6.0, "spb")

    async with session_maker() as session_db:
        state = (await get_provider_states(session_db))["2gis_scraper"]
    assert state.consecutive_blocks == 1
    assert state.last_run_at is None


async def test_check_is_limited_to_three_runs_in_a_day(session_maker, tmp_path: Path) -> None:
    for _ in range(CHECK_MAX_RUNS):
        session = Session([_page(), _page()])
        report = await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "c.json", 6.0, "spb")
        assert report.refused is None

    session = Session([])
    report = await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "c.json", 6.0, "spb")

    assert report.refused is not None
    assert report.retry_at is not None
    assert session.visited == []


async def test_the_limit_of_checks_is_over_a_sliding_day(session_maker) -> None:
    async with session_maker() as session:
        for hours in range(CHECK_MAX_RUNS):
            retry_at, left = await claim_scraper_check(
                session, CHECK_MAX_RUNS, CHECK_WINDOW, NOW + timedelta(hours=hours)
            )
            assert retry_at is None
            assert left == CHECK_MAX_RUNS - hours - 1

        almost = NOW + CHECK_WINDOW - timedelta(minutes=1)
        retry_at, left = await claim_scraper_check(session, CHECK_MAX_RUNS, CHECK_WINDOW, almost)
        assert retry_at == NOW + CHECK_WINDOW
        assert left == 0

        retry_at, _ = await claim_scraper_check(session, CHECK_MAX_RUNS, CHECK_WINDOW, NOW + CHECK_WINDOW)
        assert retry_at is None


async def test_a_refused_check_does_not_use_up_a_run(session_maker, tmp_path: Path) -> None:
    async with session_maker() as session_db:
        await record_provider_block(session_db, "2gis_scraper", "капча", 6.0)
    session = Session([])

    report = await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "c.json", 6.0, "spb")

    assert "пауза" in report.refused
    assert session.visited == []
    async with session_maker() as session_db:
        assert (await get_collector_state(session_db)).scraper_check_runs is None


async def test_check_stops_at_a_switched_off_source(session_maker, tmp_path: Path) -> None:
    async with session_maker() as session_db:
        for _ in range(MAX_CONSECUTIVE_BLOCKS):
            await record_provider_block(session_db, "2gis_scraper", "капча", 6.0)
    session = Session([])

    report = await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "c.json", 6.0, "spb")

    assert "отключён" in report.refused
    assert session.visited == []


async def test_check_ignores_the_rest_between_collections_only(session_maker, tmp_path: Path) -> None:
    async with session_maker() as session_db:
        session_db.add(ProviderState(name="2gis_scraper", consecutive_blocks=0, last_run_at=datetime.now(UTC)))
        await session_db.commit()
    session = Session([_page(), _page()])

    report = await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "c.json", 6.0, "spb")

    assert report.refused is None
    assert len(session.visited) == 1  # the page shows no organization: there is no card to open


async def test_check_follows_robots_txt(session_maker, tmp_path: Path) -> None:
    session = Session([])
    robots = parse_robots("User-agent: *\nDisallow: /spb/search/", "spb-lead-bot")

    provider = _check_provider(session, robots=robots)
    report = await run_check(session_maker, provider, TARGET, tmp_path / "c.json", 6.0, "spb")

    assert "robots" in report.failed
    assert session.visited == []
    assert (tmp_path / "c.json").exists()
    async with session_maker() as session_db:
        assert await get_provider_states(session_db) == {}


async def test_check_passes_a_captcha_with_a_person_but_stays_within_two_pages(session_maker, tmp_path: Path) -> None:
    session = Session([CAPTCHA, _page((SEARCH_API, SEARCH_BODY))])
    time = Time()

    provider = _check_provider(session, time=time)
    report = await run_check(session_maker, provider, TARGET, tmp_path / "c.json", 6.0, "spb")

    assert len(session.visited) == CHECK_MAX_PAGES
    assert report.search_found == 1
    assert "исчерпан" in report.card_problem
    assert report.blocked is None
    assert all(3.0 <= pause <= 8.0 for pause in time.page_pauses) and time.page_pauses
    async with session_maker() as session_db:
        assert await get_provider_states(session_db) == {}


async def test_a_block_during_check_is_an_ordinary_block(session_maker, tmp_path: Path) -> None:
    refused = _page((SEARCH_API, {"meta": {"code": 403}}))
    session = Session([refused])

    report = await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "c.json", 6.0, "spb")

    assert "403" in report.blocked
    async with session_maker() as session_db:
        state = (await get_provider_states(session_db))["2gis_scraper"]
    assert state.consecutive_blocks == 1
    assert state.blocked_until is not None
    assert session.closed == 1


async def test_a_refusal_of_a_side_service_is_not_a_block(session_maker, tmp_path: Path) -> None:
    session = Session(
        [
            _page((SEARCH_API, SEARCH_BODY), ("https://stat.2gis.ru/x", {"meta": {"code": 403}})),
            _page((CARD_API, CARD_BODY)),
        ]
    )

    report = await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "c.json", 6.0, "spb")

    assert report.blocked is None
    assert report.recognized["name"] == "Дент Про"


async def test_check_finds_a_card_even_if_the_list_was_not_understood(session_maker, tmp_path: Path) -> None:
    unknown = _page(("https://api.2gis.ru/other", {"data": [1]}), html='<a href="/spb/firm/70000001">x</a>')
    session = Session([unknown, _page((CARD_API, CARD_BODY))])

    report = await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "c.json", 6.0, "spb")

    assert report.search_problem is not None
    assert report.blocked is None
    assert session.visited[1] == "https://2gis.ru/spb/firm/70000001"
    assert report.recognized["name"] == "Дент Про"
    saved = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert saved["pages"][0]["responses"][0]["url"] == "https://api.2gis.ru/other"
    assert saved["pages"][0]["responses"][0]["structure"] == {"data": ["int"]}


async def test_check_says_why_when_nothing_was_recognized(session_maker, tmp_path: Path) -> None:
    session = Session([_page(("https://api.2gis.ru/other", {"data": 1}))])

    report = await run_check(session_maker, _check_provider(session), TARGET, tmp_path / "c.json", 6.0, "spb")

    assert report.search_problem is not None
    assert report.card_problem is not None
    assert report.recognized == {}


# ---------- reading a card ----------


@pytest.mark.parametrize(
    "card",
    [
        {"id": "1"},  # the field is not there: the page did not bring contacts, this says nothing
        {"id": "1", "contact_groups": "x"},
        {"id": "1", "contact_groups": [{"contacts": [{"type": "неизвестный", "value": "x"}]}]},
        {"id": "1", "contact_groups": [{"other": []}]},
    ],
)
def test_a_card_that_does_not_show_its_contacts_is_not_read(card: dict) -> None:
    assert read_card_contacts(card) is None


def test_a_card_that_lists_contacts_without_a_site_is_read() -> None:
    assert read_card_contacts({"contact_groups": []}).website is None
    contacts = read_card_contacts({"contact_groups": [{"contacts": [{"type": "phone", "value": "+78125550101"}]}]})
    assert contacts.phones == ["+78125550101"]
    assert contacts.website is None


def test_a_social_network_given_as_the_site_goes_to_the_checker() -> None:
    listed = {"contact_groups": [{"contacts": [{"type": "website", "value": "https://vk.com/x"}]}]}
    apart = {"contact_groups": [{"contacts": [{"type": "vkontakte", "value": "https://vk.com/x"}]}]}

    assert read_card_contacts(listed).website == "https://vk.com/x"
    assert read_card_contacts(apart).website is None
    assert read_card_contacts(apart).socials == ["https://vk.com/x"]


@pytest.mark.parametrize(
    ("url", "number"),
    [
        ("https://2gis.ru/spb/firm/70001", "70001"),
        ("https://2gis.ru/firm/70001", "70001"),
        ("https://www.2gis.ru/spb/firm/70001/", "70001"),
        ("https://evil.example/spb/firm/70001", None),
        ("https://2gis.ru.evil.example/spb/firm/70001", None),
        ("https://user@2gis.ru/spb/firm/70001", None),
        ("https://2gis.xyz/spb/firm/70001", None),
        ("file:///etc/passwd", None),
        ("javascript:alert(1)", None),
        ("https://2gis.ru/spb/user/70001", None),
        ("https://2gis.ru/spb/firm/70001?stat=1", None),
        ("https://2gis.ru/a/b/firm/70001", None),
        ("https://2gis.ru/spb/firm/abc", None),
        ("", None),
        (None, None),
    ],
)
def test_only_links_to_a_card_on_the_site_can_be_opened(url: str | None, number: str | None) -> None:
    assert readable_card_number(url) == number


# ---------- the request file ----------


def test_a_request_is_read_back() -> None:
    request = parse_enrich_request(_file(KIND_ENRICH_REQUEST, [_request_record()]), MAX_BYTES)

    assert request.total == 1
    assert request.items == [_item()]
    assert not request.rejected


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"checksum": "0" * 64}, "контрольная сумма"),
        ({"checksum": None}, "контрольная сумма"),
        ({"count": 5}, "count"),
        ({"schema_version": 99}, "версия"),
        ({"schema_version": True}, "версия"),
        ({"kind": "enrich_result"}, "другое значение kind"),
        ({"kind": None}, "другое значение kind"),
        ({"records": "x"}, "records"),
    ],
)
def test_a_wrong_request_is_refused_as_a_whole(change: dict, message: str) -> None:
    with pytest.raises(LeadFileError, match=message):
        parse_enrich_request(_file(KIND_ENRICH_REQUEST, [_request_record()], **change), MAX_BYTES)


def test_a_request_with_too_much_is_refused() -> None:
    records = [_request_record(number) for number in range(ENRICH_REQUEST_MAX_LEADS + 1)]
    data = _file(KIND_ENRICH_REQUEST, records)

    with pytest.raises(LeadFileError, match="записей"):
        parse_enrich_request(data, MAX_BYTES)
    with pytest.raises(LeadFileError, match="больше"):
        parse_enrich_request(data, 100)


@pytest.mark.parametrize(
    "record",
    [
        _request_record(source_url="https://evil.example/spb/firm/70001"),
        _request_record(source_url="file:///etc/passwd"),
        _request_record(source_url=None),
        _request_record(source_url="https://2gis.ru/spb/firm/99999"),  # the card of another organization
        _request_record(source="osm"),
    ],
)
def test_a_link_a_browser_should_not_open_is_rejected(record: dict) -> None:
    request = parse_enrich_request(_file(KIND_ENRICH_REQUEST, [record, _request_record(2)]), MAX_BYTES)

    assert request.rejected[REASON_BAD_CARD_LINK] == 1
    assert [item.firm_number for item in request.items] == ["70002"]


# ---------- --enrich ----------


async def _enrich(session_maker, session: Session, items: list, export_dir: Path, **kwargs) -> EnrichRun:
    run = EnrichRun()
    request = EnrichRequest(total=len(items), items=items)
    await run_enrich(session_maker, _provider(session, **kwargs), request, export_dir, 6.0, run)
    return run


def _document(run: EnrichRun) -> dict:
    return json.loads(run.path.read_text(encoding="utf-8"))


async def test_enrich_writes_a_result_the_bot_accepts(session_maker, tmp_path: Path) -> None:
    session = Session([_card_page("70001"), _card_page("70002", groups=[])])
    time = Time()

    run = await _enrich(session_maker, session, [_item(1), _item(2)], tmp_path, time=time)

    assert session.visited == ["https://2gis.ru/spb/firm/70001", "https://2gis.ru/spb/firm/70002"]
    assert session.closed == 1
    assert len(time.page_pauses) == 1
    assert 3.0 <= time.page_pauses[0] <= 8.0
    assert run.path.parent == tmp_path
    assert run.path.name.startswith("enrich_")
    assert not list(tmp_path.glob("*.tmp"))

    document = _document(run)
    assert document["kind"] == KIND_ENRICH_RESULT
    assert document["schema_version"] == ENRICH_SCHEMA_VERSION
    assert document["checksum"] == compute_checksum(document["records"])
    first, second = document["records"]
    assert first["source"] == "2gis"
    assert first["source_id"] == "70001_abc"
    assert first["parsed"] is True
    assert first["phones"] == ["8 (921) 000-00-02"]
    assert first["website"] == "https://dent.example/?utm_source=2gis"
    assert first["socials"] == ["https://vk.com/dent"]
    assert first["email"] == "Info@Dent.example"
    assert first["fetched_at"]
    assert second["parsed"] is True
    assert second["website"] is None

    parsed = parse_enrich_result(run.path.read_bytes(), MAX_BYTES, MAX_RECORDS)
    assert len(parsed.records) == 2
    assert not parsed.rejected


async def test_enrich_touches_only_the_counters(session_maker, tmp_path: Path) -> None:
    session = Session([_card_page("70001")])

    await _enrich(session_maker, session, [_item(1)], tmp_path)

    assert await _lead_count(session_maker) == 0
    async with session_maker() as session_db:
        assert (await get_provider_states(session_db))["2gis_scraper"].last_run_at is not None


async def test_an_unrecognized_card_is_marked_and_is_not_a_card_without_a_site(session_maker, tmp_path: Path) -> None:
    session = Session(
        [
            _page(),  # no data at all
            _card_page("70002", groups=None),  # the card without the field with contacts
            _card_page("70003"),
        ]
    )

    run = await _enrich(session_maker, session, [_item(1), _item(2), _item(3)], tmp_path)

    first, second, third = _document(run)["records"]
    assert first["parsed"] is False
    assert second["parsed"] is False
    assert third["parsed"] is True
    assert first["website"] is None
    assert run.blocked is None


async def test_enrich_opens_no_more_than_a_limit_of_cards(session_maker, tmp_path: Path) -> None:
    numbers = range(10, 10 + ENRICH_MAX_CARDS + 5)
    session = Session([_card_page(f"7000{number}") for number in numbers])

    run = await _enrich(session_maker, session, [_item(number) for number in numbers], tmp_path)

    assert len(session.visited) == ENRICH_MAX_CARDS
    assert run.over_limit == 5
    assert len(_document(run)["records"]) == ENRICH_MAX_CARDS


async def test_a_block_keeps_what_was_read_and_counts_as_a_block(session_maker, tmp_path: Path) -> None:
    session = Session([_card_page("70001"), _page((CARD_API, {"meta": {"code": 403}}))])

    run = await _enrich(session_maker, session, [_item(1), _item(2), _item(3)], tmp_path)

    assert "403" in run.blocked
    assert [record["source_id"] for record in _document(run)["records"]] == ["70001_abc"]
    assert run.not_opened == 2
    assert session.closed == 1
    async with session_maker() as session_db:
        state = (await get_provider_states(session_db))["2gis_scraper"]
    assert state.consecutive_blocks == 1
    assert state.blocked_until is not None


async def test_a_captcha_nobody_passes_is_a_block(session_maker, tmp_path: Path) -> None:
    class Waiting(Session):
        async def read_page(self) -> PageSnapshot | None:
            return CAPTCHA

    session = Waiting([_card_page("70001"), CAPTCHA])

    run = await _enrich(session_maker, session, [_item(1), _item(2)], tmp_path)

    assert "капча" in run.blocked
    assert len(_document(run)["records"]) == 1


async def test_a_passed_captcha_neither_blocks_nor_resets_the_counter(session_maker, tmp_path: Path) -> None:
    async with session_maker() as session_db:
        await record_provider_block(session_db, "2gis_scraper", "капча", 0.0001)
        state = (await get_provider_states(session_db))["2gis_scraper"]
        state.blocked_until = NOW - timedelta(days=1)
        await session_db.commit()
    session = Session([CAPTCHA, _card_page("70001")])

    run = await _enrich(session_maker, session, [_item(1)], tmp_path)

    assert run.read == 1
    assert run.blocked is None
    async with session_maker() as session_db:
        assert (await get_provider_states(session_db))["2gis_scraper"].consecutive_blocks == 1


async def test_a_run_without_a_captcha_resets_the_counter(session_maker, tmp_path: Path) -> None:
    async with session_maker() as session_db:
        await record_provider_block(session_db, "2gis_scraper", "капча", 0.0001)
        state = (await get_provider_states(session_db))["2gis_scraper"]
        state.blocked_until = NOW - timedelta(days=1)
        await session_db.commit()

    await _enrich(session_maker, Session([_card_page("70001")]), [_item(1)], tmp_path)

    async with session_maker() as session_db:
        assert (await get_provider_states(session_db))["2gis_scraper"].consecutive_blocks == 0


async def test_unrecognized_cards_in_a_row_stop_the_run_without_a_block(session_maker, tmp_path: Path) -> None:
    count = ENRICH_MAX_UNRECOGNIZED + 3
    session = Session([_page() for _ in range(count)])

    run = await _enrich(session_maker, session, [_item(number) for number in range(count)], tmp_path)

    assert len(session.visited) == ENRICH_MAX_UNRECOGNIZED
    assert run.blocked is None
    assert run.failed == "Формат страницы не распознан: нужна правка парсера, это не блокировка"
    assert all(record["parsed"] is False for record in _document(run)["records"])
    async with session_maker() as session_db:
        state = (await get_provider_states(session_db))["2gis_scraper"]
    assert state.consecutive_blocks == 0
    assert state.blocked_until is None
    assert state.last_run_at is None  # the interval between runs stays free


async def test_a_page_that_did_not_open_is_not_a_card_without_a_site(session_maker, tmp_path: Path) -> None:
    session = Session([ProviderTemporaryError("страница не загрузилась вовремя"), _card_page("70002")])

    run = await _enrich(session_maker, session, [_item(1), _item(2)], tmp_path)

    first, second = _document(run)["records"]
    assert first["parsed"] is False
    assert second["parsed"] is True


async def test_pages_that_do_not_open_stop_the_run_without_a_block(session_maker, tmp_path: Path) -> None:
    session = Session([ProviderTemporaryError("окно закрыто") for _ in range(5)])

    run = await _enrich(session_maker, session, [_item(number) for number in range(5)], tmp_path)

    assert len(session.visited) == 3
    assert "окно закрыто" in run.stopped
    async with session_maker() as session_db:
        assert (await get_provider_states(session_db))["2gis_scraper"].consecutive_blocks == 0


@pytest.mark.parametrize("reason", ["пауза", "rest", "off"])
async def test_enrich_keeps_to_the_state_of_blocks(session_maker, tmp_path: Path, reason: str) -> None:
    now = datetime.now(UTC)
    state = ProviderState(name="2gis_scraper", consecutive_blocks=0, last_block_reason="капча")
    if reason == "пауза":
        state.blocked_until = now + timedelta(hours=1)
    elif reason == "rest":
        state.last_run_at = now - timedelta(hours=1)
    else:
        state.disabled_at = now
    async with session_maker() as session_db:
        session_db.add(state)
        await session_db.commit()
    session = Session([])

    run = await _enrich(session_maker, session, [_item(1)], tmp_path)

    assert run.refused is not None
    assert session.visited == []
    assert run.path is None


async def test_enrich_follows_robots_txt(session_maker, tmp_path: Path) -> None:
    session = Session([])
    robots = parse_robots("User-agent: *\nDisallow: */firm/", "spb-lead-bot")

    run = await _enrich(session_maker, session, [_item(1)], tmp_path, robots=robots)

    assert "robots" in run.failed
    assert session.visited == []
    assert run.path is None


async def test_nothing_read_means_no_file(session_maker, tmp_path: Path) -> None:
    run = await _enrich(session_maker, Session([ProviderBlockedError("отказ")]), [_item(1)], tmp_path / "out")

    assert run.path is None
    assert not (tmp_path / "out").exists()


async def test_an_interrupted_run_saves_what_it_has(session_maker, tmp_path: Path) -> None:
    class Interrupted(Session):
        async def visit(self, url: str, wants_body) -> PageSnapshot:
            if self.visited:
                raise asyncio.CancelledError
            return await super().visit(url, wants_body)

    session = Interrupted([_card_page("70001")])
    run = EnrichRun()
    request = EnrichRequest(total=2, items=[_item(1), _item(2)])

    with pytest.raises(asyncio.CancelledError):
        await run_enrich(session_maker, _provider(session), request, tmp_path, 6.0, run)

    assert len(_document(run)["records"]) == 1
    assert session.closed == 1


async def test_a_record_the_bot_would_refuse_is_not_called_read(session_maker, tmp_path: Path) -> None:
    long_site = "https://dent.example/" + "x" * 600
    groups = [{"contacts": [{"type": "website", "value": long_site}]}]

    run = await _enrich(session_maker, Session([_card_page("70001", groups=groups)]), [_item(1)], tmp_path)

    assert _document(run)["records"][0]["parsed"] is False


# ---------- the browser stays as it is ----------


async def test_the_browser_starts_with_nothing_but_the_defaults(tmp_path: Path) -> None:
    playwright = MagicMock()
    context = MagicMock()
    context.pages = [MagicMock()]
    playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)
    current = Settings(_env_file=None, SCRAPER_PROFILE_DIR=str(tmp_path / "p"))
    options = ScraperOptions.from_settings(current, assisted=True)

    with patch("playwright.async_api.async_playwright", return_value=starter):
        session = PlaywrightSession(options.page_timeout_seconds, options.headless, options.profile_dir)
        await session.start()

    playwright.chromium.launch_persistent_context.assert_awaited_once_with(
        str(tmp_path / "p"), headless=False, locale="ru-RU"
    )


async def test_visit_keeps_the_address_of_every_response() -> None:
    page = MagicMock()
    handlers: dict = {}
    page.on = lambda event, handler: handlers.setdefault(event, handler)
    page.url = "https://2gis.ru/spb/firm/1"
    page.title = AsyncMock(return_value="t")
    page.content = AsyncMock(return_value="<html></html>")
    page.wait_for_load_state = AsyncMock()
    page.evaluate = AsyncMock(return_value=[{"problem": "нет"}, {"problem": "нет"}])

    def _response(url: str, body: bytes) -> MagicMock:
        response = MagicMock()
        response.url = url
        response.body = AsyncMock(return_value=body)
        return response

    async def goto(url, **kwargs):
        handlers["response"](_response("https://a.2gis.ru/bad", b"not json"))
        handlers["response"](_response("https://a.2gis.ru/good", b'{"a": 1}'))
        return MagicMock(status=200)

    page.goto = goto
    session = PlaywrightSession()
    session._page = page

    snapshot = await session.visit("https://2gis.ru/spb/firm/1", lambda _url: True)

    assert snapshot.json_urls == ["https://a.2gis.ru/good"]
    assert snapshot.json_bodies == [{"a": 1}]
    assert snapshot.bodies_from(lambda url: "good" in url) == [{"a": 1}]
    assert snapshot.bodies_from(lambda url: "other" in url) == []


def _page_with_listeners() -> tuple[MagicMock, dict]:
    """Removing a listener twice is an error, as in the real page."""
    page = MagicMock()
    handlers: dict = {}
    page.on = lambda event, handler: handlers.setdefault(event, handler)
    page.remove_listener = lambda event, handler: handlers.pop(event)
    page.url = "https://2gis.ru/spb/firm/1"
    page.title = AsyncMock(return_value="t")
    page.content = AsyncMock(return_value="<html></html>")
    page.wait_for_load_state = AsyncMock()
    page.evaluate = AsyncMock(return_value=[{"problem": "нет"}, {"problem": "нет"}])
    return page, handlers


async def test_a_response_during_the_wait_for_bodies_is_ignored_without_an_error() -> None:
    page, handlers = _page_with_listeners()
    late = MagicMock()
    late.url = "https://a.2gis.ru/late"
    late.body = AsyncMock(return_value=b'{"late": 1}')

    async def body_of_the_first() -> bytes:
        # Nothing before this waits for anything, so here the wait for bodies has already begun.
        handlers["response"](late)
        await asyncio.sleep(0)
        return b'{"first": 1}'

    first = MagicMock()
    first.url = "https://a.2gis.ru/first"
    first.body = body_of_the_first

    async def goto(url, **kwargs):
        handlers["response"](first)
        return MagicMock(status=200)

    page.goto = goto
    session = PlaywrightSession()
    session._page = page

    snapshot = await session.visit("https://2gis.ru/spb/firm/1", lambda _url: True)

    assert snapshot.json_urls == ["https://a.2gis.ru/first"]
    assert snapshot.json_bodies == [{"first": 1}]
    assert handlers == {}  # stopped listening, and only once


async def test_a_failed_page_stops_listening_and_cancels_the_reads() -> None:
    page, handlers = _page_with_listeners()
    started = asyncio.Event()

    async def endless_body() -> bytes:
        started.set()
        await asyncio.Event().wait()
        return b"{}"

    response = MagicMock()
    response.url = "https://a.2gis.ru/first"
    response.body = endless_body

    async def goto(url, **kwargs):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        handlers["response"](response)
        await started.wait()
        raise PlaywrightTimeoutError("timeout")

    page.goto = goto
    session = PlaywrightSession()
    session._page = page

    with pytest.raises(ProviderTemporaryError):
        await session.visit("https://2gis.ru/spb/firm/1", lambda _url: True)

    assert handlers == {}
    leftovers = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    await asyncio.gather(*leftovers, return_exceptions=True)
    assert all(task.cancelled() for task in leftovers)
    assert leftovers


def test_new_code_has_nothing_that_touches_or_hides_from_a_captcha() -> None:
    sources = (
        "\n".join(inspect.getsource(module).lower() for module in (assisted_runs, lead_enrich))
        + (PROJECT_ROOT / "src/collector_cli.py").read_text(encoding="utf-8").lower()
    )
    for word in (
        "stealth",
        "webdriver",
        "navigator",
        "add_init_script",
        "ignore_default_args",
        "user_agent",
        "proxy",
        ".click(",
        ".mouse",
        ".keyboard",
        "recaptcha",
        "anticaptcha",
        "2captcha",
    ):
        assert word not in sources, word


# ---------- the bot: /export_unknown ----------


async def test_the_best_unknown_leads_are_chosen(session_maker) -> None:
    await _add(
        session_maker,
        _lead(1, score=40),
        _lead(2, score=70),
        _lead(3, score=50, is_chain=True),
        _lead(4, score=90, site_status=SITE_NO_SITE),
        _lead(5, score=90, crm_status=STATUS_ISSUED),
        _lead(6, score=90, source_url=None),
        _lead(7, score=90, source_url="https://evil.example/firm/70007"),
        _lead(8, score=90, source="osm", external_id="osm_70008_abc"),
    )

    async with session_maker() as session:
        leads = await get_unknown_site_leads(session, 30)
        limited = await get_unknown_site_leads(session, 1)

    assert [lead.name for lead in leads] == ["Организация 2", "Организация 1"]
    assert [lead.name for lead in limited] == ["Организация 2"]


async def _export_command(session_maker, monkeypatch: pytest.MonkeyPatch, args: str | None) -> MagicMock:
    monkeypatch.setattr(imports, "async_session_maker", session_maker)
    message = MagicMock()
    message.answer = AsyncMock()
    message.answer_document = AsyncMock()
    await imports.handle_export_unknown(message, MagicMock(args=args))
    return message


def _sent_document(message: MagicMock) -> dict:
    return json.loads(message.answer_document.await_args.args[0].data.decode("utf-8"))


async def test_export_command_sends_a_versioned_file_without_personal_data(session_maker, monkeypatch) -> None:
    await _add(
        session_maker,
        _lead(1, phone="+79210000000", email="owner@example.com", contact_status_by="Иван", address="секретный адрес"),
    )

    message = await _export_command(session_maker, monkeypatch, None)

    document = _sent_document(message)
    assert document["kind"] == KIND_ENRICH_REQUEST
    assert document["schema_version"] == ENRICH_SCHEMA_VERSION
    assert document["count"] == 1
    assert document["checksum"] == compute_checksum(document["records"])
    assert document["exported_at"]
    assert set(document["records"][0]) == {"source", "source_id", "source_url", "name", "district"}
    text = json.dumps(document, ensure_ascii=False)
    for secret in ("+7921", "owner@", "Иван", "секретный"):
        assert secret not in text
    # What the bot gives away, a collector accepts.
    assert parse_enrich_request(json.dumps(document).encode(), MAX_BYTES).items[0].firm_number == "70001"
    assert message.answer_document.await_args.args[0].filename.startswith("enrich_request_")


@pytest.mark.parametrize(
    ("args", "expected"),
    [(None, 30), ("2", 2), ("500", ENRICH_REQUEST_MAX_LEADS), (" 7 ", 7)],
)
async def test_export_command_keeps_to_the_limits(session_maker, monkeypatch, args: str | None, expected: int) -> None:
    await _add(session_maker, *(_lead(number) for number in range(10, 90)))

    message = await _export_command(session_maker, monkeypatch, args)

    assert _sent_document(message)["count"] == expected


@pytest.mark.parametrize("args", ["0", "-1", "abc", "٣", "1 2"])
async def test_export_command_explains_a_wrong_number(session_maker, monkeypatch, args: str) -> None:
    message = await _export_command(session_maker, monkeypatch, args)

    assert "Использование" in message.answer.await_args.args[0]
    message.answer_document.assert_not_awaited()


async def test_export_command_says_when_there_is_nothing(session_maker, monkeypatch) -> None:
    message = await _export_command(session_maker, monkeypatch, None)

    message.answer_document.assert_not_awaited()
    assert "нет" in message.answer.await_args.args[0]


@pytest.mark.parametrize(("user_id", "answered"), [(100, True), (999, False)])
async def test_export_command_goes_through_the_bot_and_only_for_admins(
    session_maker, monkeypatch, user_id: int, answered: bool
) -> None:
    await _add(session_maker, _lead(1), _lead(2))
    monkeypatch.setattr(imports, "async_session_maker", session_maker)
    monkeypatch.setattr("src.bot.middlewares.settings", Settings(_env_file=None, ADMIN_TG_ID=100))
    send = AsyncMock()
    monkeypatch.setattr(Message, "answer_document", send)
    dp = Dispatcher()
    dp.message.middleware(AdminOnlyMiddleware())
    dp.include_router(imports.router)
    message = Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="x"),
        text="/export_unknown 1",
    )

    try:
        await dp.feed_update(MagicMock(id=1), Update(update_id=1, message=message))
    finally:
        imports.router._parent_router = None
        dp.sub_routers.clear()

    assert send.await_count == (1 if answered else 0)
    if answered:
        assert json.loads(send.await_args.args[0].data.decode("utf-8"))["count"] == 1


# ---------- the bot: taking a result ----------


@pytest.fixture
def service(session_maker, tmp_path: Path) -> tuple[ImportService, list]:
    reports: list[tuple[ImportResult, int | None]] = []

    async def report(result: ImportResult, exclude_chat: int | None) -> None:
        reports.append((result, exclude_chat))

    return ImportService(session_maker, report, tmp_path / "inbox", MAX_BYTES, MAX_RECORDS), reports


async def _take(service: tuple[ImportService, list], records: list, **overrides) -> ImportResult:
    return await service[0].import_bytes(_result_file(records, **overrides), "enrich.json", "telegram")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"checksum": "0" * 64}, "контрольная сумма"),
        ({"checksum": None}, "контрольная сумма"),
        ({"count": 9}, "count"),
        ({"schema_version": 2}, "версия"),
    ],
)
async def test_a_damaged_result_changes_nothing(session_maker, service, change: dict, message: str) -> None:
    await _add(session_maker, _lead())

    result = await _take(service, [_result_record(website="https://site.example")], **change)

    assert result.ok is False
    assert message in result.error
    assert (await _get(session_maker)).site_status == SITE_UNKNOWN


async def test_a_result_over_the_limits_is_refused(session_maker, tmp_path: Path) -> None:
    small = ImportService(session_maker, AsyncMock(), tmp_path, MAX_BYTES, 1)
    tiny = ImportService(session_maker, AsyncMock(), tmp_path, 10, MAX_RECORDS)
    data = _result_file([_result_record(1), _result_record(2)])

    too_many = await small.import_bytes(data, "f.json", "telegram")
    too_big = await tiny.import_bytes(data, "f.json", "telegram")

    assert "записей" in too_many.error
    assert "больше" in too_big.error


async def test_a_request_sent_back_to_the_bot_is_explained(session_maker, service) -> None:
    await _add(session_maker, _lead())
    data = _file(KIND_ENRICH_REQUEST, [_request_record()])

    result = await service[0].import_bytes(data, "enrich_request.json", "telegram")

    assert result.ok is False
    assert "файл запроса" in result.error
    assert await _lead_count(session_maker) == 1


async def test_an_unknown_kind_is_refused(service) -> None:
    result = await service[0].import_bytes(_file("something", []), "x.json", "telegram")

    assert result.ok is False
    assert "неизвестный тип" in result.error


async def test_a_working_site_takes_the_lead_out_of_the_pool(session_maker, service, checked_sites) -> None:
    await _add(session_maker, _lead())

    result = await _take(service, [_result_record(website="https://site.example/?utm_source=2gis")])

    assert result.ok is True
    assert result.saved_new == 0
    assert result.enrichment.updated == 1
    assert result.enrichment.sites_working == 1
    assert result.pool_size == 0
    lead = await _get(session_maker)
    assert checked_sites == ["https://site.example"]
    assert lead.site_status == SITE_FOUND
    assert lead.site_source == SOURCE_2GIS_CARD
    assert lead.site_confidence == CONFIDENCE_HIGH
    assert lead.site_checked_at is not None
    assert lead.site_url == "https://site.example"
    assert lead.raw_website == "https://site.example"
    assert lead.score == 0
    assert lead.crm_status == STATUS_NEW
    async with session_maker() as session:
        assert await issue_next_leads(session, 10) == []


@pytest.mark.parametrize(
    ("website", "status", "counter"),
    [
        ("https://dead.example", SITE_DEAD, "sites_dead"),
        ("https://vk.com/org", SITE_SOCIAL_ONLY, "sites_social"),
        ("https://yclients.com/org", SITE_SOCIAL_ONLY, "sites_social"),
    ],
)
async def test_other_sites_get_their_statuses(session_maker, service, website: str, status: str, counter: str) -> None:
    await _add(session_maker, _lead())

    result = await _take(service, [_result_record(website=website, phones=["+79210000002"])])

    lead = await _get(session_maker)
    assert lead.site_status == status
    assert lead.site_source == SOURCE_2GIS_CARD
    assert lead.site_confidence == CONFIDENCE_HIGH
    assert lead.score >= STATUS_POINTS[status]
    assert getattr(result.enrichment, counter) == 1
    assert result.enrichment.updated == 1
    assert result.enrichment.sites_found == 1
    assert result.pool_size == 1
    if status == SITE_SOCIAL_ONLY:
        assert website in lead.socials


async def test_a_card_without_a_site_says_only_that(session_maker, service, checked_sites) -> None:
    await _add(session_maker, _lead(score=30))

    result = await _take(service, [_result_record(phones=["8 (921) 000-00-02"], socials=["https://vk.com/org"])])

    lead = await _get(session_maker)
    assert checked_sites == []
    assert lead.site_status == SITE_NO_SITE
    assert lead.site_source == SOURCE_2GIS_CARD
    assert lead.site_confidence == CONFIDENCE_MEDIUM
    assert lead.phone == "+79210000002"
    assert lead.socials == "https://vk.com/org"
    assert lead.score > 30
    assert result.enrichment.no_site == 1
    assert "В 2ГИС сайт не указан" in format_issued_lead(lead)


async def test_a_card_read_as_empty_is_still_a_card_without_a_site(session_maker, service) -> None:
    await _add(session_maker, _lead())

    await _take(service, [_result_record()])

    assert (await _get(session_maker)).site_status == SITE_NO_SITE


async def test_an_unrecognized_card_leaves_the_lead_as_it_is(session_maker, service, checked_sites) -> None:
    await _add(session_maker, _lead(score=33))
    # Contacts in such a record mean nothing: it says that the card was not understood.
    record = _result_record(parsed=False, website="https://site.example", phones=["+78125550101"])

    result = await _take(service, [record])

    lead = await _get(session_maker)
    assert lead.site_status == SITE_UNKNOWN
    assert lead.site_source is None
    assert lead.site_checked_at is None
    assert lead.phone is None
    assert lead.score == 33
    assert checked_sites == []
    assert result.enrichment.not_recognized == 1
    assert result.enrichment.updated == 0


async def test_phones_are_normalized_and_added_to_a_saved_one(session_maker, service) -> None:
    await _add(session_maker, _lead(phone="+78125550101", email="old@example.com", socials="https://t.me/old"))

    await _take(
        service,
        [
            _result_record(
                phones=["8 812 555-01-01", "8 (921) 000-00-02"],
                email="New@Example.com",
                socials=["https://vk.com/org", "https://t.me/old"],
            )
        ],
    )

    lead = await _get(session_maker)
    assert lead.phone == "+78125550101, +79210000002"
    assert lead.email == "old@example.com"
    assert lead.socials == "https://t.me/old, https://vk.com/org"


async def test_an_email_is_saved_if_there_was_none(session_maker, service) -> None:
    await _add(session_maker, _lead())

    await _take(service, [_result_record(email="Info@Example.com")])

    assert (await _get(session_maker)).email == "info@example.com"


async def test_many_phones_fit_into_the_column_as_whole_numbers(session_maker, service) -> None:
    await _add(session_maker, _lead())

    await _take(service, [_result_record(phones=[f"+7812555{number:04d}" for number in range(10)])])

    phone = (await _get(session_maker)).phone
    assert len(phone) <= 128
    assert all(len(part) == 12 for part in phone.split(", "))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"crm_status": STATUS_ISSUED, "issued_at": NOW}, REASON_ISSUED),
        ({"crm_status": "in_work"}, REASON_ISSUED),
        ({"crm_status": STATUS_REJECTED}, REASON_REJECTED),
        ({"crm_status": STATUS_ISSUED, "issued_at": NOW, "contact_status": CONTACT_REJECTED}, REASON_REJECTED),
        ({"site_source": SOURCE_MANUAL, "site_status": SITE_NO_SITE}, REASON_MANUAL),
        ({"site_source": SOURCE_MANUAL, "site_status": SITE_FOUND}, REASON_MANUAL),
    ],
)
async def test_leads_that_must_not_change(session_maker, service, checked_sites, changes: dict, reason: str) -> None:
    await _add(session_maker, _lead(score=33, **changes))
    before = await _get(session_maker)

    result = await _take(service, [_result_record(website="https://site.example", phones=["+78125550101"])])

    lead = await _get(session_maker)
    assert lead.site_status == before.site_status
    assert lead.site_source == before.site_source
    assert lead.phone is None
    assert lead.raw_website is None
    assert lead.score == 33
    assert lead.crm_status == before.crm_status
    assert checked_sites == []  # and no requests to a site that would not be used
    assert result.enrichment.ignored[reason] == 1
    assert result.enrichment.updated == 0


async def test_a_verdict_given_while_sites_are_checked_wins(session_maker, service, monkeypatch) -> None:
    await _add(session_maker, _lead())

    async def analyze_during_verdict(url, client=None):
        async with session_maker() as session:
            lead = (await session.scalars(select(Lead))).one()
            await set_site_verdict(session, lead.id, False)
        return "has_site", url

    monkeypatch.setattr(lead_enrich, "analyze_website", analyze_during_verdict)

    result = await _take(service, [_result_record(website="https://site.example")])

    lead = await _get(session_maker)
    assert lead.site_status == SITE_NO_SITE
    assert lead.site_source == SOURCE_MANUAL
    assert result.enrichment.ignored[REASON_MANUAL] == 1


async def test_a_lead_issued_while_sites_are_checked_stays_as_it_is(session_maker, service, monkeypatch) -> None:
    await _add(session_maker, _lead())

    async def analyze_during_issue(url, client=None):
        async with session_maker() as session:
            await issue_next_leads(session, 1)
        return "has_site", url

    monkeypatch.setattr(lead_enrich, "analyze_website", analyze_during_issue)

    result = await _take(service, [_result_record(website="https://site.example")])

    lead = await _get(session_maker)
    assert lead.site_status == SITE_UNKNOWN
    assert lead.crm_status == STATUS_ISSUED
    assert result.enrichment.ignored[REASON_ISSUED] == 1


async def test_a_card_never_counts_as_a_verdict_by_a_person(session_maker, service) -> None:
    await _add(session_maker, _lead())
    await _take(service, [_result_record()])

    # The buttons stay free for a person: a card is not a final answer.
    async with session_maker() as session:
        lead = (await session.scalars(select(Lead))).one()
        _, changed = await set_site_verdict(session, lead.id, True)

    assert changed is True
    assert (await _get(session_maker)).site_source == SOURCE_MANUAL


async def test_unknown_leads_are_ignored_and_counted(session_maker, service) -> None:
    await _add(session_maker, _lead(1))
    other_source = _result_record(1)
    other_source["source"] = "osm"

    result = await _take(service, [_result_record(1), _result_record(99), other_source])

    assert await _lead_count(session_maker) == 1
    assert result.enrichment.updated == 1
    assert result.enrichment.ignored[REASON_UNKNOWN_LEAD] == 2


async def test_a_lead_from_another_source_with_the_same_id_is_not_matched(session_maker, service) -> None:
    await _add(session_maker, _lead(1, source="2gis_scraper"))

    result = await _take(service, [_result_record(1)])

    assert result.enrichment.ignored[REASON_UNKNOWN_LEAD] == 1
    assert (await _get(session_maker)).site_status == SITE_UNKNOWN


async def test_a_repeated_record_counts_once(session_maker, service) -> None:
    await _add(session_maker, _lead(1))

    result = await _take(service, [_result_record(1), _result_record(1, website="https://site.example")])

    assert result.enrichment.updated == 1
    assert result.enrichment.ignored[REASON_REPEATED] == 1
    assert (await _get(session_maker)).site_status == SITE_NO_SITE


async def test_the_same_file_again_changes_nothing(session_maker, service) -> None:
    await _add(session_maker, _lead())
    records = [_result_record(fetched_at=datetime.now(UTC), phones=["+78125550101"])]

    await _take(service, records)
    first = await _get(session_maker)
    result = await _take(service, records)

    lead = await _get(session_maker)
    assert result.enrichment.updated == 0
    assert result.enrichment.ignored[REASON_NOT_NEWER] == 1
    assert lead.site_checked_at == first.site_checked_at
    assert lead.phone == "+78125550101"


async def test_a_newer_reading_of_a_card_applies(session_maker, service) -> None:
    await _add(session_maker, _lead())
    await _take(service, [_result_record(fetched_at=datetime.now(UTC))])

    later = datetime.now(UTC) + timedelta(hours=1)
    result = await _take(service, [_result_record(fetched_at=later, website="https://site.example")])

    assert result.enrichment.updated == 1
    assert (await _get(session_maker)).site_status == SITE_FOUND


async def test_bad_records_are_counted_and_the_others_go_through(session_maker, service, checked_sites) -> None:
    await _add(session_maker, *(_lead(number) for number in range(1, 6)))
    no_flag = _result_record(1)
    del no_flag["parsed"]
    text_flag = {**_result_record(2), "parsed": "true"}
    bad_date = {**_result_record(3), "fetched_at": "вчера"}
    no_date = {**_result_record(4), "fetched_at": None}
    bad_site = _result_record(5, website="javascript:alert(1)")
    not_object = "запись"

    result = await _take(service, [no_flag, text_flag, bad_date, no_date, bad_site, not_object, _result_record(1)])

    ignored = result.enrichment.ignored
    assert ignored[REASON_NO_PARSED_FLAG] == 2
    assert ignored[REASON_BAD_DATE] == 2
    # The unusable address must not read as 'no site': lead 5 stays unknown.
    assert ignored[REASON_BAD_WEBSITE] == 1
    assert (await _get(session_maker, 5)).site_status == SITE_UNKNOWN
    assert (await _get(session_maker, 2)).site_status == SITE_UNKNOWN
    assert result.enrichment.updated == 1
    assert checked_sites == []


async def test_a_crashing_check_counts_as_a_dead_site_like_in_the_pipeline(session_maker, service, monkeypatch) -> None:
    await _add(session_maker, _lead())

    async def crash(url, client=None):
        raise RuntimeError("bug")

    monkeypatch.setattr(lead_enrich, "analyze_website", crash)

    await _take(service, [_result_record(website="https://site.example")])

    assert (await _get(session_maker)).site_status == SITE_DEAD


async def test_a_phone_of_another_lead_makes_a_duplicate(session_maker, service) -> None:
    await _add(session_maker, _lead(1, phone="+78125550101"), _lead(2))

    result = await _take(service, [_result_record(2, phones=["8 812 555-01-01"], website="https://site.example")])

    assert result.enrichment.ignored[REASON_DUPLICATE_PHONE] == 1
    lead = await _get(session_maker, 2)
    assert lead.site_status == SITE_UNKNOWN
    assert lead.phone is None


async def test_two_records_with_one_phone_make_a_duplicate(session_maker, service) -> None:
    await _add(session_maker, _lead(1), _lead(2))

    result = await _take(
        service, [_result_record(1, phones=["+78125550101"]), _result_record(2, phones=["8 812 555-01-01"])]
    )

    assert result.enrichment.updated == 1
    assert result.enrichment.ignored[REASON_DUPLICATE_PHONE] == 1


async def test_its_own_phone_is_not_a_duplicate(session_maker, service) -> None:
    await _add(session_maker, _lead(1, phone="+78125550101"), _lead(2, phone="+78125550101"))

    result = await _take(service, [_result_record(1, phones=["+78125550101"])])

    assert result.enrichment.updated == 1


async def test_a_saved_phone_catches_later_duplicates(session_maker, service) -> None:
    await _add(session_maker, _lead())
    await _take(service, [_result_record(phones=["8 812 555-01-01"])])

    place = RawPlace(
        external_id="osm_1", source="osm", name="Другое название", category="dentist_clinic", phone="+78125550101"
    )
    async with session_maker() as session:
        stats = await save_collected_places(session, [place])

    assert stats["saved_new"] == 0
    assert stats["duplicates_skipped"] == 1


async def test_a_lead_updated_by_a_card_comes_earlier_in_the_batch(session_maker, service) -> None:
    await _add(session_maker, _lead(1, score=90), _lead(2, score=10))

    await _take(service, [_result_record(2, phones=["+79210000002"])])

    async with session_maker() as session:
        issued = await issue_next_leads(session, 2)
        assert await count_new_leads(session) == 0
    assert [lead.name for lead in issued] == ["Организация 2", "Организация 1"]


async def test_the_report_tells_all_the_numbers(session_maker, service) -> None:
    await _add(
        session_maker,
        _lead(1),
        _lead(2),
        _lead(3),
        _lead(4),
        _lead(5),
        _lead(6, crm_status=STATUS_ISSUED),
        _lead(7, site_source=SOURCE_MANUAL),
    )
    records = [
        _result_record(1, website="https://site.example"),
        _result_record(2, website="https://dead.example"),
        _result_record(3, website="https://vk.com/x"),
        _result_record(4),
        _result_record(5, parsed=False),
        _result_record(6),
        _result_record(7),
        _result_record(99),
    ]

    result = await _take(service, records)
    text = format_import_report(result)

    assert result.total == 8
    assert "В файле: <b>8</b>" in text
    assert "Обновлено лидов: <b>4</b>" in text
    assert "Сайтов найдено: <b>3</b> (рабочих: 1, не открываются: 1, соцсеть или агрегатор: 1)" in text
    assert "Без сайта в карточке: <b>1</b>" in text
    assert "Не распознано: <b>1</b>" in text
    assert "Проигнорировано: <b>3</b>" in text
    assert f"{REASON_ISSUED}: 1" in text
    assert f"{REASON_MANUAL}: 1" in text
    assert f"{REASON_UNKNOWN_LEAD}: 1" in text
    assert "В запасе" in text
    assert service[1][0][0] is result  # the admins are told, the same way as about any import


async def test_a_result_is_recorded_in_the_log_of_imports(session_maker, service) -> None:
    await _add(session_maker, _lead())

    await _take(service, [_result_record(1), _result_record(99)])

    async with session_maker() as session:
        log = (await session.scalars(select(ImportLog))).one()
    assert log.ok is True
    assert log.total == 2
    assert log.saved_new == 0
    assert log.rejected == 1


async def test_a_result_is_taken_from_the_inbox_too(session_maker, tmp_path: Path, monkeypatch) -> None:
    await _add(session_maker, _lead())
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "enrich_good.json").write_bytes(_result_file([_result_record(website="https://site.example")]))
    (inbox / "enrich_bad.json").write_bytes(_result_file([_result_record()], checksum="0" * 64))
    monkeypatch.setattr("src.services.lead_import.INBOX_MIN_AGE_SECONDS", 0.0)
    service = ImportService(session_maker, AsyncMock(), inbox, MAX_BYTES, MAX_RECORDS)

    assert await service.scan_inbox() == 2

    assert (inbox / "done" / "enrich_good.json").exists()
    assert (inbox / "failed" / "enrich_bad.json").exists()
    assert (await _get(session_maker)).site_status == SITE_FOUND


async def test_ordinary_files_with_leads_are_imported_as_before(session_maker, service) -> None:
    from src.services.lead_file import build_document

    record = {
        "source": "2gis",
        "source_id": "1",
        "name": "Новая",
        "phones": ["+78125550101"],
        "rubric": "dentist_clinic",
    }
    data = json.dumps(build_document([record], NOW, None), ensure_ascii=False).encode("utf-8")

    result = await service[0].import_bytes(data, "leads.json", "telegram")

    assert result.saved_new == 1
    assert result.enrichment is None


def test_a_result_written_by_a_collector_has_the_agreed_fields(tmp_path: Path) -> None:
    path = lead_export.write_enrich_result(tmp_path, [_result_record()])

    assert set(json.loads(path.read_text(encoding="utf-8"))["records"][0]) == {
        "source",
        "source_id",
        "parsed",
        "phones",
        "website",
        "socials",
        "email",
        "fetched_at",
    }


async def test_old_databases_get_the_counter_of_checks(tmp_path: Path) -> None:
    from sqlalchemy import text

    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "old.db")))
    await init_db(engine)
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE collector_state DROP COLUMN scraper_check_runs"))
    await init_db(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        assert (await claim_scraper_check(session, 3, CHECK_WINDOW, NOW))[0] is None
    await engine.dispose()


# ---------- the command line ----------


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The command line with settings for a temporary folder and a browser of the test."""
    settings = Settings(
        _env_file=None,
        COLLECTOR_DB_PATH=str(tmp_path / "data" / "collector.db"),
        COLLECTOR_EXPORT_DIR=str(tmp_path / "exports"),
        COLLECTOR_PROVIDERS="2gis",
        COLLECT_QUERIES="dentist_clinic",
        COLLECT_DISTRICTS="Центральный",
    )
    monkeypatch.setattr(collector_cli, "settings", settings)
    monkeypatch.setattr(collector_cli, "settings_error", None)
    monkeypatch.setattr(collector_cli, "CHECK_LOG_PATH", str(tmp_path / "logs" / "scraper_check.json"))
    holder = MagicMock()
    holder.tmp = tmp_path
    holder.options = []

    def provider(options: ScraperOptions) -> TwoGisScraperProvider:
        holder.options.append(options)
        return _provider(holder.session, options)

    monkeypatch.setattr(collector_cli, "_scraper_provider", provider)
    return holder


def _db_counts(tmp_path: Path) -> tuple[int, ProviderState | None]:
    async def read():
        engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "data" / "collector.db")))
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            count = int((await session.execute(select(func.count(Lead.id)))).scalar_one())
            state = (await get_provider_states(session)).get("2gis_scraper")
        await engine.dispose()
        return count, state

    return asyncio.run(read())


@pytest.mark.parametrize("argv", [["--check"], ["--enrich", "f.json"]])
def test_the_modes_need_a_person(argv: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        collector_cli.main(argv)

    assert stopped.value.code == 2
    assert "--assisted" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--assisted", "--check", "--enrich", "f.json"],
        ["--assisted", "--check", "--export-only"],
        ["--assisted", "--enrich", "f.json", "--no-export"],
    ],
)
def test_the_modes_do_not_mix(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        collector_cli.main(argv)


def test_check_from_the_command_line(cli, capsys) -> None:
    cli.session = Session([_page((SEARCH_API, SEARCH_BODY)), _page((CARD_API, CARD_BODY))])

    assert collector_cli.main(["--assisted", "--check"]) == collector_cli.EXIT_OK

    out = capsys.readouterr().out
    assert "Название: Дент Про" in out
    assert "Адрес: Невский проспект, 1" in out
    assert "Телефоны: 8 (921) 000-00-02" in out
    assert "Сайт: https://dent.example" in out
    assert "Соцсети: https://vk.com/dent" in out
    assert "Ссылка: https://2gis.ru/spb/firm/70000001" in out
    assert "scraper_check.json" in out
    assert "осталось за 24 часа: 2" in out
    assert (cli.tmp / "logs" / "scraper_check.json").exists()
    assert not (cli.tmp / "exports").exists()
    assert cli.options[0].max_pages_per_run == CHECK_MAX_PAGES
    assert cli.options[0].headless is False
    assert _db_counts(cli.tmp) == (0, None)
    assert "/search/" in cli.session.visited[0]
    assert "%D0%A6%D0%B5%D0%BD%D1%82%D1%80" in cli.session.visited[0]  # the district from the settings


def test_a_fourth_check_is_refused_with_its_own_exit_code(cli, capsys) -> None:
    for _ in range(CHECK_MAX_RUNS):
        cli.session = Session([_page()])
        assert collector_cli.main(["--assisted", "--check"]) == collector_cli.EXIT_OK

    cli.session = Session([])
    assert collector_cli.main(["--assisted", "--check"]) == collector_cli.EXIT_BLOCKED

    assert cli.session.visited == []
    assert "не больше 3" in capsys.readouterr().out


def test_check_says_why_there_is_no_field(cli, capsys) -> None:
    cli.session = Session([_page(("https://api.2gis.ru/other", {"data": 1}))])

    collector_cli.main(["--assisted", "--check"])

    out = capsys.readouterr().out
    assert "Поиск: нет ответа в ожидаемом формате" in out
    assert "Сайт: не распознано" in out
    assert "нет в карточке" not in out


def test_enrich_from_the_command_line(cli, capsys) -> None:
    request = cli.tmp / "enrich_request.json"
    request.write_bytes(_file(KIND_ENRICH_REQUEST, [_request_record(1), _request_record(2)]))
    cli.session = Session([_card_page("70001"), _page()])

    assert collector_cli.main(["--assisted", "--enrich", str(request)]) == collector_cli.EXIT_OK

    out = capsys.readouterr().out
    assert "Карточек прочитано 1, не распознано 1" in out
    files = list((cli.tmp / "exports").glob("enrich_*.json"))
    assert len(files) == 1
    assert str(files[0]) in out
    count, state = _db_counts(cli.tmp)
    assert count == 0
    assert state.last_run_at is not None
    assert cli.options[0].assisted is True


def test_enrich_waits_for_the_rest_after_a_collection(cli, capsys) -> None:
    request = cli.tmp / "enrich_request.json"
    request.write_bytes(_file(KIND_ENRICH_REQUEST, [_request_record(1)]))
    cli.session = Session([_card_page("70001")])
    assert collector_cli.main(["--assisted", "--enrich", str(request)]) == collector_cli.EXIT_OK

    cli.session = Session([])
    assert collector_cli.main(["--assisted", "--enrich", str(request)]) == collector_cli.EXIT_BLOCKED

    assert cli.session.visited == []
    assert "не открывались" in capsys.readouterr().out


def test_a_block_during_enrich_gives_its_exit_code(cli) -> None:
    request = cli.tmp / "enrich_request.json"
    request.write_bytes(_file(KIND_ENRICH_REQUEST, [_request_record(1)]))
    cli.session = Session([_page((CARD_API, {"meta": {"code": 429}}))])

    assert collector_cli.main(["--assisted", "--enrich", str(request)]) == collector_cli.EXIT_BLOCKED

    assert _db_counts(cli.tmp)[1].consecutive_blocks == 1


@pytest.mark.parametrize(
    "content",
    [b"not json", json.dumps({"kind": "enrich_result"}).encode(), _file(KIND_ENRICH_REQUEST, [], checksum="0")],
)
def test_a_wrong_request_file_opens_no_browser(cli, capsys, content: bytes) -> None:
    request = cli.tmp / "enrich_request.json"
    request.write_bytes(content)
    cli.session = Session([])

    assert collector_cli.main(["--assisted", "--enrich", str(request)]) == collector_cli.EXIT_ERROR

    assert "не принят" in capsys.readouterr().out
    assert cli.session.visited == []
    assert cli.options == []


def test_a_missing_request_file_is_explained(cli, capsys) -> None:
    assert collector_cli.main(["--assisted", "--enrich", str(cli.tmp / "нет.json")]) == collector_cli.EXIT_ERROR

    assert "не прочитан" in capsys.readouterr().out


def test_the_failure_of_a_browser_is_an_error(cli, capsys) -> None:
    request = cli.tmp / "enrich_request.json"
    request.write_bytes(_file(KIND_ENRICH_REQUEST, [_request_record(1)]))

    class Broken(Session):
        async def start(self) -> None:
            raise ProviderUnavailableError("браузер не запустился")

    cli.session = Broken([])

    assert collector_cli.main(["--assisted", "--enrich", str(request)]) == collector_cli.EXIT_ERROR

    assert "браузер не запустился" in capsys.readouterr().out
