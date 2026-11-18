"""The manual 2gis_cdp provider: gate (limit, robots.txt, pause), stops at refusals, off by default."""

from __future__ import annotations

import json
import re
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config import PROJECT_ROOT, Settings
from src.database.models import Base
from src.database.repo import get_provider_states
from src.services import pipeline
from src.services.api_client import (
    ProviderBlockedError,
    ProviderBudgetExhausted,
    ProviderFormatError,
    ProviderNoConnectionError,
    ProviderUnavailableError,
)
from src.services.collector import collect_leads, load_collector_config
from src.services.providers import registry
from src.services.providers.base import SearchArea, SearchQuery
from src.services.providers.registry import build_providers
from src.services.providers.twogis_cdp import MIN_PAUSE_SECONDS, CdpOptions, TwoGisCdpProvider
from src.services.scraper import twogis_site
from src.services.scraper.chrome import parsers, remote
from src.services.scraper.chrome.dom import DOMNode
from src.services.scraper.chrome.parsers import ChromeScraperSession, Gate
from src.services.scraper.chrome.remote import ChromeRemote
from src.services.scraper.chrome.utils import ensure_local_cdp_url
from src.services.scraper.robots import RobotsNoConnectionError, RobotsRules, RobotsUnavailableError
from src.services.twogis_links import build_card_url

QUERY = SearchQuery(text="стоматология", category="dentist_clinic", high_margin=True)
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4))
SEARCH_PAGE = ("https://2gis.ru/spb/search/стоматология", "Стоматологии — 2ГИС", "<html></html>")
FOREIGN_PAGE = ("https://example.com/", "Пример", "<html></html>")
CAPTCHA_PAGE = ("https://captcha.2gis.ru/form", "2GIS Captcha", "<div class='g-recaptcha'></div>")
_FIRM_RE = re.compile(r"/firm/([0-9]+)")


# ---------- doubles ----------


def _link(node_id: int, href: str) -> DOMNode:
    return DOMNode(
        nodeId=node_id,
        backendNodeId=node_id * 10,
        nodeType=1,
        nodeName="A",
        localName="a",
        nodeValue="",
        attributes=["href", href],
    )


def _doc(firm_ids: list[str], next_page: int | None = None) -> DOMNode:
    children = [_link(index + 2, f"/spb/firm/{fid}?stat=c3RhdA%3D%3D") for index, fid in enumerate(firm_ids)]
    if next_page is not None:
        children.append(_link(99, f"/spb/search/x/page/{next_page}"))
    return DOMNode(
        nodeId=1, backendNodeId=10, nodeType=9, nodeName="#document", localName="", nodeValue="", children=children
    )


def _card(firm_id: str, *, contacts: bool = True, status: int = 200) -> dict[str, Any]:
    item: dict[str, Any] = {"id": firm_id, "name": f"Клиника {firm_id}", "type": "branch", "address_name": "Невский, 1"}
    if contacts:
        item["contact_groups"] = [{"contacts": [{"type": "phone", "value": f"+7 812 555-00-{firm_id[-2:]}"}]}]
    body = {"meta": {"code": 200}, "result": {"items": [item]}}
    return {
        "status": status,
        "url": f"https://catalog.api.2gis.ru/3.0/items/byid?id={firm_id}",
        "body": json.dumps(body) if status == 200 else "",
    }


class FakeRemote:
    """A browser tab: search pages switched by pagination clicks, a card response for every card click."""

    def __init__(
        self,
        pages: dict[int, DOMNode] | None = None,
        cards: dict[str, dict[str, Any]] | None = None,
        page_info: tuple[str, str, str] = SEARCH_PAGE,
        doc_status: int = 200,
    ) -> None:
        self.pages = pages or {}
        self.cards = cards or {}
        self.page_info = page_info
        self.doc_status = doc_status
        self.current_page = 1
        self.navigations: list[str] = []
        self.clicks: list[str] = []
        self.started = 0

    def actions(self) -> int:
        return len(self.navigations) + len(self.clicks)

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        return None

    def clear_requests(self) -> None:
        return None

    def drain_responses(self, _pattern: str) -> None:
        return None

    def navigate(self, url: str, timeout: int = 60) -> None:
        self.navigations.append(url)
        self.current_page = 1

    def get_document_response(self) -> dict[str, Any]:
        return {"status": self.doc_status, "meta": {"type": "Document"}}

    def execute_script(self, expression: str) -> Any:
        return "complete" if expression == "document.readyState" else None

    def get_document(self, full: bool = True) -> DOMNode:
        return self.pages.get(self.current_page, _doc([]))

    def perform_click(self, node: DOMNode) -> None:
        href = node.attributes["href"]
        self.clicks.append(href)
        if "/page/" in href:
            self.current_page = int(href.rsplit("/", 1)[1])

    def wait_response(self, _pattern: str) -> dict[str, Any] | None:
        match = _FIRM_RE.search(self.clicks[-1]) if self.clicks else None
        return self.cards.get(match.group(1)) if match else None

    def get_response_body(self, response: dict[str, Any]) -> str:
        return str(response.get("body") or "")

    def get_responses(self) -> list[dict[str, Any]]:
        return []

    def read_current_page(self) -> tuple[str, str, str]:
        return self.page_info

    def read_page_state(self) -> tuple[dict[str, Any], dict[str, str]]:
        return {}, {}

    def bring_to_front(self) -> None:
        return None


class RecordingRules(RobotsRules):
    def __init__(self, disallowed: set[str] | None = None) -> None:
        super().__init__()
        self.disallowed = disallowed or set()
        self.checked: list[str] = []

    def is_allowed(self, url: str) -> bool:
        self.checked.append(url)
        return url not in self.disallowed


class _FastClock:
    """The page-ready waits of the readers pass at once instead of in seconds."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 1.0
        return self.now

    def sleep(self, _seconds: float) -> None:
        return None


@pytest.fixture(autouse=True)
def _fast_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parsers, "time", _FastClock())


@pytest.fixture(autouse=True)
def _no_site_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_analyze(url, client=None):
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", fake_analyze)


class Harness:
    def __init__(self, fake: FakeRemote, rules: RobotsRules | Exception | None = None, **options: Any) -> None:
        self.fake = fake
        self.rules = rules if rules is not None else RecordingRules()
        self.pauses: list[float] = []
        self.notes: list[str] = []
        self.sessions = 0

        async def load_rules() -> RobotsRules:
            if isinstance(self.rules, Exception):
                raise self.rules
            return self.rules

        def session_factory(gate: Gate) -> ChromeScraperSession:
            self.sessions += 1
            return ChromeScraperSession(  # type: ignore[arg-type,return-value]
                gate=gate, remote_factory=lambda _o, _p: fake, city_slug=CdpOptions(**options).city_slug
            )

        self.provider = TwoGisCdpProvider(
            CdpOptions(**options),
            session_factory=session_factory,
            robots_loader=load_rules,
            sleep=self.pauses.append,
            notify=self.notes.append,
        )


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1, **overrides)


# ---------- normal reading ----------


async def test_reads_pages_by_clicks_and_returns_only_cards_with_known_contacts() -> None:
    fake = FakeRemote(
        pages={1: _doc(["101", "102"], next_page=2), 2: _doc(["201"])},
        cards={"101": _card("101"), "102": _card("102", contacts=False), "201": _card("201")},
    )
    harness = Harness(fake)
    provider = harness.provider
    try:
        first = await provider.search(QUERY, AREA, page=1)
        assert [place.external_id for place in first.places] == ["2gis_101"]
        assert first.has_more is True
        second = await provider.search(QUERY, AREA, page=2)
        assert [place.external_id for place in second.places] == ["2gis_201"]
        assert second.has_more is False
    finally:
        await provider.close()

    # One navigation; the next page through its pagination link.
    assert len(fake.navigations) == 1
    assert fake.clicks[2] == "/spb/search/x/page/2"
    assert provider.run_counted is False  # reset by close()


# ---------- 1. the lower bound of pauses ----------


def test_pause_below_three_seconds_cannot_be_configured() -> None:
    assert MIN_PAUSE_SECONDS == 3.0
    with pytest.raises(ValidationError):
        _settings(SCRAPER_CDP_MIN_DELAY_SECONDS=2.9)
    with pytest.raises(ValidationError):
        _settings(SCRAPER_CDP_MAX_DELAY_SECONDS=2.9)
    with pytest.raises(ValidationError):
        _settings(SCRAPER_CDP_MIN_DELAY_SECONDS=9, SCRAPER_CDP_MAX_DELAY_SECONDS=8)
    with pytest.raises(ValueError):
        CdpOptions(min_delay_seconds=1)
    with pytest.raises(ValueError):
        CdpOptions(min_delay_seconds=9, max_delay_seconds=8)
    defaults = CdpOptions.from_settings(_settings())
    assert (defaults.min_delay_seconds, defaults.max_delay_seconds) == (3.0, 8.0)
    assert defaults.max_pages_per_run == 60
    assert defaults.max_results_per_run == 30


async def test_every_click_and_page_change_waits_three_to_eight_seconds() -> None:
    fake = FakeRemote(
        pages={1: _doc(["101", "102"], next_page=2), 2: _doc(["201", "202"])},
        cards={fid: _card(fid) for fid in ("101", "102", "201", "202")},
    )
    harness = Harness(fake)
    try:
        await harness.provider.search(QUERY, AREA, page=1)
        await harness.provider.search(QUERY, AREA, page=2)
    finally:
        await harness.provider.close()

    # Not before the first action of the run, before every other one.
    assert fake.actions() == 6
    assert len(harness.pauses) == fake.actions() - 1
    assert all(3.0 <= pause <= 8.0 for pause in harness.pauses)


# ---------- 2. robots.txt ----------


async def test_robots_is_checked_for_every_card_by_its_canonical_link() -> None:
    fake = FakeRemote(pages={1: _doc(["101", "102", "103"])}, cards={fid: _card(fid) for fid in ("101", "102", "103")})
    rules = RecordingRules()
    harness = Harness(fake, rules)
    try:
        page = await harness.provider.search(QUERY, AREA)
    finally:
        await harness.provider.close()

    card_checks = [url for url in rules.checked if "/firm/" in url]
    card_clicks = [href for href in fake.clicks if "/firm/" in href]
    assert len(card_checks) == len(card_clicks) == 3
    assert card_checks == [build_card_url("spb", fid) for fid in ("101", "102", "103")]
    assert not any("stat=" in url for url in rules.checked)
    assert len(rules.checked) == 3 + 1  # the cards and the search page itself
    assert len(page.places) == 3


async def test_card_forbidden_by_robots_is_not_clicked() -> None:
    fake = FakeRemote(pages={1: _doc(["101", "102", "103"])}, cards={fid: _card(fid) for fid in ("101", "102", "103")})
    forbidden = build_card_url("spb", "102")
    assert forbidden is not None
    harness = Harness(fake, RecordingRules({forbidden}))
    try:
        page = await harness.provider.search(QUERY, AREA)
    finally:
        await harness.provider.close()

    assert not any("/firm/102" in href for href in fake.clicks)
    assert len(fake.clicks) == 2
    assert [place.external_id for place in page.places] == ["2gis_101", "2gis_103"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RobotsUnavailableError("код 503"), ProviderUnavailableError),
        (RobotsNoConnectionError("сетевая ошибка"), ProviderNoConnectionError),
    ],
)
async def test_unreadable_robots_opens_nothing(error: Exception, expected: type[Exception]) -> None:
    fake = FakeRemote(pages={1: _doc(["101"])}, cards={"101": _card("101")})
    harness = Harness(fake, error)
    with pytest.raises(expected):
        await harness.provider.search(QUERY, AREA)
    await harness.provider.close()
    assert fake.navigations == []  # no page of 2GIS is opened (the browser only checks the exit IP at start)
    assert fake.clicks == []


async def test_search_page_forbidden_by_robots_is_not_opened() -> None:
    fake = FakeRemote(pages={1: _doc(["101"])}, cards={"101": _card("101")})
    harness = Harness(fake, RecordingRules({twogis_site.build_search_url("spb", QUERY.text)}))
    with pytest.raises(ProviderUnavailableError, match="robots"):
        await harness.provider.search(QUERY, AREA)
    await harness.provider.close()
    assert fake.actions() == 0


# ---------- 3. the limit of pages per run ----------


@pytest.mark.parametrize("limit", [2, 3, 4])
async def test_page_limit_counts_navigations_and_every_card(limit: int) -> None:
    firm_ids = ["101", "102", "103", "104", "105"]
    fake = FakeRemote(pages={1: _doc(firm_ids, next_page=2)}, cards={fid: _card(fid) for fid in firm_ids})
    harness = Harness(fake, max_pages_per_run=limit)
    try:
        page = await harness.provider.search(QUERY, AREA)
        # Partial: what was read before the limit, and no more pages.
        assert len(page.places) == limit - 1
        assert page.has_more is False
        with pytest.raises(ProviderBudgetExhausted):
            await harness.provider.search(QUERY, AREA, page=2)
    finally:
        await harness.provider.close()
    assert fake.actions() <= limit


async def test_limit_reached_before_anything_is_read_ends_the_provider() -> None:
    fake = FakeRemote(pages={1: _doc(["101"])}, cards={"101": _card("101")})
    harness = Harness(fake, max_pages_per_run=1)
    try:
        with pytest.raises(ProviderBudgetExhausted):
            await harness.provider.search(QUERY, AREA)
    finally:
        await harness.provider.close()
    assert fake.actions() == 1


# ---------- 4. on the schedule ----------


def test_provider_is_on_by_default_and_on_the_schedule() -> None:
    settings = _settings()
    assert settings.COLLECT_PROVIDERS.split(",") == ["2gis_cdp", "osm"]
    assert load_collector_config(settings).providers == ["2gis_cdp", "osm"]
    env_lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    collect_line = next(line for line in env_lines if line.startswith("COLLECT_PROVIDERS="))
    assert "2gis_cdp" in collect_line


def test_registry_lets_it_into_the_schedule_with_pychrome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry.importlib.util, "find_spec", lambda name: object())
    built = build_providers(["2gis_cdp"])  # no options at all: what the schedule may do with defaults
    assert [provider.name for provider in built.providers] == ["2gis_cdp"]

    built = build_providers(["2gis_cdp"], cdp_options=CdpOptions(min_hours_between_runs=12))
    assert built.providers[0].min_hours_between_runs == 12


def test_registry_leaves_it_out_without_pychrome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry.importlib.util, "find_spec", lambda name: None)
    missing = build_providers(["2gis_cdp", "osm"], cdp_options=CdpOptions())
    assert [provider.name for provider in missing.providers] == ["osm"]
    assert "pychrome" in missing.disabled["2gis_cdp"]


def test_the_old_name_of_the_api_provider_is_an_alias(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        config = load_collector_config(_settings(COLLECT_PROVIDERS="2gis,osm,2gis_cdp"))
    assert config.providers == ["2gis_cdp", "osm"]  # one 2gis_cdp, in the place of the first name
    assert "COLLECT_PROVIDERS" in caplog.text and "2gis_cdp" in caplog.text


# ---------- 5. refusals stop everything ----------


@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        (FakeRemote(pages={1: _doc(["101"])}, page_info=CAPTCHA_PAGE), "капч"),
        (FakeRemote(pages={1: _doc(["101"])}, doc_status=403), "403"),
        (FakeRemote(pages={1: _doc(["101"])}, doc_status=429), "429"),
        (FakeRemote(page_info=("https://2gis.ru/spb/search/x", "", "")), "пустой"),
        (FakeRemote(pages={1: _doc(["101"])}, page_info=FOREIGN_PAGE), "не с 2ГИС"),
        (FakeRemote(page_info=SEARCH_PAGE), "не распознана"),
    ],
)
async def test_refusal_stops_at_once_and_nothing_follows(fake: FakeRemote, reason: str) -> None:
    harness = Harness(fake)
    try:
        with pytest.raises(ProviderBlockedError, match=reason):
            await harness.provider.search(QUERY, AREA)
        actions = fake.actions()
        assert harness.notes and reason in harness.notes[0]
        with pytest.raises(ProviderBlockedError):
            await harness.provider.search(QUERY, AREA, page=2)
        with pytest.raises(ProviderBlockedError):
            await harness.provider.search(SearchQuery(text="кафе", category="horeca"), AREA)
        assert fake.actions() == actions
    finally:
        await harness.provider.close()
    assert fake.clicks == []


async def test_refusal_on_a_card_keeps_what_was_read() -> None:
    fake = FakeRemote(
        pages={1: _doc(["101", "102", "103"])},
        cards={"101": _card("101"), "102": _card("102", status=429), "103": _card("103")},
    )
    harness = Harness(fake)
    try:
        with pytest.raises(ProviderBlockedError, match="429") as caught:
            await harness.provider.search(QUERY, AREA)
        assert [place.external_id for place in caught.value.partial] == ["2gis_101"]
        clicks = list(fake.clicks)
        with pytest.raises(ProviderBlockedError):
            await harness.provider.search(QUERY, AREA)
    finally:
        await harness.provider.close()
    assert fake.clicks == clicks
    assert not any("/firm/103" in href for href in clicks)


async def test_nothing_found_is_not_a_refusal() -> None:
    fake = FakeRemote(page_info=("https://2gis.ru/spb/search/x", "Ничего не найдено — 2ГИС", "<html></html>"))
    harness = Harness(fake)
    try:
        page = await harness.provider.search(QUERY, AREA)
    finally:
        await harness.provider.close()
    assert page.places == []
    assert page.has_more is False
    assert harness.notes == []


# ---------- the district is asked for as "<name> район" ----------


async def test_a_district_goes_into_the_search_as_a_name_with_the_word_district() -> None:
    fake = FakeRemote(page_info=("https://2gis.ru/spb/search/x", "Ничего не найдено — 2ГИС", "<html></html>"))
    harness = Harness(fake)
    try:
        await harness.provider.search(QUERY, SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Калининский"))
    finally:
        await harness.provider.close()
    assert fake.navigations == [twogis_site.build_search_url("spb", "стоматология Калининский район")]
    assert "%D1%80%D0%B0%D0%B9%D0%BE%D0%BD" in fake.navigations[0]  # the word "район" itself


async def test_an_area_without_a_district_adds_nothing() -> None:
    fake = FakeRemote(page_info=("https://2gis.ru/spb/search/x", "Ничего не найдено — 2ГИС", "<html></html>"))
    harness = Harness(fake)
    try:
        await harness.provider.search(QUERY, SearchArea(bbox=(59.9, 30.3, 59.95, 30.4)))
    finally:
        await harness.provider.close()
    assert fake.navigations == [twogis_site.build_search_url("spb", "стоматология")]


# ---------- 404 of a search: nothing found, not a block ----------

NOTHING_FOUND_404 = ("https://2gis.ru/spb/search/x", "Точных совпадений нет — 2ГИС", "<html></html>")


def _query(number: int) -> SearchQuery:
    return SearchQuery(text=f"запрос {number}", category="dentist_clinic", high_margin=True)


async def test_404_of_a_search_without_a_captcha_is_an_empty_result(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeRemote(doc_status=404, page_info=NOTHING_FOUND_404)
    harness = Harness(fake)
    try:
        with caplog.at_level("INFO", logger="src.services.providers.twogis_cdp"):
            page = await harness.provider.search(_query(1), AREA)
        # The target is left out, and the next one is read: nothing is stopped, nobody is told about a refusal.
        fake.doc_status = 200
        fake.pages = {1: _doc(["101"])}
        fake.cards = {"101": _card("101")}
        next_page = await harness.provider.search(_query(2), AREA)
    finally:
        await harness.provider.close()
    assert page.places == [] and page.has_more is False
    assert harness.notes == []
    assert [place.external_id for place in next_page.places] == ["2gis_101"]
    assert "taken as nothing found: https://2gis.ru/spb/search/x, title 'Точных совпадений нет — 2ГИС'" in caplog.text


@pytest.mark.parametrize(
    ("page_info", "reason"),
    [
        (CAPTCHA_PAGE, "капч"),
        (("https://2gis.ru/spb/search/x", "Проверка", "<div class='g-recaptcha'></div>"), "капч"),
        (("https://2gis.ru/spb/search/x", "", ""), "пустой"),
        (FOREIGN_PAGE, "не с 2ГИС"),
    ],
)
async def test_404_with_a_sign_of_a_captcha_or_an_odd_page_is_still_a_refusal(
    page_info: tuple[str, str, str], reason: str
) -> None:
    harness = Harness(FakeRemote(doc_status=404, page_info=page_info))
    try:
        with pytest.raises(ProviderBlockedError, match=reason):
            await harness.provider.search(_query(1), AREA)
        assert harness.notes and reason in harness.notes[0]
    finally:
        await harness.provider.close()


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_refusing_statuses_are_not_taken_for_an_empty_search(status: int) -> None:
    harness = Harness(FakeRemote(doc_status=status, page_info=NOTHING_FOUND_404))
    try:
        with pytest.raises(ProviderBlockedError, match=str(status)):
            await harness.provider.search(_query(1), AREA)
    finally:
        await harness.provider.close()


async def test_five_empty_404_searches_in_a_row_are_a_refusal() -> None:
    fake = FakeRemote(doc_status=404, page_info=NOTHING_FOUND_404)
    harness = Harness(fake)
    try:
        for number in range(1, 5):
            assert (await harness.provider.search(_query(number), AREA)).places == []
        with pytest.raises(ProviderBlockedError, match="5-й раз подряд"):
            await harness.provider.search(_query(5), AREA)
        assert harness.notes and "404" in harness.notes[0]
    finally:
        await harness.provider.close()


async def test_a_search_that_found_something_resets_the_count_of_empty_ones() -> None:
    fake = FakeRemote(doc_status=404, page_info=NOTHING_FOUND_404)
    harness = Harness(fake)
    try:
        for number in range(1, 5):  # four empty ones: one short of a refusal
            await harness.provider.search(_query(number), AREA)
        fake.doc_status = 200
        fake.pages = {1: _doc(["101"])}
        fake.cards = {"101": _card("101")}
        assert len((await harness.provider.search(_query(5), AREA)).places) == 1
        fake.doc_status = 404
        for number in range(6, 10):  # four empty ones again, not five in a row
            await harness.provider.search(_query(number), AREA)
        with pytest.raises(ProviderBlockedError):
            await harness.provider.search(_query(10), AREA)
    finally:
        await harness.provider.close()


# ---------- what is clicked, and why a click gave nothing: the log, the limits of failures, the snapshot ----------

KALININSKY = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Калининский")
AUTO = SearchQuery(text="автосервис", category="auto_repair", high_margin=True)


def _doc_with(hrefs: list[str]) -> DOMNode:
    children = [_link(index + 2, href) for index, href in enumerate(hrefs)]
    return DOMNode(
        nodeId=1, backendNodeId=10, nodeType=9, nodeName="#document", localName="", nodeValue="", children=children
    )


def test_a_link_is_described_by_its_kind_and_number_only() -> None:
    assert parsers.describe_link("/spb/firm/70000001018420390?stat=c3RhdA%3D%3D") == "firm 70000001018420390"
    assert parsers.describe_link("https://2gis.ru/spb/station/123?stat=abc") == "station 123"
    assert parsers.describe_link("/spb/geo/4504235282649497") == "other /spb/geo/4504235282649497"
    assert len(parsers.describe_link("/" + "x" * 200)) <= len("other ") + 40


async def test_the_log_says_which_page_was_opened_what_it_offers_and_what_each_click_gave(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeRemote(pages={1: _doc(["101", "102"])}, cards={"101": _card("101"), "102": _card("102")})
    harness = Harness(fake)
    try:
        with caplog.at_level("INFO"):
            await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    text = caplog.text
    assert "Page opened: https://2gis.ru/spb/search/автосервис Калининский район, status 200" in text
    assert "Search page 1:" in text and "2 links to organizations (clicked)" in text
    assert "Card 1/2 (firm 101): read" in text and "Card 2/2 (firm 102): read" in text
    assert "Клиника" not in text  # no names of organizations in the log


async def test_only_organizations_are_clicked_not_stations_and_not_places() -> None:
    stat = "?stat=c3RhdA%3D%3D"
    fake = FakeRemote(
        pages={1: _doc_with([f"/spb/station/55{stat}", "/spb/geo/4504235282649497", f"/spb/firm/101{stat}"])},
        cards={"101": _card("101")},
    )
    harness = Harness(fake)
    try:
        page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in page.places] == ["2gis_101"]
    assert len(fake.clicks) == 1 and "/firm/101" in fake.clicks[0]


async def test_a_card_that_gives_nothing_is_left_after_its_attempts_and_the_next_one_is_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeRemote(pages={1: _doc(["101", "102"])}, cards={"102": _card("102")})  # no answer to the first card
    harness = Harness(fake)
    try:
        with caplog.at_level("INFO"):
            page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in page.places] == ["2gis_102"]
    assert len(fake.clicks) == 3 + 1  # three attempts on the first card, one on the second
    assert "Card 1/2 (firm 101), attempt 1/3: the catalog did not answer after the click" in caplog.text
    assert "Card 1/2 (firm 101): not read after 3 attempts (the catalog did not answer after the click)" in caplog.text
    assert "Card 2/2 (firm 102): read" in caplog.text


async def test_three_cards_in_a_row_that_give_nothing_stop_the_page_and_keep_the_budget() -> None:
    fake = FakeRemote(
        pages={1: _doc(["101", "102", "103", "104", "105"])},
        cards={"101": _card("101")},  # the first is read, then three give nothing
    )
    harness = Harness(fake)
    try:
        with pytest.raises(ProviderFormatError, match="3 карточки подряд не прочитались") as stopped:
            await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in stopped.value.partial] == ["2gis_101"]
    assert len(fake.clicks) == 1 + 3 * 3  # the fifth card is not even tried
    assert harness.notes == []  # not a refusal of the site


async def test_a_read_card_between_the_failures_starts_the_count_anew() -> None:
    fake = FakeRemote(
        pages={1: _doc(["101", "102", "103", "104", "105"])},
        cards={"101": _card("101"), "103": _card("103")},  # 102, 104 and 105 give nothing: never three in a row
    )
    harness = Harness(fake)
    try:
        page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in page.places] == ["2gis_101", "2gis_103"]


async def test_the_search_page_is_saved_for_a_diagnostic_run(tmp_path: Path) -> None:
    fake = FakeRemote(pages={1: _doc(["101", "102"])}, cards={"101": _card("101"), "102": _card("102")})
    provider = TwoGisCdpProvider(
        CdpOptions(),
        session_factory=lambda gate: ChromeScraperSession(  # type: ignore[arg-type,return-value]
            gate=gate, remote_factory=lambda _o, _p: fake, snapshot_dir=tmp_path / "snapshots"
        ),
        robots_loader=lambda: _rules(),
        sleep=lambda _seconds: None,
        notify=lambda _text: None,
    )
    try:
        await provider.search(AUTO, KALININSKY)
    finally:
        await provider.close()
    html_files = list((tmp_path / "snapshots").glob("search_*_p1.html"))
    json_files = list((tmp_path / "snapshots").glob("search_*_p1.json"))
    assert len(html_files) == len(json_files) == 1
    assert html_files[0].read_text(encoding="utf-8") == SEARCH_PAGE[2]
    summary = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert summary["title"] == SEARCH_PAGE[1] and summary["page"] == 1
    assert [(link["link"], link["clicked"]) for link in summary["links"]] == [("firm 101", True), ("firm 102", True)]


async def _rules() -> RobotsRules:
    return RecordingRules()


def test_the_snapshot_folder_is_off_by_default_and_comes_from_the_settings() -> None:
    assert CdpOptions().snapshot_dir is None
    assert CdpOptions.from_settings(_settings()).snapshot_dir is None
    options = CdpOptions.from_settings(_settings(TWOGIS_SNAPSHOT_DIR="data/snapshots"))
    assert options.snapshot_dir == PROJECT_ROOT / "data" / "snapshots"


# ---------- the list of results is redrawn after the load: wait for it, take the link anew before the click ----------


class ScriptedRemote(FakeRemote):
    """A browser tab whose list of results is whatever `script(call_number)` says, and that remembers the clicks."""

    def __init__(self, script: Any, cards: dict[str, dict[str, Any]]) -> None:
        super().__init__(cards=cards)
        self.script = script
        self.document_reads = 0
        self.clicked_generations: list[tuple[int, int]] = []

    def get_document(self, full: bool = True) -> DOMNode:
        self.document_reads += 1
        stat = "?stat=c3RhdA%3D%3D"
        generation = self.document_reads
        hrefs = [f"/spb/firm/{firm_id}{stat}" for firm_id in self.script(generation)]
        children = [_link(generation * 1000 + index, href) for index, href in enumerate(hrefs)]
        return DOMNode(
            nodeId=1, backendNodeId=10, nodeType=9, nodeName="#document", localName="", nodeValue="", children=children
        )

    def perform_click(self, node: DOMNode) -> None:
        # The node must come from the latest read of the page, not from an earlier one.
        self.clicked_generations.append((node.id // 1000, self.document_reads))
        super().perform_click(node)


def _cards(*ids: str) -> dict[str, dict[str, Any]]:
    return {firm_id: _card(firm_id) for firm_id in ids}


async def test_the_clicks_wait_until_the_list_has_settled(caplog: pytest.LogCaptureFixture) -> None:
    # The list is one thing at first, then another (the page is sorted again), then it stays.
    remote_ = ScriptedRemote(lambda n: ["201", "202"] if n <= 3 else ["101", "102"], _cards("101", "102", "201", "202"))
    harness = Harness(remote_)
    try:
        with caplog.at_level("INFO"):
            page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in page.places] == ["2gis_101", "2gis_102"]  # the settled list, not the first
    assert "The list of results has settled: the same" in caplog.text


async def test_a_list_that_never_settles_is_read_as_it_is_after_the_limit(caplog: pytest.LogCaptureFixture) -> None:
    remote_ = ScriptedRemote(
        lambda n: ["101", "102"] if n % 2 else ["102", "101"], _cards("101", "102")
    )  # the order changes at every read
    harness = Harness(remote_)
    try:
        with caplog.at_level("INFO"):
            page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert "still changing after" in caplog.text and "read as it is" in caplog.text
    assert {place.external_id for place in page.places} == {"2gis_101", "2gis_102"}


async def test_the_link_is_taken_anew_right_before_every_click() -> None:
    remote_ = ScriptedRemote(lambda n: ["101", "102", "103"], _cards("101", "102", "103"))
    harness = Harness(remote_)
    try:
        await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert len(remote_.clicked_generations) == 3
    assert all(generation == latest for generation, latest in remote_.clicked_generations)
    # and the page was read again before each click: no two clicks come from the same read
    reads = [latest for _generation, latest in remote_.clicked_generations]
    assert reads == sorted(set(reads)) and len(set(reads)) == 3


async def test_a_link_that_left_the_page_before_its_click_is_not_clicked(caplog: pytest.LogCaptureFixture) -> None:
    # 101 is in the list when it is read, and gone when its turn comes (the list was redrawn once more).
    remote_ = ScriptedRemote(lambda n: ["101", "102"] if n <= 6 else ["102"], _cards("101", "102"))
    harness = Harness(remote_)
    try:
        with caplog.at_level("INFO"):
            page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in page.places] == ["2gis_102"]
    assert len(remote_.clicks) == 1 and "/firm/102" in remote_.clicks[0]  # nothing was spent on the missing one
    assert "Card 1/2 (firm 101): the link is no longer on the page" in caplog.text


# ---------- a link is found by the browser itself, not by reading the whole page; what a failure looks like ----------

PAGE_URL = "https://2gis.ru/spb/search/автосервис Калининский район?m=30.607964,60.009023/10"


class QuickFinderRemote(ScriptedRemote):
    """A tab that finds a link by its address at once (like ChromeRemote.find_link_by_href) and says its address."""

    def __init__(self, script: Any, cards: dict[str, dict[str, Any]], *, missing: set[str] | None = None) -> None:
        super().__init__(script, cards)
        self.missing = missing or set()
        self.quick_lookups = 0
        self.fail_with: Exception | None = None

    def find_link_by_href(self, href: str) -> DOMNode | None:
        self.quick_lookups += 1
        if self.fail_with is not None:
            raise self.fail_with
        if any(firm_id in href for firm_id in self.missing):
            return None
        return _link(900_000 + self.quick_lookups, href)

    def execute_script(self, expression: str) -> Any:
        if expression.startswith("String(location.href)"):
            return PAGE_URL
        return super().execute_script(expression)


class SlowRemote(ScriptedRemote):
    """The same tab without the quick search: every click needs a read of the whole page."""

    def execute_script(self, expression: str) -> Any:
        if expression.startswith("String(location.href)"):
            return PAGE_URL
        return super().execute_script(expression)


async def test_a_quick_search_saves_a_read_of_the_whole_page_for_every_click(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ids = ["101", "102", "103"]
    slow = SlowRemote(lambda n: ids, _cards(*ids))
    quick = QuickFinderRemote(lambda n: ids, _cards(*ids))
    for remote_ in (slow, quick):
        harness = Harness(remote_)
        try:
            with caplog.at_level("INFO"):
                page = await harness.provider.search(AUTO, KALININSKY)
        finally:
            await harness.provider.close()
        assert [place.external_id for place in page.places] == ["2gis_101", "2gis_102", "2gis_103"]
    assert slow.document_reads - quick.document_reads == 3  # one read of the whole page less for each click
    assert quick.quick_lookups == 3
    assert "quick searches of a link: 3" in caplog.text  # the line about the time of the reads, per page


async def test_the_line_about_the_reads_of_the_page_has_the_count_the_least_the_most_and_the_sum(
    caplog: pytest.LogCaptureFixture,
) -> None:
    remote_ = QuickFinderRemote(lambda n: ["101"], _cards("101"))
    harness = Harness(remote_)
    try:
        with caplog.at_level("INFO"):
            await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    line = next(line for line in caplog.text.splitlines() if "Reads of the whole page:" in line)
    assert re.search(
        r"Reads of the whole page: \d+, the shortest [\d.]+ s, the longest [\d.]+ s, [\d.]+ s in all", line
    )


async def test_when_the_quick_search_fails_the_whole_page_is_read_instead(caplog: pytest.LogCaptureFixture) -> None:
    from src.services.scraper.chrome.exceptions import ChromeRuntimeException

    remote_ = QuickFinderRemote(lambda n: ["101", "102"], _cards("101", "102"))
    remote_.fail_with = ChromeRuntimeException("quick search of a link failed: boom")
    harness = Harness(remote_)
    try:
        with caplog.at_level("INFO"):
            page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in page.places] == ["2gis_101", "2gis_102"]  # the old way still works
    assert "Quick search of the link failed" in caplog.text and "the whole page is read instead" in caplog.text


async def test_a_link_the_quick_search_misses_is_looked_for_in_the_whole_page_before_it_is_given_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The quick search says 101 is not there, but the whole page still lists it: the click goes on with that link.
    remote_ = QuickFinderRemote(lambda n: ["101", "102"], _cards("101", "102"), missing={"101"})
    harness = Harness(remote_)
    try:
        with caplog.at_level("INFO"):
            page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in page.places] == ["2gis_101", "2gis_102"]
    assert "no longer on the page" not in caplog.text


async def test_the_state_of_the_page_is_logged_when_a_link_has_left_it(caplog: pytest.LogCaptureFixture) -> None:
    remote_ = QuickFinderRemote(lambda n: ["101", "102"] if n <= 6 else ["102", "103"], _cards("101", "102", "103"))
    remote_.missing = {"101"}
    harness = Harness(remote_)
    try:
        with caplog.at_level("INFO"):
            await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    text = caplog.text
    assert "Card 1/2 (firm 101): the link is no longer on the page" in text
    state = next(line for line in text.splitlines() if "Page state (the link is no longer on the page)" in line)
    assert "address https://2gis.ru/spb/search/автосервис Калининский район?m=30.607964" in state
    assert "/firm/ in the address: False" in state and "map position (m=) in the address: True" in state
    assert "2 links to organizations" in state and "'firm 102'" in state and "'firm 103'" in state
    assert "the last read of the whole page took" in state


async def test_three_cards_in_a_row_that_were_not_read_describe_the_page_and_save_a_snapshot_when_asked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = SlowRemote(lambda n: ["101", "102", "103", "104"], {})  # nothing answers to any click
    provider = TwoGisCdpProvider(
        CdpOptions(),
        session_factory=lambda gate: ChromeScraperSession(  # type: ignore[arg-type,return-value]
            gate=gate, remote_factory=lambda _o, _p: fake, snapshot_dir=tmp_path / "snap"
        ),
        robots_loader=lambda: _rules(),
        sleep=lambda _seconds: None,
        notify=lambda _text: None,
    )
    try:
        with caplog.at_level("INFO"), pytest.raises(ProviderFormatError):
            await provider.search(AUTO, KALININSKY)
    finally:
        await provider.close()
    assert "Page state (several cards in a row could not be read)" in caplog.text
    failures = list((tmp_path / "snap").glob("failure_*.html"))
    assert len(failures) == 1
    summary = json.loads(failures[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert summary["reason"].startswith("3 карточки подряд") and summary["links_in_the_last_read"][0] == "firm 101"


async def test_without_a_snapshot_folder_nothing_is_saved_when_the_page_is_left(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = SlowRemote(lambda n: ["101", "102", "103"], {})
    harness = Harness(fake)
    try:
        with caplog.at_level("INFO"), pytest.raises(ProviderFormatError):
            await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert "Page state (several cards in a row could not be read)" in caplog.text  # the log still says what it saw
    assert "Snapshot of the page saved" not in caplog.text


def test_only_the_three_latest_snapshots_are_kept(tmp_path: Path) -> None:
    import os

    for number in range(5):
        for suffix in (".html", ".json"):
            path = tmp_path / f"search_{number}{suffix}"
            path.write_text("x", encoding="utf-8")
            os.utime(path, (1_000_000 + number, 1_000_000 + number))
    parsers._BaseChromePageReader._prune_snapshots(tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "search_2.html",
        "search_2.json",
        "search_3.html",
        "search_3.json",
        "search_4.html",
        "search_4.json",
    ]


async def test_the_wait_for_the_list_counts_reads_and_seconds_and_has_a_longer_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert parsers._LINKS_SETTLE_TIMEOUT_SECONDS == 30.0
    remote_ = SlowRemote(lambda n: ["101"], _cards("101"))
    harness = Harness(remote_)
    try:
        with caplog.at_level("INFO"):
            await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    line = next(line for line in caplog.text.splitlines() if "The list of results has settled" in line)
    assert "the same 3 reads in a row, 3 reads in all" in line and re.search(r"[\d.]+ s, 1 links", line)


# ---------- links of other cities are not clicked; a click that leaves the search ends the page ----------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SPB_SEARCH = "https://2gis.ru/spb/search/автосервис Невский район"


def _doc_from_html(name: str) -> DOMNode:
    """The links of a saved page as a tree of nodes: the page itself is made anonymous, only the links are there."""
    import html as html_module

    text = (FIXTURES_DIR / name).read_text(encoding="utf-8")
    hrefs = [html_module.unescape(href) for href in re.findall(r'href="([^"]+)"', text)]
    return _doc_with(hrefs)


class UrlRemote(FakeRemote):
    """A tab that knows its address: it is the page that was opened until a click on a link of `leave_on`."""

    def __init__(self, *args: Any, leave_on: tuple[str, ...] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.url = ""
        self.leave_on = leave_on

    def navigate(self, url: str, timeout: int = 60) -> None:
        super().navigate(url, timeout)
        self.url = url

    def perform_click(self, node: DOMNode) -> None:
        super().perform_click(node)
        if any(marker in node.attributes["href"] for marker in self.leave_on):
            self.url = "https://2gis.ru/spb/firm/70000001999999999?m=30.5,59.9/15"

    def execute_script(self, expression: str) -> Any:
        if expression.startswith("String(location.href)"):
            return self.url or None
        return super().execute_script(expression)


class FixtureRemote(UrlRemote):
    def get_document(self, full: bool = True) -> DOMNode:
        return _doc_from_html("twogis_search_cities.html")


@pytest.mark.parametrize(
    ("href", "city"),
    [
        ("/spb/firm/70000001?stat=c3RhdA%3D%3D", "spb"),
        ("/kudrovo/firm/70000001?stat=c3RhdA%3D%3D", "kudrovo"),
        ("https://2gis.ru/murino/firm/70000001?stat=x", "murino"),
        ("/spb-region/firm/70000001", "spb-region"),
        ("/firm/70000001", None),
        ("/spb/search/query", None),
        ("/spb/station/55", None),
    ],
)
def test_the_city_of_a_link_is_its_whole_first_segment(href: str, city: str | None) -> None:
    assert parsers.link_city(href) == city


async def test_on_a_saved_page_only_the_links_of_the_city_are_clicked(caplog: pytest.LogCaptureFixture) -> None:
    ids = [f"7000000100000{number:04d}" for number in range(1, 13)]
    fake = FixtureRemote(cards=_cards(*ids))
    fake.url = SPB_SEARCH
    harness = Harness(fake)
    try:
        with caplog.at_level("INFO"):
            page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    clicked = fake.clicks
    assert len(clicked) == 10 and all(href.startswith("/spb/firm/") for href in clicked)
    assert not any("/kudrovo/" in href for href in clicked)  # no click, no attempt, no card spent on them
    assert len(page.places) == 10
    assert "Card 10/10" in caplog.text and "Card 11/" not in caplog.text
    other = next(line for line in caplog.text.splitlines() if "of other cities, left alone" in line)
    assert "left alone: 2 (kudrovo: 2)" in other
    assert not re.search(r"\d{6,}", other.split("left alone")[1])  # only the slug, no numbers of organizations
    assert harness.notes == []


async def test_a_city_is_compared_by_the_whole_segment_not_by_a_part_of_it(caplog: pytest.LogCaptureFixture) -> None:
    stat = "?stat=c3RhdA%3D%3D"
    fake = UrlRemote(
        pages={1: _doc_with([f"/spb/firm/101{stat}", f"/spbx/firm/102{stat}", f"/spb-region/firm/103{stat}"])},
        cards=_cards("101", "102", "103"),
    )
    fake.url = SPB_SEARCH
    harness = Harness(fake)
    try:
        with caplog.at_level("INFO"):
            page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in page.places] == ["2gis_101"]
    assert "left alone: 2 (spb-region: 1, spbx: 1)" in caplog.text


async def test_a_click_that_leaves_the_search_ends_the_page_and_keeps_what_the_click_gave(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stat = "?stat=c3RhdA%3D%3D"
    fake = UrlRemote(
        pages={1: _doc_with([f"/spb/firm/101{stat}", f"/spb/firm/102{stat}", f"/spb/firm/103{stat}"])},
        cards=_cards("101", "102", "103"),
        leave_on=("/firm/102",),
    )
    fake.url = SPB_SEARCH
    harness = Harness(fake)
    try:
        with caplog.at_level("INFO"):
            page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in page.places] == ["2gis_101", "2gis_102"]  # the card of the click is kept
    assert page.has_more is False and len(fake.clicks) == 2  # the third is not clicked: no way back
    assert "the click led away from the search page, to /spb/firm" in caplog.text
    assert "not read after" not in caplog.text and harness.notes == []  # not an unreadable card, not a refusal


async def test_a_click_that_leaves_without_an_answer_of_the_catalog_still_ends_the_page_quietly() -> None:
    stat = "?stat=c3RhdA%3D%3D"
    fake = UrlRemote(
        pages={1: _doc_with([f"/spb/firm/101{stat}", f"/spb/firm/102{stat}"])},
        cards=_cards("101"),  # nothing answers to the second one
        leave_on=("/firm/102",),
    )
    fake.url = SPB_SEARCH
    harness = Harness(fake)
    try:
        page = await harness.provider.search(AUTO, KALININSKY)
    finally:
        await harness.provider.close()
    assert [place.external_id for place in page.places] == ["2gis_101"]
    assert len(fake.clicks) == 2  # not three attempts on the second: it is over at once


async def test_the_run_goes_on_with_the_next_query_after_a_page_was_left() -> None:
    stat = "?stat=c3RhdA%3D%3D"
    fake = UrlRemote(
        pages={1: _doc_with([f"/spb/firm/101{stat}", f"/spb/firm/102{stat}"])},
        cards=_cards("101", "102"),
        leave_on=("/firm/102",),
    )
    harness = Harness(fake)
    try:
        first = await harness.provider.search(AUTO, KALININSKY)
        assert [place.external_id for place in first.places] == ["2gis_101", "2gis_102"]
        fake.leave_on = ()  # the next query is read as usual: the page is opened anew
        second = await harness.provider.search(_query(2), KALININSKY)
    finally:
        await harness.provider.close()
    assert len(second.places) == 2


async def test_two_pages_left_in_one_run_stop_the_run_with_a_clear_reason() -> None:
    stat = "?stat=c3RhdA%3D%3D"
    fake = UrlRemote(
        pages={1: _doc_with([f"/spb/firm/101{stat}", f"/spb/firm/102{stat}"])},
        cards=_cards("101", "102"),
        leave_on=("/firm/102",),
    )
    harness = Harness(fake)
    try:
        await harness.provider.search(AUTO, KALININSKY)  # the first time: the page is over, the run goes on
        with pytest.raises(ProviderFormatError, match="клик увёл со страницы поиска 2-й раз за сбор") as stopped:
            await harness.provider.search(_query(2), KALININSKY)
        assert [place.external_id for place in stopped.value.partial] == ["2gis_101", "2gis_102"]
        assert harness.notes == []  # not a refusal of the site
    finally:
        await harness.provider.close()
    # a new run starts the count anew
    fake.leave_on = ()
    again = Harness(fake)
    try:
        assert len((await again.provider.search(_query(3), KALININSKY)).places) == 2
    finally:
        await again.provider.close()


# ---------- 6. blocks recorded by the existing mechanism ----------


@pytest.fixture
async def memory_session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


async def test_three_blocks_in_a_row_switch_the_provider_off(memory_session_maker) -> None:
    settings = _settings(COLLECT_QUERIES="dentist_clinic", COLLECT_DISTRICTS="Центральный")
    config = load_collector_config(settings)
    fake = FakeRemote(pages={1: _doc(["101"])}, page_info=CAPTCHA_PAGE)
    harness = Harness(fake, min_hours_between_runs=0)

    for number in range(1, 4):
        before = datetime.now(UTC)
        await collect_leads(memory_session_maker, [harness.provider], config)
        async with memory_session_maker() as session:
            state = (await get_provider_states(session))["2gis_cdp"]
            assert state.consecutive_blocks == number
            if number == 1:
                assert state.disabled_at is None
                assert state.blocked_until >= before + timedelta(hours=settings.SCRAPER_BLOCK_BACKOFF_HOURS)
            state.blocked_until = datetime.now(UTC) - timedelta(seconds=1)  # the pause has passed
            await session.commit()

    async with memory_session_maker() as session:
        state = (await get_provider_states(session))["2gis_cdp"]
    assert state.consecutive_blocks == 3
    assert state.disabled_at is not None
    assert fake.clicks == []


# ---------- 7. the DevTools address stays on this computer ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9222",
        "https://127.0.0.1:9222/json",
        "ws://localhost:9222/devtools/browser/abc",
        "wss://localhost:9222",
        "http://[::1]:9222",
    ],
)
def test_local_cdp_url_is_accepted(url: str) -> None:
    assert ensure_local_cdp_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:9222",
        "http://example.com:9222",
        "http://0.0.0.0:9222",
        "http://127.0.0.1",
        "ws://localhost/devtools",
        "http://[::1]",
        "ftp://127.0.0.1:21",
        "http://127.0.0.1:notaport",
    ],
)
def test_remote_cdp_url_is_refused(url: str) -> None:
    with pytest.raises(ValueError):
        ensure_local_cdp_url(url)


def test_chrome_remote_checks_its_devtools_address(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    checked: list[str] = []

    class _Browser:
        remote_port = 9333
        returncode = None

        def __init__(self, _options: Any) -> None:
            return None

        def close(self) -> None:
            closed.append(True)

    def _refuse(url: str) -> str:
        checked.append(url)
        raise ValueError("не локальный адрес")

    monkeypatch.setattr(remote, "ChromeBrowser", _Browser)
    monkeypatch.setattr(remote, "patch_pychrome", lambda: None)
    monkeypatch.setattr(remote, "ensure_local_cdp_url", _refuse)
    # requests is in requirements-cdp.txt only: a stand-in for stop().
    fake_exceptions = types.ModuleType("requests.exceptions")
    fake_exceptions.RequestException = type("RequestException", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "requests.exceptions", fake_exceptions)
    monkeypatch.setitem(sys.modules, "requests", types.ModuleType("requests"))
    chrome = ChromeRemote(parsers.ChromeOptions(), [])
    with pytest.raises(ValueError):
        chrome.start()
    assert checked == ["http://127.0.0.1:9333"]
    assert closed == [True]  # the browser it started is closed again


# ---------- 8. nothing that hides automation ----------

FORBIDDEN_WORDS = (
    "user_agent",
    "useragent",
    "setuseragentoverride",
    "webdriver",
    "stealth",
    "evaluateonnewdocument",
    "automationcontrolled",
    "humanize",
    "fingerprint",
    "rotat",  # no rotation of proxies: one fixed proxy of the right country, nothing more
    "google.com",
    "dispatchmouse",
)


def test_sources_contain_nothing_that_hides_automation() -> None:
    sources = sorted((PROJECT_ROOT / "src" / "services" / "scraper" / "chrome").glob("*.py"))
    sources.append(PROJECT_ROOT / "src" / "services" / "providers" / "twogis_cdp.py")
    assert len(sources) >= 9
    for path in sources:
        text = Path(path).read_text(encoding="utf-8").lower()
        found = [word for word in FORBIDDEN_WORDS if word in text]
        assert found == [], f"{path.name}: {found}"
