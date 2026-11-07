"""Tests for looking for sites and contacts of leads on Google with a person at the computer. No network, no browser."""

import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src import collector_cli
from src.config import Settings
from src.database.models import STATUS_NEW, Lead, ProviderState
from src.database.repo import MAX_CONSECUTIVE_BLOCKS, enable_provider, get_provider_states, init_db
from src.services import google_assisted, lead_enrich
from src.services.api_client import FORMAT_NOT_RECOGNIZED_TEXT, ProviderNoConnectionError, ProviderUnavailableError
from src.services.collector import REST_REASON
from src.services.google_assisted import (
    GOOGLE_PROVIDER_NAME,
    MAX_PAUSE_SECONDS,
    MIN_PAUSE_SECONDS,
    OWNER_POLL_SECONDS,
    REASON_NO_NAME,
    GoogleOptions,
    GoogleRun,
    SiteVisit,
    run_google_enrich,
)
from src.services.lead_enrich import REASON_KNOWN_BETTER
from src.services.lead_file import (
    ENRICH_GOOGLE_SCHEMA_VERSION,
    ENRICH_SCHEMA_VERSION,
    FOUND_BY_GOOGLE,
    KIND_ENRICH_RESULT,
    REASON_BAD_FOUND_BY,
    EnrichRequest,
    EnrichRequestItem,
    LeadFileError,
    build_enrich_document,
    enrich_result_record,
    parse_enrich_result,
)
from src.services.lead_import import ImportService
from src.services.scraper import google_site
from src.services.scraper.browser import PageSnapshot
from src.services.site_status import (
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SITE_FOUND,
    SITE_NO_SITE,
    SITE_NOT_FOUND,
    SITE_SOCIAL_ONLY,
    SITE_UNKNOWN,
    SOURCE_2GIS_CARD,
    SOURCE_GOOGLE,
)

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://www.google.com/search?q=x&hl=ru"
TODAY = date(2026, 11, 7)
OPTIONS = GoogleOptions(max_per_run=15, max_per_day=60, min_hours_between_runs=3.0, wait_minutes=5.0)
MAX_BYTES = 1024 * 1024


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _page(name: str, url: str = SEARCH_URL, title: str = "Улыбка - Поиск в Google", status: int = 200) -> PageSnapshot:
    return PageSnapshot(status=status, final_url=url, title=title, html=_fixture(name))


RESULTS = _page("google_results.html")
NOTHING = _page("google_nothing_found.html")
CAPTCHA = _page("google_captcha.html", url="https://www.google.com/sorry/index?continue=x", title="x", status=429)
CONSENT = _page("google_consent.html", url="https://consent.google.com/ml?continue=x", title="Прежде чем перейти")
UNKNOWN = _page("google_unknown.html", title="Google")


class Browser:
    """Answers come from a list; after a captcha or a question the window shows `after`."""

    def __init__(self, pages: list[PageSnapshot | Exception], after: PageSnapshot | None = None) -> None:
        self.pages = list(pages)
        self.after = after or RESULTS
        self.visited: list[str] = []
        self.started = 0
        self.closed = 0
        self.fronted = 0

    async def start(self) -> None:
        self.started += 1

    async def visit(self, url: str, wants_body) -> PageSnapshot:
        assert wants_body(url) is False  # nothing is read from the network of the page
        self.visited.append(url)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    async def read_page(self) -> PageSnapshot | None:
        if isinstance(self.after, Exception):
            raise self.after
        return self.after

    async def bring_to_front(self) -> None:
        self.fronted += 1

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
    def pauses(self) -> list[float]:
        return [delay for delay in self.delays if delay != OWNER_POLL_SECONDS]


def _item(number: int = 1, name: str | None = "Стоматология Улыбка") -> EnrichRequestItem:
    return EnrichRequestItem(
        "2gis",
        f"7000{number}_abc",
        f"https://2gis.ru/spb/firm/7000{number}",
        f"7000{number}",
        name=name,
        address=f"Невский проспект, {number}",
        category="dentist_clinic",
    )


SITE = google_site.SiteContacts(phones=["+78121234567"], socials=["https://t.me/ulybka_dent_spb"], email="a@ulybka.ru")


class Sites:
    def __init__(self, visit: SiteVisit | None = None) -> None:
        self.visit = visit or SiteVisit(SITE)
        self.opened: list[str] = []

    async def __call__(self, url: str) -> SiteVisit:
        self.opened.append(url)
        return self.visit


async def _run(
    session_maker,
    browser: Browser,
    items: list[EnrichRequestItem],
    tmp_path: Path,
    options: GoogleOptions = OPTIONS,
    time: Time | None = None,
    sites: Sites | None = None,
    notes: list[str] | None = None,
    today: date = TODAY,
) -> GoogleRun:
    run = GoogleRun()
    time = time or Time()
    await run_google_enrich(
        session_maker,
        EnrichRequest(total=len(items), items=items),
        options,
        tmp_path,
        run,
        session_factory=lambda: browser,
        fetch_site=sites or Sites(),
        sleep=time.sleep,
        clock=time.clock,
        notify=(notes if notes is not None else []).append,
        today=lambda: today,
    )
    return run


async def _state(session_maker) -> ProviderState | None:
    async with session_maker() as session:
        return (await get_provider_states(session)).get(GOOGLE_PROVIDER_NAME)


def _document(run: GoogleRun) -> dict:
    assert run.path is not None
    return json.loads(run.path.read_text(encoding="utf-8"))


# ---------- reading pages of Google ----------


def test_results_are_read_and_unwrapped_from_google_links() -> None:
    page = google_site.read_results_page(200, SEARCH_URL, "x", _fixture("google_results.html"))

    assert page.kind == google_site.PAGE_RESULTS
    assert page.layout == "#rso"
    urls = [result.url for result in page.results]
    # Links of Google itself (pictures, the policy in the footer) are not results.
    assert urls == [
        "https://2gis.ru/spb/firm/70000001234",
        "https://spb.zoon.ru/medical/ulybka/",
        "https://www.ulybka-dent.ru/uslugi/implantaciya/?utm_source=google",
        "https://vk.com/ulybka_spb",
        "https://www.instagram.com/ulybka.dent/?hl=ru",
        "https://t.me/ulybka_dent_spb",
    ]
    assert page.results[2].title == "Стоматология «Улыбка» — лечение и имплантация зубов в СПб"


def test_the_older_layout_is_read_by_a_fallback() -> None:
    page = google_site.read_results_page(200, SEARCH_URL, "x", _fixture("google_results_old.html"))

    assert page.kind == google_site.PAGE_RESULTS
    assert page.layout == "#search"
    assert page.results[0].url == "http://romashka-cafe.spb.ru/"


def test_titled_links_without_a_known_block_are_trusted_only_on_a_page_of_google() -> None:
    html = '<html><body><div class="new"><a href="https://ulybka-dent.ru/"><h3>Улыбка</h3></a></div></body></html>'

    on_google = google_site.read_results_page(200, SEARCH_URL, "Улыбка - Поиск в Google", html)
    elsewhere = google_site.read_results_page(200, SEARCH_URL, "Какая-то страница", html)

    assert on_google.kind == google_site.PAGE_RESULTS
    assert on_google.layout == "page"
    assert elsewhere.kind == google_site.PAGE_UNRECOGNIZED


def test_nothing_found_is_not_the_same_as_not_understood() -> None:
    nothing = google_site.read_results_page(200, SEARCH_URL, "x", _fixture("google_nothing_found.html"))
    empty_block = google_site.read_results_page(200, SEARCH_URL, "x", '<div id="rso"></div>')
    unknown = google_site.read_results_page(200, SEARCH_URL, "Google", _fixture("google_unknown.html"))

    assert nothing.kind == google_site.PAGE_NOTHING_FOUND
    assert nothing.results == []
    # An empty block without the words of an empty search may be a new layout: not trusted.
    assert empty_block.kind == google_site.PAGE_UNRECOGNIZED
    assert unknown.kind == google_site.PAGE_UNRECOGNIZED


@pytest.mark.parametrize(
    ("status", "url", "html", "expected"),
    [
        (429, "https://www.google.com/sorry/index?continue=x", "", google_site.PAGE_CAPTCHA),
        (200, SEARCH_URL, _fixture("google_captcha.html"), google_site.PAGE_CAPTCHA),
        (200, "https://consent.google.com/ml?continue=x", "", google_site.PAGE_CONSENT),
        (200, SEARCH_URL, _fixture("google_consent.html"), google_site.PAGE_CONSENT),
        (429, SEARCH_URL, _fixture("google_results.html"), google_site.PAGE_REFUSED),
        (403, SEARCH_URL, "", google_site.PAGE_REFUSED),
        (503, SEARCH_URL, "", google_site.PAGE_SERVER_ERROR),
    ],
)
def test_refusals_and_questions_come_before_results(status: int, url: str, html: str, expected: str) -> None:
    assert google_site.read_results_page(status, url, "x", html).kind == expected


def test_a_banner_about_cookies_over_results_does_not_stop_the_search() -> None:
    html = _fixture("google_results.html").replace(
        "</body>", '<form action="https://consent.google.com/save"><button>OK</button></form></body>'
    )

    assert google_site.read_results_page(200, SEARCH_URL, "x", html).kind == google_site.PAGE_RESULTS


# ---------- is it the site of this organization ----------


def test_the_name_is_compared_by_its_own_words() -> None:
    assert google_site.name_words("ООО «Стоматология Улыбка»") == ["улыбка"]
    # A name of generic words only has nothing better to compare by.
    assert google_site.name_words("Стоматология") == ["стоматология"]
    # The words of the niche do not count either.
    assert google_site.name_words("Ромашка кафе-бар", "Кафе и бары") == ["ромашка"]


@pytest.mark.parametrize(
    ("name", "title", "label", "expected"),
    [
        ("Улыбка", "Главная", "ulybka-dent", 1.0),
        ("Улыбка", "Стоматология «Улыбка» в СПб", "smile-clinic", 1.0),
        ("Дент Мастер", "Главная", "dentmaster", 1.0),
        ("Дент Мастер", "Главная", "dentclinic", 0.5),
        ("Улыбка", "Главная", "zuby-spb", 0.0),
        # A title of a page about the organization on someone else's site: only the address counts.
        ("Улыбка", "Улыбка: отзывы, цены, запись", "mediportal", 0.0),
    ],
)
def test_similarity_of_a_name(name: str, title: str, label: str, expected: float) -> None:
    assert google_site.similarity(google_site.name_words(name), title, label) == pytest.approx(expected)


def test_the_site_is_chosen_past_catalogs_and_networks() -> None:
    results = google_site.read_results_page(200, SEARCH_URL, "x", _fixture("google_results.html")).results

    choice = google_site.choose(results, "Стоматология Улыбка", 0.6, "Стоматологии и клиники")

    # The main page, not the inner one of the result.
    assert choice.website == "https://www.ulybka-dent.ru"
    # Telegram is the sixth result: only the first five are looked at.
    assert choice.socials == ["https://vk.com/ulybka_spb", "https://instagram.com/ulybka.dent"]


def test_a_site_of_another_organization_is_not_taken() -> None:
    results = [
        google_site.SearchResult("https://zuby-spb.ru/", "Стоматология на Невском"),
        google_site.SearchResult("https://2gis.ru/spb/firm/1", "Улыбка — 2ГИС"),
        google_site.SearchResult("https://vk.com/other", "Другая клиника | ВКонтакте"),
        google_site.SearchResult("https://vk.com/share.php?url=x", "Улыбка"),
    ]

    choice = google_site.choose(results, "Стоматология Улыбка", 0.6)

    assert choice == google_site.Choice(website=None, socials=[])


def test_only_the_top_five_are_looked_at() -> None:
    others = [google_site.SearchResult(f"https://other{number}.ru/", "Другое") for number in range(5)]
    late = google_site.SearchResult("https://ulybka-dent.ru/", "Улыбка")

    assert google_site.choose([*others, late], "Улыбка", 0.6).website is None
    assert google_site.choose([late, *others], "Улыбка", 0.6).website == "https://ulybka-dent.ru"


def test_the_search_text_names_the_city_once() -> None:
    assert google_site.search_text("Улыбка", "Невский проспект, 10", None) == (
        "Улыбка Невский проспект, 10 Санкт-Петербург"
    )
    assert google_site.search_text("Улыбка", "Санкт-Петербург, Невский 10", None) == (
        "Улыбка Санкт-Петербург, Невский 10"
    )
    assert google_site.search_text("Улыбка", None, "Центральный") == "Улыбка Центральный Санкт-Петербург"


# ---------- contacts on a site ----------


def test_contacts_are_taken_from_links_and_text_of_the_site() -> None:
    contacts = google_site.extract_site_contacts(_fixture("company_site.html"))

    # Not from the script: '+7 999 111-22-33' and 'bugs@sentry.io' are code, not contacts.
    assert contacts.phones == ["+78121234567", "+79217654321"]
    assert contacts.email == "info@ulybka-dent.ru"
    # Buttons to share are not pages; WhatsApp keeps its number without the ready text.
    assert contacts.socials == [
        "https://vk.com/ulybka_spb",
        "https://instagram.com/ulybka.dent",
        "https://t.me/ulybka_dent_spb",
        "https://wa.me/79217654321",
    ]


def test_a_page_without_contacts_gives_nothing() -> None:
    contacts = google_site.extract_site_contacts("<html><body><img src='a@2x.png'><p>Привет</p></body></html>")

    assert contacts == google_site.SiteContacts(phones=[], socials=[], email=None)


# ---------- a run ----------


async def test_a_run_writes_a_file_of_google_results(session_maker, tmp_path: Path) -> None:
    browser = Browser([RESULTS, NOTHING])
    time = Time()
    sites = Sites()

    run = await _run(session_maker, browser, [_item(1), _item(2, name="Зубная фея")], tmp_path, time=time, sites=sites)

    assert run.searched == 2
    assert browser.visited[0].startswith("https://www.google.com/search?q=")
    assert "%D0%A1%D0%B0%D0%BD%D0%BA%D1%82" in browser.visited[0]  # Санкт-Петербург
    assert sites.opened == ["https://www.ulybka-dent.ru"]
    # One pause, between the two searches, and a long one.
    assert len(time.pauses) == 1
    assert MIN_PAUSE_SECONDS <= time.pauses[0] <= MAX_PAUSE_SECONDS
    assert browser.closed == 1

    assert run.path is not None and run.path.name.startswith("google_enrich_")
    document = _document(run)
    assert document["kind"] == KIND_ENRICH_RESULT
    assert document["schema_version"] == ENRICH_GOOGLE_SCHEMA_VERSION
    first, second = document["records"]
    assert first["found_by"] == second["found_by"] == FOUND_BY_GOOGLE
    assert first["parsed"] is True
    assert first["website"] == "https://www.ulybka-dent.ru"
    assert first["phones"] == ["+78121234567"]
    assert first["email"] == "a@ulybka.ru"
    assert first["socials"] == [
        "https://vk.com/ulybka_spb",
        "https://instagram.com/ulybka.dent",
        "https://t.me/ulybka_dent_spb",
    ]
    assert (second["parsed"], second["website"], second["socials"]) == (True, None, [])

    # What the collector writes, the bot accepts.
    parsed = parse_enrich_result(run.path.read_bytes(), MAX_BYTES, 100)
    assert len(parsed.records) == 2 and not parsed.rejected


async def test_a_captcha_stops_the_run_waits_for_the_owner_and_counts_as_a_block(session_maker, tmp_path) -> None:
    browser = Browser([RESULTS, CAPTCHA, RESULTS], after=RESULTS)
    notes: list[str] = []

    run = await _run(session_maker, browser, [_item(1), _item(2), _item(3)], tmp_path, notes=notes)

    assert run.searched == 2
    assert len(browser.visited) == 2  # nothing after the captcha
    assert run.blocked == google_assisted.CAPTCHA_TEXT
    assert run.owner_passed is True
    assert browser.fronted == 1
    assert any("Пройдите проверку" in note for note in notes)
    # What was found before is in the file.
    assert [record["source_id"] for record in _document(run)["records"]] == ["70001_abc"]
    state = await _state(session_maker)
    assert state is not None and state.consecutive_blocks == 1 and state.blocked_until is not None
    assert run.problems and "капчу" in run.problems[0]


async def test_a_captcha_nobody_passed_ends_the_run_after_the_wait(session_maker, tmp_path) -> None:
    time = Time()
    browser = Browser([CAPTCHA], after=CAPTCHA)

    run = await _run(session_maker, browser, [_item(1)], tmp_path, time=time)

    assert run.owner_passed is False
    assert run.blocked == google_assisted.CAPTCHA_TEXT
    assert sum(time.delays) >= OPTIONS.wait_minutes * 60
    assert run.path is None
    assert browser.closed == 1


async def test_a_closed_window_ends_the_wait(session_maker, tmp_path) -> None:
    browser = Browser([CAPTCHA], after=ProviderUnavailableError("окно браузера закрыто"))

    run = await _run(session_maker, browser, [_item(1)], tmp_path)

    assert run.owner_passed is False
    assert run.blocked == google_assisted.CAPTCHA_TEXT


async def test_a_question_about_cookies_stops_the_run_but_is_not_a_block(session_maker, tmp_path) -> None:
    browser = Browser([RESULTS, CONSENT], after=RESULTS)

    run = await _run(session_maker, browser, [_item(1), _item(2)], tmp_path)

    assert run.blocked is None
    assert run.stopped is not None and "cookies" in run.stopped
    assert run.owner_passed is True
    assert len(_document(run)["records"]) == 1
    state = await _state(session_maker)
    assert state is not None and state.consecutive_blocks == 0 and state.blocked_until is None


async def test_too_many_requests_is_a_block(session_maker, tmp_path) -> None:
    browser = Browser([_page("google_results.html", status=429)])

    run = await _run(session_maker, browser, [_item(1)], tmp_path)

    assert run.blocked == "Google ответил кодом 429"
    assert (await _state(session_maker)).consecutive_blocks == 1


async def test_a_page_not_understood_fails_loudly_and_is_not_a_block(session_maker, tmp_path) -> None:
    browser = Browser([RESULTS, UNKNOWN, RESULTS])

    run = await _run(session_maker, browser, [_item(1), _item(2), _item(3)], tmp_path)

    assert run.failed == FORMAT_NOT_RECOGNIZED_TEXT
    assert run.blocked is None
    assert run.problems and "не распознана" in run.problems[0]
    # The second lead is not in the file: it was not searched, and must not look like a lead without a site.
    assert [record["source_id"] for record in _document(run)["records"]] == ["70001_abc"]
    state = await _state(session_maker)
    assert state is not None and state.consecutive_blocks == 0 and state.blocked_until is None


async def test_three_blocks_in_a_row_switch_google_off_until_the_owner_turns_it_on(session_maker, tmp_path) -> None:
    options = replace(OPTIONS, min_hours_between_runs=0.0, backoff_hours=0.0)
    for _ in range(MAX_CONSECUTIVE_BLOCKS):
        run = await _run(session_maker, Browser([CAPTCHA], after=RESULTS), [_item(1)], tmp_path, options=options)
        assert run.blocked

    state = await _state(session_maker)
    assert state is not None and state.disabled_at is not None

    browser = Browser([RESULTS])
    refused = await _run(session_maker, browser, [_item(1)], tmp_path, options=replace(options, ignore_interval=True))
    assert refused.refused is not None and "отключён" in refused.refused
    assert browser.started == 0 and browser.visited == []

    async with session_maker() as session:
        await enable_provider(session, GOOGLE_PROVIDER_NAME)
    again = await _run(session_maker, Browser([RESULTS]), [_item(1)], tmp_path, options=options)
    assert again.refused is None and again.searched == 1


async def test_a_clean_run_clears_blocks_but_not_one_with_the_interval_skipped(session_maker, tmp_path) -> None:
    options = replace(OPTIONS, min_hours_between_runs=0.0, backoff_hours=0.0)
    await _run(session_maker, Browser([CAPTCHA], after=RESULTS), [_item(1)], tmp_path, options=options)

    await _run(session_maker, Browser([RESULTS]), [_item(1)], tmp_path, options=replace(options, ignore_interval=True))
    assert (await _state(session_maker)).consecutive_blocks == 1

    await _run(session_maker, Browser([RESULTS]), [_item(1)], tmp_path, options=options)
    assert (await _state(session_maker)).consecutive_blocks == 0


async def test_no_more_leads_than_the_limit_of_a_run(session_maker, tmp_path) -> None:
    items = [_item(number) for number in range(1, 21)]
    browser = Browser([NOTHING] * 20)

    run = await _run(session_maker, browser, items, tmp_path, options=replace(OPTIONS, max_per_run=15))

    assert (run.planned, run.over_limit, run.searched) == (15, 5, 15)
    assert len(browser.visited) == 15


def test_the_limit_of_a_run_cannot_be_set_above_twenty() -> None:
    with pytest.raises(ValidationError):
        Settings(GOOGLE_MAX_PER_RUN=21)
    with pytest.raises(ValidationError):
        Settings(GOOGLE_NAME_MATCH_THRESHOLD=0.3)
    assert Settings(GOOGLE_MIN_HOURS_BETWEEN_RUNS=0).GOOGLE_MIN_HOURS_BETWEEN_RUNS == 0


async def test_the_limit_of_a_day_holds_across_runs_and_starts_over_the_next_day(session_maker, tmp_path) -> None:
    options = replace(OPTIONS, max_per_day=3, min_hours_between_runs=0.0)
    items = [_item(number) for number in range(1, 6)]

    first = await _run(session_maker, Browser([NOTHING] * 5), items[:2], tmp_path, options=options)
    second = await _run(session_maker, Browser([NOTHING] * 5), items, tmp_path, options=options)
    third_browser = Browser([NOTHING])
    third = await _run(session_maker, third_browser, items, tmp_path, options=options)
    next_day = await _run(
        session_maker, Browser([NOTHING] * 5), items, tmp_path, options=options, today=TODAY + timedelta(days=1)
    )

    assert (first.searched, first.day_left) == (2, 1)
    assert second.searched == 1
    assert second.stopped is not None and "за сутки" in second.stopped
    assert third.searched == 0 and third_browser.visited == []
    assert next_day.searched == 3


async def test_a_run_that_never_reached_google_does_not_use_up_the_interval(session_maker, tmp_path) -> None:
    options = replace(OPTIONS, max_per_day=1)
    await _run(session_maker, Browser([NOTHING]), [_item(1)], tmp_path, options=replace(options, ignore_interval=True))
    used = (await _state(session_maker)).last_run_at

    run = await _run(session_maker, Browser([]), [_item(1)], tmp_path, options=replace(options, ignore_interval=True))

    assert run.searched == 0
    assert (await _state(session_maker)).last_run_at == used


async def test_no_connection_is_not_a_block_and_not_a_run(session_maker, tmp_path) -> None:
    browser = Browser([ProviderNoConnectionError("нет связи")])

    run = await _run(session_maker, browser, [_item(1)], tmp_path)

    assert run.failed == google_assisted.NO_CONNECTION_TEXT
    state = await _state(session_maker)
    assert state is not None and state.last_run_at is None and state.consecutive_blocks == 0


async def test_the_interval_between_runs_is_kept_apart_from_2gis(session_maker, tmp_path) -> None:
    async with session_maker() as session:
        session.add(ProviderState(name="2gis_scraper", consecutive_blocks=0, last_run_at=datetime.now(UTC)))
        await session.commit()

    first = await _run(session_maker, Browser([NOTHING]), [_item(1)], tmp_path)
    second = await _run(session_maker, Browser([NOTHING]), [_item(1)], tmp_path)
    forced = await _run(
        session_maker, Browser([NOTHING]), [_item(1)], tmp_path, options=replace(OPTIONS, ignore_interval=True)
    )

    assert first.refused is None and first.searched == 1
    assert second.refused == REST_REASON and second.retry_at is not None
    assert forced.refused is None and forced.searched == 1


async def test_leads_without_a_name_are_not_searched(session_maker, tmp_path) -> None:
    browser = Browser([NOTHING])

    run = await _run(session_maker, browser, [_item(1, name=None), _item(2)], tmp_path)

    assert run.rejected[REASON_NO_NAME] == 1
    assert len(browser.visited) == 1


async def test_the_browser_is_closed_whatever_happens(session_maker, tmp_path) -> None:
    class Broken(Browser):
        async def start(self) -> None:
            raise ProviderUnavailableError("браузер не запустился")

    browser = Broken([])

    run = await _run(session_maker, browser, [_item(1)], tmp_path)

    assert run.failed == "браузер не запустился"
    assert browser.closed == 1
    # Nothing was sent: neither the interval nor the day are used up.
    state = await _state(session_maker)
    assert state is not None and state.last_run_at is None and not state.requests_day_used


# ---------- the bot takes the file ----------


def _lead(number: int, **overrides) -> Lead:
    values = {
        "external_id": f"2gis_7000{number}_abc",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": "dentist_clinic",
        "address": f"Невский проспект, {number}",
        "site_status": SITE_UNKNOWN,
        "score": 40,
        "crm_status": STATUS_NEW,
        "source_url": f"https://2gis.ru/spb/firm/7000{number}",
    }
    values.update(overrides)
    return Lead(**values)


def _google_file(records: list[dict], version: int = ENRICH_GOOGLE_SCHEMA_VERSION) -> bytes:
    document = build_enrich_document(KIND_ENRICH_RESULT, records, datetime.now(UTC), version)
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _google_record(number: int, **fields) -> dict:
    return enrich_result_record(
        "2gis", f"7000{number}_abc", datetime.now(UTC), parsed=True, found_by=FOUND_BY_GOOGLE, **fields
    )


@pytest.fixture
def no_site_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_analyze(url, client=None):
        return ("social_only", url) if "vk.com" in url else ("has_site", url)

    monkeypatch.setattr(lead_enrich, "analyze_website", fake_analyze)


async def test_the_bot_applies_a_google_file(session_maker, tmp_path, no_site_checks) -> None:
    async with session_maker() as session:
        session.add_all(
            [
                _lead(1),
                _lead(2),
                _lead(3),
                # A card already said there is no site: not finding one on Google says less.
                _lead(4, site_status=SITE_NO_SITE, site_source=SOURCE_2GIS_CARD),
            ]
        )
        await session.commit()
    data = _google_file(
        [
            _google_record(1, website="https://ulybka-dent.ru", phones=["+78121234567"]),
            _google_record(2, socials=["https://vk.com/ulybka_spb"]),
            _google_record(3),
            _google_record(4),
        ]
    )
    service = ImportService(session_maker, _no_report, tmp_path / "inbox", MAX_BYTES, 100)

    result = await service.import_bytes(data, "google_enrich.json", "telegram")

    assert result.ok, result.error
    stats = result.enrichment
    assert stats is not None and stats.from_google
    assert (stats.updated, stats.sites_working, stats.sites_social, stats.not_found) == (3, 1, 1, 1)
    assert stats.ignored[REASON_KNOWN_BETTER] == 1
    async with session_maker() as session:
        leads = {lead.external_id: lead for lead in (await session.scalars(select(Lead))).all()}
    found, social, nothing, card = (leads[f"2gis_7000{number}_abc"] for number in range(1, 5))
    assert (found.site_status, found.site_source, found.site_confidence) == (
        SITE_FOUND,
        SOURCE_GOOGLE,
        CONFIDENCE_MEDIUM,
    )
    assert found.phone == "+78121234567"
    assert (social.site_status, social.socials) == (SITE_SOCIAL_ONLY, "https://vk.com/ulybka_spb")
    assert (nothing.site_status, nothing.site_confidence) == (SITE_NOT_FOUND, CONFIDENCE_LOW)
    assert (card.site_status, card.site_source) == (SITE_NO_SITE, SOURCE_2GIS_CARD)


async def _no_report(result, exclude_chat) -> None:
    return None


def test_the_version_of_a_file_must_match_where_its_records_came_from() -> None:
    card_record = enrich_result_record("2gis", "70001_abc", datetime.now(UTC), parsed=True)

    with pytest.raises(LeadFileError, match="версия"):
        parse_enrich_result(_google_file([_google_record(1)], version=ENRICH_SCHEMA_VERSION), MAX_BYTES, 100)
    with pytest.raises(LeadFileError, match="версия"):
        parse_enrich_result(_google_file([card_record], version=ENRICH_GOOGLE_SCHEMA_VERSION), MAX_BYTES, 100)
    # Files from cards are read as before.
    old = parse_enrich_result(_google_file([card_record], version=ENRICH_SCHEMA_VERSION), MAX_BYTES, 100)
    assert old.records[0].found_by is None


def test_an_unknown_origin_of_a_record_is_refused() -> None:
    record = {**_google_record(1), "found_by": "bing"}

    parsed = parse_enrich_result(_google_file([record]), MAX_BYTES, 100)

    assert parsed.rejected[REASON_BAD_FOUND_BY] == 1


# ---------- the command line ----------


@pytest.mark.parametrize(
    "argv",
    [
        ["--google"],
        ["--assisted", "--google"],
        ["--google", "--enrich", "f.json"],
        ["--enrich-latest"],
    ],
)
def test_google_needs_a_person_and_a_request(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        collector_cli._parse_args(argv)


def test_the_newest_request_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exports, downloads = tmp_path / "exports", tmp_path / "home" / "Downloads"
    exports.mkdir()
    downloads.mkdir(parents=True)
    older = exports / "enrich_request_20261106_100000.json"
    newer = downloads / "enrich_request_20261107_100000.json"
    (exports / "enrich_20261107_110000.json").write_text("{}")
    older.write_text("{}")
    newer.write_text("{}")
    older.touch()
    newer.touch()

    os.utime(older, (1, 1))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    found = collector_cli._latest_request(Settings(COLLECTOR_EXPORT_DIR=str(exports)))

    assert found == newer


async def test_the_command_runs_google_and_logs_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    request = tmp_path / "enrich_request_1.json"
    request.write_text(
        json.dumps(
            build_enrich_document(
                "enrich_request",
                [
                    {
                        "source": "2gis",
                        "source_id": "70001_abc",
                        "source_url": "https://2gis.ru/spb/firm/70001",
                        "name": "Улыбка",
                    }
                ],
                datetime.now(UTC),
            )
        ),
        encoding="utf-8",
    )
    seen: dict = {}

    async def fake_run(session_maker, parsed, options, export_dir, run, **kwargs) -> None:
        seen["names"] = [item.name for item in parsed.items]
        seen["options"] = options
        run.searched = 1
        run.failed = FORMAT_NOT_RECOGNIZED_TEXT
        run.problems.append("Страница Google не распознана (https://www.google.com/search, код 200)")

    monkeypatch.setattr(collector_cli, "run_google_enrich", fake_run)
    monkeypatch.setattr(collector_cli, "ERROR_LOG_PATH", str(tmp_path / "errors.log"))
    current = Settings(COLLECTOR_DB_PATH=str(tmp_path / "collector.db"), COLLECTOR_EXPORT_DIR=str(tmp_path))
    args = collector_cli._parse_args(["--assisted", "--google", "--enrich", str(request), "--ignore-interval"])

    code = await collector_cli._run(args, current)

    assert code == collector_cli.EXIT_ERROR
    assert seen["names"] == ["Улыбка"]
    assert seen["options"].ignore_interval is True
    assert "не распознана" in (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "Поиск остановлен" in capsys.readouterr().out


async def test_new_columns_are_added_to_an_older_database(tmp_path: Path) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE provider_state (name VARCHAR(32) PRIMARY KEY, consecutive_blocks INTEGER)")
        )
        await conn.execute(text("INSERT INTO provider_state (name, consecutive_blocks) VALUES ('google', 0)"))
    await init_db(engine)
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT requests_day, requests_day_used FROM provider_state"))).one()
    await engine.dispose()

    assert tuple(row) == (None, 0)
