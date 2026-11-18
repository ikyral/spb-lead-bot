"""The light mode of the browser of 2GIS: memory limit, no heavy resources, restarts, and the reasons of rejection."""

import logging
import subprocess
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings
from src.services import pipeline
from src.services.api_client import ProviderProxyError
from src.services.providers.base import RawPlace, SearchArea, SearchQuery
from src.services.providers.twogis_cdp import CdpOptions, TwoGisCdpProvider
from src.services.scraper.chrome import (
    ChromeBrowser,
    ChromeOptions,
    ChromeRemote,
    ChromeScraperSession,
    ProxyError,
    parsers,
)
from src.services.scraper.chrome.filters import HEAVY_RESOURCE_PATTERNS
from src.services.scraper.snapshot import PageSnapshot

QUERY = SearchQuery(text="стоматология", category="dentist_clinic", high_margin=True)
AREA = SearchArea(bbox=(59.9, 30.3, 59.95, 30.4), district="Центральный")


# ---------- settings ----------


def test_defaults_are_made_for_a_small_server() -> None:
    settings = Settings(_env_file=None)
    assert settings.TWOGIS_CHROME_MEMORY_MB == 300
    assert settings.TWOGIS_LIGHT_MODE is True
    assert settings.TWOGIS_RESTART_EVERY_CARDS == 20
    assert settings.SCRAPER_CDP_MAX_RESULTS_PER_RUN == 30

    options = CdpOptions.from_settings(settings)
    assert (options.chrome_memory_mb, options.light, options.restart_every_cards) == (300, True, 20)
    assert options.max_results_per_run == 30


def test_memory_limit_is_zero_for_the_default_of_chrome_or_at_least_100_mb() -> None:
    assert Settings(_env_file=None, TWOGIS_CHROME_MEMORY_MB="0").TWOGIS_CHROME_MEMORY_MB == 0
    assert Settings(_env_file=None, TWOGIS_CHROME_MEMORY_MB="1500").TWOGIS_CHROME_MEMORY_MB == 1500
    for bad in ("50", "-1", "много"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, TWOGIS_CHROME_MEMORY_MB=bad)


def test_restart_setting_can_be_turned_off_and_has_a_ceiling() -> None:
    assert Settings(_env_file=None, TWOGIS_RESTART_EVERY_CARDS="0").TWOGIS_RESTART_EVERY_CARDS == 0
    with pytest.raises(ValidationError):
        Settings(_env_file=None, TWOGIS_RESTART_EVERY_CARDS="5000")


def test_env_example_documents_the_new_settings() -> None:
    example = Settings(_env_file=Path(__file__).parent.parent / ".env.example")
    assert (example.TWOGIS_CHROME_MEMORY_MB, example.TWOGIS_LIGHT_MODE, example.TWOGIS_RESTART_EVERY_CARDS) == (
        300,
        True,
        20,
    )
    assert example.SCRAPER_CDP_MAX_RESULTS_PER_RUN == 30


# ---------- the command line of Chrome ----------


def _cmd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **options: Any) -> list[str]:
    fake_bin = tmp_path / "chrome"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    spawned: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kwargs: Any) -> Any:
        spawned.append(cmd)
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    ChromeBrowser(ChromeOptions(binary_path=fake_bin, profile_dir=tmp_path / "profile", **options))
    return spawned[0]


def test_light_mode_adds_the_flags_for_a_small_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cmd = _cmd(monkeypatch, tmp_path, light=True, memory_limit=300)
    for flag in (
        "--renderer-process-limit=1",
        "--disable-extensions",
        "--disable-dev-shm-usage",
        "--blink-settings=imagesEnabled=false",
        "--js-flags=--expose-gc --max-old-space-size=300",
    ):
        assert flag in cmd


def test_without_the_light_mode_the_browser_keeps_its_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cmd = _cmd(monkeypatch, tmp_path)
    assert not any(
        arg.startswith(("--renderer-process-limit", "--disable-extensions", "--blink-settings")) for arg in cmd
    )
    assert "--disable-dev-shm-usage" not in cmd


def test_the_session_gives_the_memory_limit_to_chrome() -> None:
    gate = lambda _action: None  # noqa: E731
    limited = ChromeScraperSession(gate=gate, memory_limit_mb=300, light=True)  # type: ignore[arg-type,return-value]
    assert limited._chrome_options.memory_limit == 300 and limited._chrome_options.light
    automatic = ChromeScraperSession(gate=gate, memory_limit_mb=0)  # type: ignore[arg-type,return-value]
    assert automatic._chrome_options.memory_limit > 0 and not automatic._chrome_options.light


# ---------- the heavy resources are not asked for ----------


def _remote_with_tab(**options: Any) -> tuple[ChromeRemote, MagicMock]:
    remote = ChromeRemote(ChromeOptions(**options), [])
    tab = MagicMock()
    remote._chrome_tab = tab
    remote._setup_tab()
    return remote, tab


def test_light_mode_blocks_what_only_draws_the_page() -> None:
    _, tab = _remote_with_tab(light=True)
    tab.Network.setBlockedURLs.assert_called_once_with(urls=list(HEAVY_RESOURCE_PATTERNS))
    patterns = " ".join(HEAVY_RESOURCE_PATTERNS)
    for drawn in ("fonts", "tile", "styles", "photo", "video"):
        assert drawn in patterns


def test_the_site_own_logs_metrics_and_ads_are_not_blocked() -> None:
    patterns = " ".join(HEAVY_RESOURCE_PATTERNS).lower()
    for telemetry in ("/_/log", "metrics", "analytics", "counter", "/ads", "mc.yandex", "privacy"):
        assert telemetry not in patterns
    # and nothing that the card itself is read from
    assert not any("catalog.api" in pattern for pattern in HEAVY_RESOURCE_PATTERNS)


def test_nothing_is_blocked_without_the_light_mode() -> None:
    _, tab = _remote_with_tab()
    tab.Network.setBlockedURLs.assert_not_called()


def test_an_old_browser_that_cannot_block_is_only_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    import pychrome

    remote = ChromeRemote(ChromeOptions(light=True), [])
    tab = MagicMock()
    tab.Network.setBlockedURLs.side_effect = pychrome.CallMethodException("no such method")
    remote._chrome_tab = tab
    with caplog.at_level(logging.WARNING):
        remote._setup_tab()
    assert "Could not block" in caplog.text


def test_own_refusals_are_counted_and_are_not_failures_of_the_site() -> None:
    remote, tab = _remote_with_tab(light=True)
    tab.Network.loadingFailed(requestId="1", blockedReason="inspector", errorText="net::ERR_BLOCKED_BY_CLIENT")
    tab.Network.loadingFailed(requestId="2", blockedReason="inspector", errorText="net::ERR_BLOCKED_BY_CLIENT")
    assert remote.blocked_count == 2
    assert remote._requests == {}  # nothing stored as a failed response


# ---------- Chrome is started anew after N cards ----------


class FakeRemote:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def start(self) -> None:
        self.log.append("start")

    def stop(self) -> None:
        self.log.append("stop")


class StubReader:
    """Stands in for the reader of search pages: every read returns the cards that were prepared."""

    cards_per_read = 2
    reads: ClassVar[list[str]] = []

    def __init__(self, remote: Any, *_args: Any, **_kwargs: Any) -> None:
        self.remote = remote

    def reset(self) -> None:
        return None

    def read_page(self, url: str, page: int = 1, **_kwargs: Any) -> parsers.ScrapedSearchResult:
        StubReader.reads.append(f"{id(self.remote)}:{page}")
        snapshot = PageSnapshot(status=200, final_url=url, title="t", html="<html></html>")
        items = [{"n": number} for number in range(self.cards_per_read)]
        return parsers.ScrapedSearchResult(snapshot=snapshot, items=items, has_more=True)


@pytest.fixture
def stub_reader(monkeypatch: pytest.MonkeyPatch) -> type[StubReader]:
    StubReader.reads = []
    monkeypatch.setattr(parsers, "SearchPageReader", StubReader)
    return StubReader


async def _session(log: list[str], restart_every_cards: int) -> ChromeScraperSession:
    session = ChromeScraperSession(
        gate=lambda _action: None,  # type: ignore[arg-type,return-value]
        remote_factory=lambda _options, _patterns: FakeRemote(log),  # type: ignore[arg-type,return-value]
        restart_every_cards=restart_every_cards,
    )
    await session.start()
    return session


URL = "https://2gis.ru/spb/search/x"


async def test_chrome_is_started_anew_when_the_cards_are_read(stub_reader: type[StubReader]) -> None:
    log: list[str] = []
    session = await _session(log, restart_every_cards=4)
    await session.read_search(URL, 1)  # 2 cards
    await session.read_search(URL, 2)  # 4 cards
    assert log == ["start"]
    await session.read_search(URL, 3)  # the limit is reached: a new browser first
    assert log == ["start", "stop", "start"]
    assert len({entry.split(":")[0] for entry in stub_reader.reads}) == 2  # the last read went through a new reader
    await session.read_search(URL, 4)
    assert log == ["start", "stop", "start"]  # the count began anew


async def test_without_a_limit_chrome_is_never_restarted(stub_reader: type[StubReader]) -> None:
    log: list[str] = []
    session = await _session(log, restart_every_cards=0)
    for page in range(1, 8):
        await session.read_search(URL, page)
    assert log == ["start"]


async def test_the_proxy_is_checked_again_and_a_failure_stops_the_run(stub_reader: type[StubReader]) -> None:
    log: list[str] = []
    starts = {"count": 0}

    class Remote(FakeRemote):
        def start(self) -> None:
            starts["count"] += 1
            if starts["count"] == 2:
                raise ProxyError("Внешний IP из страны DE")
            super().start()

    session = ChromeScraperSession(
        gate=lambda _action: None,  # type: ignore[arg-type,return-value]
        remote_factory=lambda _options, _patterns: Remote(log),  # type: ignore[arg-type,return-value]
        restart_every_cards=2,
    )
    await session.start()
    await session.read_search(URL, 1)
    with pytest.raises(ProviderProxyError):
        await session.read_search(URL, 2)
    assert log == ["start", "stop"]  # the old browser is closed, the new one did not come up


# ---------- why a place is not a lead ----------


@pytest.fixture
async def session(session_maker: Any) -> Any:
    async with session_maker() as open_session:
        yield open_session


def _place(**fields: Any) -> RawPlace:
    values: dict[str, Any] = {
        "external_id": "2gis_1",
        "source": "2gis",
        "name": "Улыбка",
        "category": "dentist_clinic",
        "phone": "+7 812 555-01-01",
        "raw_website": "https://smile.example",
        "high_margin": True,
        "contacts_known": True,
    }
    return RawPlace(**(values | fields))


async def test_a_working_site_is_named_as_the_reason(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def site_works(url: str, client: Any = None) -> tuple[str, None]:
        return "site_found", None

    monkeypatch.setattr(pipeline, "analyze_website", site_works)
    with caplog.at_level(logging.INFO, logger="src.services.pipeline"):
        stats = await pipeline._process_and_save_places(session, [_place()])
    assert stats["qualified"] == 0
    expected = "Place [dentist_clinic] not-a-lead (2gis_1): the site works (https://smile.example), status site_found"
    assert expected in caplog.text


async def test_a_low_score_is_named_with_the_numbers(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def dead_site(url: str, client: Any = None) -> tuple[str, None]:
        return "dead_site", None

    monkeypatch.setattr(pipeline, "analyze_website", dead_site)
    monkeypatch.setattr(pipeline, "calculate_lead_score", lambda **_kwargs: 5)
    with caplog.at_level(logging.INFO, logger="src.services.pipeline"):
        stats = await pipeline._process_and_save_places(session, [_place()])
    assert stats["qualified"] == 0
    assert "score 5 is below 30 (site status dead_site)" in caplog.text


async def test_a_duplicate_and_a_known_place_are_named(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def no_site(url: str | None, client: Any = None) -> tuple[str, None]:
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", no_site)
    place = _place(raw_website=None)
    await pipeline._process_and_save_places(session, [place])
    with caplog.at_level(logging.INFO, logger="src.services.pipeline"):
        await pipeline._process_and_save_places(session, [place])
        caplog.clear()
        await pipeline.save_collected_places(session, [place])
    assert "Already in the database, not checked again: 1 of 1 places" in caplog.text


async def test_a_duplicate_found_after_the_check_is_named(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def no_site(url: str | None, client: Any = None) -> tuple[str, None]:
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", no_site)
    place = _place(raw_website=None)
    await pipeline._process_and_save_places(session, [place])
    with caplog.at_level(logging.INFO, logger="src.services.pipeline"):
        stats = await pipeline._process_and_save_places(session, [place])
    assert stats["duplicates_skipped"] == 1
    assert "Place [dentist_clinic] known (2gis_1): the lead is already in the database" in caplog.text


def test_cards_the_provider_leaves_out_are_named(caplog: pytest.LogCaptureFixture) -> None:
    provider = TwoGisCdpProvider(CdpOptions())
    payloads = [
        {
            "result": {
                "items": [
                    {"id": "1", "name": "Здание", "type": "building"},
                    {"id": "2", "name": "Без контактов", "type": "branch"},
                ]
            }
        }
    ]
    with caplog.at_level(logging.INFO, logger="src.services.providers.twogis_cdp"):
        places = provider._places(payloads, QUERY, AREA)
    assert places == []
    assert "'Здание' is left out: not an organization (type 'building')" in caplog.text
    assert "'Без контактов' is left out: the card does not say its contacts" in caplog.text


# ---------- a node that went stale between the search and the click ----------


def test_a_stale_node_is_an_error_the_reader_can_retry() -> None:
    import pychrome

    from src.services.scraper.chrome import DOMNode
    from src.services.scraper.chrome.exceptions import ChromeRuntimeException

    remote = ChromeRemote(ChromeOptions(), [])
    tab = MagicMock()
    tab._stopped.is_set.return_value = False
    tab.DOM.resolveNode.side_effect = pychrome.CallMethodException("DOM.resolveNode error: No node with given id found")
    remote._chrome_tab = tab
    node = DOMNode(
        nodeId=1, backendNodeId=10, nodeType=1, nodeName="A", localName="a", nodeValue="", attributes=["href", "/x"]
    )
    with pytest.raises(ChromeRuntimeException, match="No node with given id"):
        remote.perform_click(node)


# ---------- finding a link by a selector instead of reading the whole page ----------


def _remote_for_lookup(tab: MagicMock) -> ChromeRemote:
    remote = ChromeRemote(ChromeOptions(), [])
    tab._stopped.is_set.return_value = False
    remote._chrome_tab = tab
    return remote


def _tab_with_link(node_id: int = 7, backend_id: int = 70, href: str = "/spb/firm/101?stat=c3RhdA%3D%3D") -> MagicMock:
    tab = MagicMock()
    tab.DOM.getDocument.return_value = {"root": {"nodeId": 1}}
    tab.DOM.querySelector.return_value = {"nodeId": node_id}
    tab.DOM.describeNode.return_value = {
        "node": {
            "backendNodeId": backend_id,
            "nodeType": 1,
            "nodeName": "A",
            "localName": "a",
            "nodeValue": "",
            "attributes": ["href", href],
        }
    }
    return tab


def test_a_link_is_found_by_its_address_without_reading_the_tree_of_the_page() -> None:
    href = "/spb/firm/101?stat=c3RhdA%3D%3D"
    tab = _tab_with_link(href=href)
    node = _remote_for_lookup(tab).find_link_by_href(href)
    assert node is not None
    assert (node.id, node.backend_id, node.attributes["href"]) == (7, 70, href)
    tab.DOM.getDocument.assert_called_once_with(depth=0)  # the root only, not the whole tree
    tab.DOM.querySelector.assert_called_once_with(nodeId=1, selector=f'a[href="{href}"]')


def test_quotes_and_backslashes_of_the_address_are_escaped_in_the_selector() -> None:
    tab = _tab_with_link()
    _remote_for_lookup(tab).find_link_by_href('/a"b\\c')
    assert tab.DOM.querySelector.call_args.kwargs["selector"] == 'a[href="/a\\"b\\\\c"]'


def test_a_link_that_is_not_there_is_none_and_nothing_more_is_asked() -> None:
    tab = _tab_with_link(node_id=0)
    assert _remote_for_lookup(tab).find_link_by_href("/spb/firm/9") is None
    tab.DOM.describeNode.assert_not_called()


def test_a_failure_of_the_browser_command_is_a_runtime_error_the_reader_can_fall_back_from() -> None:
    import pychrome

    from src.services.scraper.chrome.exceptions import ChromeRuntimeException

    tab = _tab_with_link()
    tab.DOM.querySelector.side_effect = pychrome.CallMethodException("DOM.querySelector error: bad selector")
    with pytest.raises(ChromeRuntimeException, match="quick search of a link failed"):
        _remote_for_lookup(tab).find_link_by_href("/spb/firm/101")
