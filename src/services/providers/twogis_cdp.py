"""
2GIS through its public pages in a Chrome window, driven over the DevTools protocol.

The main source of leads: it runs on the schedule of the bot (COLLECT_PROVIDERS) and by hand
(collect_cli.py). On a server the window is a virtual screen (Xvfb); on a computer a person may sit at it.
The browser goes out through TWOGIS_PROXY (an IP of Russia), and before the first request to 2GIS it checks
the country of the exit IP: from the wrong one, or with a dead proxy, nothing is opened and the run stops.
That is a problem of the network of this side, not a refusal of the site, and it is reported as such.
The browser keeps its own defaults: nothing changes how it presents itself or hides that it is driven.

Every action that reaches the site goes through one gate, in this order: the limits per run (loads
of search pages, i.e. the first page of a target and every pagination click, count against the
page limit; every card click, retries included, against the separate click limit, which is never
above the limit of results), robots.txt for the exact URL (a card by its canonical link, without
stat=), a random pause of at least MIN_PAUSE_SECONDS before every action but the first of a run.
Only then the action.

A search that 2GIS sends to another city (2gis.ru/<other city>/search/..., no captcha, no refusing
status) is not a refusal: the search page of the city is opened once more, and if 2GIS sends it away
again the target is skipped as a format error. A captcha, 401/403/429 and a page not of 2GIS stay refusals.

At the first sign of a refusal (captcha, 401/403/429, an empty page, a page not of 2GIS, a search
page that is not understood) it stops and never tries to pass the check itself. Only the owner's --assisted run on their
own computer waits for a person to pass the captcha; the server and the scheduled run never wait.
The block is recorded by the collector like any other.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote

from pydantic import SecretStr

from src.config import Settings, resolve_path
from src.services.api_client import (
    NO_CONNECTION_TEXT,
    ProviderBlockedError,
    ProviderBudgetExhausted,
    ProviderError,
    ProviderFormatError,
    ProviderNoConnectionError,
    ProviderProxyError,
    ProviderUnavailableError,
)
from src.services.providers.base import BaseMapProvider, RawPlace, SearchArea, SearchPage, SearchQuery
from src.services.providers.twogis_catalog import parse_catalog_items
from src.services.providers.twogis_scraper import CAPTCHA_POLL_SECONDS, ROBOTS_AGENT, call_owner, read_card_contacts
from src.services.scraper import twogis_site
from src.services.scraper.chrome.parsers import (
    BlockChecker,
    ChromeScraperSession,
    Gate,
    GateAction,
    GateDecision,
    ScrapedSearchResult,
    foreign_search_city,
    is_network_error_page,
)
from src.services.scraper.chrome.proxy import parse_proxy
from src.services.scraper.robots import (
    RobotsNoConnectionError,
    RobotsRules,
    RobotsUnavailableError,
    fetch_robots,
    parse_robots,
)
from src.services.scraper.snapshot import PageSnapshot
from src.services.twogis_links import build_card_url

logger = logging.getLogger(__name__)

PROVIDER_NAME: str = "2gis_cdp"
# 2GIS takes a bare name of a district for a street or another city ("Калининский" finds Tver), so a district is
# asked for as "<name> район".
DISTRICT_WORD: str = "район"
# Below this no pause is possible, whatever the settings say.
MIN_PAUSE_SECONDS: float = 3.0
# 2GIS answers 404 to a search that found nothing ("Точных совпадений нет"). That is an empty result, but a ban could
# look the same, so this many such answers in a row, with nothing found in between, are taken for a refusal.
MAX_EMPTY_SEARCHES_IN_A_ROW: int = 5
# A click that led away from the search page ends that page; this many times in a run stop the run.
MAX_LEFT_SEARCH_PAGES: int = 2


@dataclass(frozen=True)
class CdpOptions:
    """Limits of one manual run."""

    city_slug: str = "spb"
    min_delay_seconds: float = 3.0
    max_delay_seconds: float = 8.0
    max_pages_per_run: int = 60
    max_results_per_run: int = 30
    # Card clicks per run, retries included; 0 = as many as max_results_per_run, never more than that.
    max_card_clicks_per_run: int = 0
    min_hours_between_runs: float = 20.0
    page_timeout_seconds: float = 45.0
    profile_dir: Path | None = None
    chrome_path: Path | None = None
    # The proxy with its password: SecretStr, left out of repr(), so it cannot get into a log.
    proxy: SecretStr | None = field(default=None, repr=False)
    proxy_country: str = "OFF"
    timezone: str | None = None
    lang: str | None = None
    headless: bool = False
    assisted: bool = False
    assisted_wait_minutes: float = 5.0
    assisted_max_captchas: int = 2
    # The memory of Chrome: the limit of the V8 heap in MB (0 = what Chrome takes by itself), the light mode
    # (no images, fonts, map tiles), and after how many read cards Chrome is started anew (0 = never).
    chrome_memory_mb: int = 0
    light: bool = False
    restart_every_cards: int = 0
    # A diagnostic run saves every search page here (HTML and the list of its links); None = nothing is saved.
    snapshot_dir: Path | None = None
    # Only the command line of the collector sets this, for one run: see --ignore-interval.
    ignore_interval: bool = False

    def __post_init__(self) -> None:
        if self.min_delay_seconds < MIN_PAUSE_SECONDS:
            raise ValueError(f"пауза перед действием не может быть меньше {MIN_PAUSE_SECONDS:g} с")
        if self.min_delay_seconds > self.max_delay_seconds:
            raise ValueError("минимальная пауза не может быть больше максимальной")
        if self.max_pages_per_run < 1 or self.max_results_per_run < 1:
            raise ValueError("лимиты страниц и результатов за запуск должны быть не меньше 1")
        if self.max_card_clicks_per_run < 0:
            raise ValueError("лимит кликов по карточкам за запуск не может быть меньше 0")

    @property
    def card_click_limit(self) -> int:
        """
        Card clicks allowed per run, retries included: the setting, but never above the limit of results.

        So a run asks 2GIS for no more than max_results_per_run cards plus the loads of its search pages.
        """
        limit = self.max_card_clicks_per_run or self.max_results_per_run
        return min(limit, self.max_results_per_run)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        ignore_interval: bool = False,
        *,
        assisted: bool = False,
    ) -> "CdpOptions":
        chrome_path = settings.SCRAPER_CDP_CHROME_PATH.strip()
        options = cls(
            city_slug=settings.SCRAPER_CITY_SLUG,
            min_delay_seconds=settings.SCRAPER_CDP_MIN_DELAY_SECONDS,
            max_delay_seconds=settings.SCRAPER_CDP_MAX_DELAY_SECONDS,
            max_pages_per_run=settings.SCRAPER_CDP_MAX_PAGES_PER_RUN,
            max_results_per_run=settings.SCRAPER_CDP_MAX_RESULTS_PER_RUN,
            max_card_clicks_per_run=settings.SCRAPER_CDP_MAX_CARD_CLICKS_PER_RUN,
            min_hours_between_runs=settings.SCRAPER_CDP_MIN_HOURS_BETWEEN_RUNS,
            page_timeout_seconds=settings.SCRAPER_CDP_PAGE_TIMEOUT_SECONDS,
            profile_dir=resolve_path(settings.TWOGIS_PROFILE_DIR),
            chrome_path=Path(chrome_path) if chrome_path else None,
            proxy=settings.TWOGIS_PROXY,
            proxy_country=settings.TWOGIS_PROXY_COUNTRY,
            timezone=settings.TWOGIS_TIMEZONE,
            lang=settings.TWOGIS_LANG,
            headless=settings.TWOGIS_HEADLESS,
            chrome_memory_mb=settings.TWOGIS_CHROME_MEMORY_MB,
            light=settings.TWOGIS_LIGHT_MODE,
            restart_every_cards=settings.TWOGIS_RESTART_EVERY_CARDS,
            snapshot_dir=resolve_path(settings.TWOGIS_SNAPSHOT_DIR) if settings.TWOGIS_SNAPSHOT_DIR.strip() else None,
            ignore_interval=ignore_interval,
        )
        if not assisted:
            return options
        return replace(
            options,
            assisted=True,
            headless=False,
            assisted_wait_minutes=settings.SCRAPER_ASSISTED_WAIT_MINUTES,
            assisted_max_captchas=settings.SCRAPER_ASSISTED_MAX_CAPTCHAS,
        )


class CdpSession(Protocol):
    async def start(self) -> None: ...

    async def read_search(
        self,
        url: str,
        page: int = 1,
        *,
        max_items: int | None = None,
        block_checker: BlockChecker | None = None,
        reset: bool = False,
        continue_current: bool = False,
        skip_firm_ids: set[str] | frozenset[str] | None = None,
    ) -> ScrapedSearchResult: ...

    async def read_page(self) -> PageSnapshot | None: ...

    async def bring_to_front(self) -> None: ...

    async def fetch_text(self, url: str) -> tuple[int | None, str]: ...

    async def close(self) -> None: ...


RobotsLoader = Callable[[], Awaitable[RobotsRules]]


class TwoGisCdpProvider(BaseMapProvider):
    """Reads organization cards by clicking them on 2GIS search pages in a visible Chrome window."""

    name = PROVIDER_NAME
    external_id_prefix: str | None = "2gis_"

    def __init__(
        self,
        options: CdpOptions | None = None,
        session_factory: Callable[[Gate], CdpSession] | None = None,
        robots_loader: RobotsLoader | None = None,
        sleep: Callable[[float], None] = time.sleep,
        notify: Callable[[str], None] = call_owner,
        clock: Callable[[], float] = time.monotonic,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._options = options or CdpOptions()
        self.min_hours_between_runs = self._options.min_hours_between_runs
        self.ignore_interval = self._options.ignore_interval
        self._session_factory = session_factory or self._chrome_session
        self._robots_loader = robots_loader or self._fetch_robots
        # Called in the worker thread of the browser, so it is a plain blocking sleep.
        self._sleep = sleep
        self._notify = notify
        self._clock = clock
        self._async_sleep = async_sleep
        self._session: CdpSession | None = None
        self._robots: RobotsRules | None = None
        # Every action that went through the gate (for the pause before the next one), then by kind for the limits.
        self._pages = 0
        self._page_loads = 0
        self._card_clicks = 0
        self._results = 0
        self._captchas = 0
        self._search_text = ""
        self._stop_error: ProviderError | None = None
        self._blocked: str | None = None
        # Why the budget of the run has ended: the next searches stop with the same reason.
        self._budget_ended: str | None = None
        self._proxy_failure: ProviderProxyError | None = None
        self._empty_in_a_row = 0
        self._left_search_pages = 0
        self._known_firm_ids: set[str] = set()
        self.human_check_passed = False
        self.run_counted = False

    def set_known_external_ids(self, external_ids: set[str]) -> None:
        """Store numeric 2GIS firm IDs already saved in the DB so search pages skip clicking them."""
        self._known_firm_ids = {
            firm_id
            for eid in external_ids
            if eid.startswith("2gis_") and (firm_id := eid.removeprefix("2gis_").split("_", 1)[0])
        }

    def _remember_places(self, places: list[RawPlace]) -> None:
        for place in places:
            firm_id = place.external_id.removeprefix("2gis_").split("_", 1)[0]
            if firm_id:
                self._known_firm_ids.add(firm_id)

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        """Not used for the manual search in the bot: it is slow, and the browser pauses between pages."""
        return []

    async def search(self, query: SearchQuery, area: SearchArea, page: int = 1, page_size: int = 50) -> SearchPage:
        """One page of search results: page 1 is opened, the next ones by their pagination link, then every card."""
        if self._proxy_failure is not None:
            raise self._proxy_failure  # the proxy does not work: nothing more is tried in this run
        if self._blocked is not None:
            raise ProviderBlockedError(self._blocked)
        if self._budget_ended is not None:
            raise ProviderBudgetExhausted(self._budget_ended)
        if self._results >= self._options.max_results_per_run:
            raise ProviderBudgetExhausted("лимит результатов за запуск")

        # The browser first: it checks the exit IP before anything is asked of 2GIS, robots.txt included.
        try:
            session = await self._started_session()
        except ProviderProxyError as exc:
            self._proxy_failure = exc
            logger.error("2gis_cdp stopped, the proxy does not work: %s", exc)
            raise
        await self._robots_rules()  # before any page: unknown rules mean nothing is opened
        self._search_text = f"{query.text} {area.district} {DISTRICT_WORD}" if area.district else query.text
        self._stop_error = None
        url = twogis_site.build_search_url(self._options.city_slug, self._search_text)
        try:
            result = await self._read_search_in_city(session, url, page, query, area)
        except ProviderNoConnectionError:
            raise
        except ProviderError:
            # The site did answer, if anything was opened at all.
            self.run_counted = self.run_counted or self._pages > 0
            raise
        self.run_counted = self.run_counted or self._pages > 0
        self.cards_opened += len(result.items)

        places = self._places(result.items, query, area)
        self._remember_places(places)
        self._raise_if_network_page(result, places)
        reason = self._result_problem(result)
        if reason is not None:
            is_captcha = twogis_site.is_captcha(result.snapshot.final_url, result.snapshot.title, result.snapshot.html)
            if is_captcha:
                self.run_counted = True
            if self._options.assisted and is_captcha:
                try:
                    result = await self._after_human_check(
                        session,
                        url,
                        page,
                        max_items=max(0, self._options.max_results_per_run - self._results - len(places)),
                    )
                except ProviderBlockedError as exc:
                    self._stop_on_block(str(exc))
                    raise ProviderBlockedError(str(exc), partial=places) from None
                self.cards_opened += len(result.items)
                retry_places = self._places(result.items, query, area)
                self._remember_places(retry_places)
                places = self._dedupe_places(places, retry_places)
                self._raise_if_network_page(result, places)
                retry_reason = self._result_problem(result)
                if retry_reason is not None:
                    if twogis_site.is_captcha(result.snapshot.final_url, result.snapshot.title, result.snapshot.html):
                        retry_reason = "капча появилась снова сразу после прохождения"
                    self._stop_on_block(retry_reason)
                    raise ProviderBlockedError(retry_reason, partial=places)
            else:
                self._stop_on_block(reason)
                raise ProviderBlockedError(reason, partial=places)

        self._results += len(places)
        if result.unreadable is not None:
            # Cards of this page could not be read, one after another: not a refusal of the site, but not to go on.
            logger.warning("2gis_cdp stopped: %s", result.unreadable)
            raise ProviderFormatError(result.unreadable, partial=places)
        if result.left_search is not None:
            # Not a refusal and not a card that could not be read: the page is over, the next query goes on.
            self._left_search_pages += 1
            logger.warning(
                "2gis_cdp: a click led away from the search page (to %s), the page is left (%d in this run)",
                result.left_search,
                self._left_search_pages,
            )
            if self._left_search_pages >= MAX_LEFT_SEARCH_PAGES:
                reason = (
                    f"клик увёл со страницы поиска {self._left_search_pages}-й раз за сбор (на {result.left_search}): "
                    "страницу или код надо проверить, это не блокировка"
                )
                raise ProviderFormatError(reason, partial=places)
            return SearchPage(places=places, has_more=False)
        stop = self._stop_error
        if stop is not None:
            if isinstance(stop, ProviderBudgetExhausted):
                self._budget_ended = str(stop)
                if places:
                    return SearchPage(places=places, has_more=False)
            raise stop
        has_more = result.has_more and not result.stopped and self._results < self._options.max_results_per_run
        return SearchPage(places=places, has_more=has_more)

    async def close(self) -> None:
        """End the run: the browser is closed and the limits start over next time."""
        session, self._session = self._session, None
        self._robots = None
        self._pages = self._page_loads = self._card_clicks = self._results = self._captchas = 0
        self._stop_error = None
        self._blocked = None
        self._budget_ended = None
        self._proxy_failure = None
        self._empty_in_a_row = 0
        self._left_search_pages = 0
        self._known_firm_ids = set()
        self.human_check_passed = False
        self.run_counted = False
        self.cards_opened = 0
        if session is not None:
            await session.close()

    # ---------- the gate: called in the worker thread before every navigation and click ----------

    def _gate(self, action: GateAction) -> GateDecision:
        # Only loads of search pages count against the page limit; card clicks (retries too) have their own limit.
        if action.kind == "card":
            if self._card_clicks >= self._options.card_click_limit:
                self._stop_error = ProviderBudgetExhausted("лимит кликов по карточкам за запуск")
                return GateDecision.STOP
        elif self._page_loads >= self._options.max_pages_per_run:
            self._stop_error = ProviderBudgetExhausted("лимит страниц за запуск")
            return GateDecision.STOP

        rules = self._robots
        if rules is None:
            self._stop_error = ProviderUnavailableError("robots.txt не прочитан")
            return GateDecision.STOP
        url = self._action_url(action)
        if url is None or not rules.is_allowed(url):
            if action.kind == "open":
                self._stop_error = ProviderUnavailableError("robots.txt сайта запрещает открывать такие страницы")
                return GateDecision.STOP
            logger.info("robots.txt does not allow this %s, it is left out", action.kind)
            return GateDecision.SKIP

        if self._pages:
            self._sleep(self._pause_seconds())
        self._pages += 1
        if action.kind == "card":
            self._card_clicks += 1
        else:
            self._page_loads += 1
        return GateDecision.GO

    def _action_url(self, action: GateAction) -> str | None:
        """The URL robots.txt is checked against: a card by its canonical link, without stat=."""
        if action.kind == "card":
            return build_card_url(self._options.city_slug, action.firm_id)
        if action.kind == "page":
            return twogis_site.build_search_url(self._options.city_slug, self._search_text, action.page)
        return action.url

    def _pause_seconds(self) -> float:
        low = max(MIN_PAUSE_SECONDS, self._options.min_delay_seconds)
        high = max(low, self._options.max_delay_seconds)
        # Not cryptography: only spreads requests out in time.
        return random.uniform(low, high)  # noqa: S311

    # ---------- what the pages showed ----------

    @staticmethod
    def _block_reason(snapshot: PageSnapshot) -> str | None:
        """Checked while a page loads too, so a refusal stops it before any card is clicked."""
        reason = twogis_site.detect_block(snapshot.status, snapshot.final_url, snapshot.title, snapshot.html)
        if reason is not None:
            return reason
        # about:blank and the like only mean that nothing has loaded yet.
        if snapshot.final_url.startswith(("http://", "https://")) and not twogis_site.is_site_response(
            snapshot.final_url
        ):
            return TwoGisCdpProvider._not_2gis(snapshot)
        return None

    @staticmethod
    def _not_2gis(snapshot: PageSnapshot) -> str:
        """The refusal "a page not of 2GIS", with the page in the log: address without query, status and title."""
        logger.warning(
            "2gis_cdp: a page not of 2GIS: %s, status %s, title %r",
            unquote(twogis_site.page_address(snapshot.final_url)),
            snapshot.status,
            snapshot.title[:120],
        )
        return f"открылась страница не с 2ГИС ({twogis_site.page_host(snapshot.final_url)})"

    @staticmethod
    def _raise_if_network_page(result: ScrapedSearchResult, places: list[RawPlace]) -> None:
        """A page of a network failure of the browser (chrome-error://, net::ERR_*) is no connection, not a refusal."""
        snapshot = result.snapshot
        # A sign of a block always wins: the refusal path decides about such a page as before.
        if twogis_site.detect_block(snapshot.status, snapshot.final_url, snapshot.title, snapshot.html) is not None:
            return
        if snapshot.final_url.startswith(("http://", "https://")) and not twogis_site.is_site_response(
            snapshot.final_url
        ):
            return
        if result.blocked_reason is not None or not is_network_error_page(snapshot.final_url, snapshot.title):
            return
        logger.warning(
            "2gis_cdp: the browser shows a network failure page: %s, status %s, title %r",
            unquote(twogis_site.page_address(snapshot.final_url)),
            snapshot.status,
            snapshot.title[:120],
        )
        raise ProviderNoConnectionError(NO_CONNECTION_TEXT, partial=places)

    def _page_problem(self, result: ScrapedSearchResult) -> str | None:
        """A page that is not a normal search page of 2GIS counts as a refusal: reading stops."""
        snapshot = result.snapshot
        blocked = self._block_reason(snapshot)
        if blocked is not None:
            return blocked
        if not snapshot.title.strip():
            return "страница пришла пустой"
        if not twogis_site.is_site_response(snapshot.final_url):
            return self._not_2gis(snapshot)
        if result.not_found:
            # A page of 2GIS itself, with no sign of a captcha: the search found nothing. The target is left out.
            logger.info(
                "2gis_cdp: the search answered 404, taken as nothing found: %s, title %r",
                snapshot.final_url,
                snapshot.title,
            )
            return None
        if result.links_found == 0 and not result.nothing_found:
            return "страница поиска не распознана: нет ни организаций, ни ответа «ничего не найдено»"
        return None

    def _count_empty_searches(self, result: ScrapedSearchResult) -> str | None:
        """Several 404 searches in a row are not a coincidence of empty niches: that is a refusal."""
        if not result.not_found:
            self._empty_in_a_row = 0
            return None
        self._empty_in_a_row += 1
        if self._empty_in_a_row >= MAX_EMPTY_SEARCHES_IN_A_ROW:
            return f"сайт ответил кодом 404 на поиск {self._empty_in_a_row}-й раз подряд, ничего не найдено"
        return None

    def _result_problem(self, result: ScrapedSearchResult) -> str | None:
        reason = result.blocked_reason
        if reason is None and not result.stopped:
            reason = self._page_problem(result)
        if reason is None:
            reason = self._count_empty_searches(result)
        return reason

    def _foreign_city_url(self, result: ScrapedSearchResult) -> str | None:
        """
        The address of another city the search ended on, if that is all that is wrong with the page.

        A captcha, a refusing status or a page not of 2GIS on the way stay refusals: then None.
        """
        address = result.foreign_city_url
        if address is None or result.blocked_reason is not None or self._block_reason(result.snapshot) is not None:
            return None
        if foreign_search_city(address, self._options.city_slug) is None:
            return None
        return address

    async def _read_search_in_city(
        self,
        session: CdpSession,
        url: str,
        page: int,
        query: SearchQuery,
        area: SearchArea,
    ) -> ScrapedSearchResult:
        """
        Read the page of the target; if 2GIS sent the search to another city, open the city's search page once more.

        Sent away again: the target is skipped as a format error (the collector moves its cursor on, no block).
        """
        result = await session.read_search(
            url,
            page,
            max_items=self._options.max_results_per_run - self._results,
            block_checker=self._block_reason,
            continue_current=True,
            skip_firm_ids=self._known_firm_ids,
        )
        foreign = self._foreign_city_url(result)
        if foreign is None:
            return result
        logger.warning(
            "2gis_cdp: the search went to another city, not a block: opened %s, ended on %s; "
            "the search page of the city is opened once more",
            unquote(url),
            unquote(foreign),
        )
        retry = await session.read_search(
            url,
            page,
            max_items=max(0, self._options.max_results_per_run - self._results - len(result.items)),
            block_checker=self._block_reason,
            reset=True,
            continue_current=True,
            skip_firm_ids=self._known_firm_ids | self._payload_firm_ids(result.items),
        )
        combined = replace(retry, items=[*result.items, *retry.items])
        again = self._foreign_city_url(retry)
        if again is None:
            return combined
        logger.warning(
            "2gis_cdp: the search went to another city again: opened %s, ended on %s; the target is skipped",
            unquote(url),
            unquote(again),
        )
        places = self._places(combined.items, query, area)
        self._remember_places(places)
        self._results += len(places)
        city = foreign_search_city(again, self._options.city_slug)
        raise ProviderFormatError(
            f"2ГИС дважды увёл поиск в другой город ({city}): цель пропущена, это не блокировка",
            partial=places,
        )

    @staticmethod
    def _payload_firm_ids(payloads: list[dict[str, Any]]) -> set[str]:
        """Numbers of the organizations in card payloads (`/items/byid`), without the branch suffix."""
        ids: set[str] = set()
        for payload in payloads:
            result = payload.get("result")
            items = result.get("items") if isinstance(result, dict) else None
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and (firm_id := str(item.get("id") or "").split("_", 1)[0]):
                    ids.add(firm_id)
        return ids

    @staticmethod
    def _dedupe_places(first: list[RawPlace], second: list[RawPlace]) -> list[RawPlace]:
        seen: set[str] = set()
        combined: list[RawPlace] = []
        for place in (*first, *second):
            if place.external_id not in seen:
                seen.add(place.external_id)
                combined.append(place)
        return combined

    async def _after_human_check(
        self,
        session: CdpSession,
        url: str,
        page: int,
        *,
        max_items: int,
    ) -> ScrapedSearchResult:
        """Ask the owner to pass the captcha, then reopen the search page once."""
        if self._captchas >= self._options.assisted_max_captchas:
            raise ProviderBlockedError(
                f"сайт снова показал капчу, а запросов к человеку за запуск уже {self._captchas}"
            )
        self._captchas += 1
        await self._wait_for_human(session)
        self.human_check_passed = True
        return await session.read_search(
            url,
            page,
            max_items=max_items,
            block_checker=self._block_reason,
            reset=True,
            continue_current=True,
            skip_firm_ids=self._known_firm_ids,
        )

    async def _wait_for_human(self, session: CdpSession) -> None:
        """Wait until the browser window stops showing the captcha or timeout expires."""
        minutes = self._options.assisted_wait_minutes
        try:
            await session.bring_to_front()
        except ProviderUnavailableError:
            raise ProviderBlockedError("окно браузера закрыто, капча не пройдена") from None
        self._notify(f"Сайт показал капчу. Пройдите проверку в окне браузера, жду до {minutes:g} мин.")
        deadline = self._clock() + minutes * 60
        while self._clock() < deadline:
            await self._async_sleep(CAPTCHA_POLL_SECONDS)
            try:
                shown = await session.read_page()
            except ProviderUnavailableError:
                raise ProviderBlockedError("окно браузера закрыто, капча не пройдена") from None
            if (
                shown is not None
                and shown.final_url.startswith("http")
                and not twogis_site.is_captcha(shown.final_url, shown.title, shown.html)
            ):
                self._notify("Проверка пройдена, продолжаю сбор.")
                return
        raise ProviderBlockedError(f"капча не пройдена за {minutes:g} мин")

    def _stop_on_block(self, reason: str) -> None:
        self._blocked = reason
        # The reason is a text of this code: no URLs, no keys.
        logger.warning("2gis_cdp stopped: %s", reason)
        self._notify(
            f"2ГИС отказал ({reason}). Сбор 2gis_cdp остановлен, повтор только после паузы. "
            "Проверку сайта можно пройти вручную в окне Chrome между запусками."
        )

    def _places(self, payloads: list[dict[str, Any]], query: SearchQuery, area: SearchArea) -> list[RawPlace]:
        """Cards with recognized contacts, as places. A card that does not say its contacts is not returned."""
        cards: list[dict[str, Any]] = []
        for payload in payloads:
            result = payload.get("result")
            items = result.get("items") if isinstance(result, dict) else None
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") not in (None, "branch"):
                    logger.info(
                        "2gis_cdp: %r is left out: not an organization (type %r)", item.get("name"), item.get("type")
                    )
                    continue
                if read_card_contacts(item) is None:
                    logger.info("2gis_cdp: %r is left out: the card does not say its contacts", item.get("name"))
                    continue
                cards.append(item)
        return parse_catalog_items(
            {"result": {"items": cards}},
            area.district,
            query.category,
            query.high_margin,
            city_slug=self._options.city_slug,
        )

    # ---------- the browser and the rules ----------

    def _chrome_session(self, gate: Gate) -> CdpSession:
        return ChromeScraperSession(
            gate=gate,
            page_timeout_seconds=self._options.page_timeout_seconds,
            profile_dir=self._options.profile_dir,
            chrome_path=self._options.chrome_path,
            proxy=self._options.proxy,
            proxy_country=self._options.proxy_country,
            timezone=self._options.timezone,
            lang=self._options.lang,
            headless=self._options.headless,
            memory_limit_mb=self._options.chrome_memory_mb,
            light=self._options.light,
            restart_every_cards=self._options.restart_every_cards,
            snapshot_dir=self._options.snapshot_dir,
            city_slug=self._options.city_slug,
        )

    async def _started_session(self) -> CdpSession:
        if self._session is None:
            session = self._session_factory(self._gate)
            self._session = session  # close() must find it even if starting fails halfway
            await session.start()
        return self._session

    async def _fetch_robots(self) -> RobotsRules:
        proxy = self._options.proxy.get_secret_value() if self._options.proxy is not None else None
        try:
            return await fetch_robots(twogis_site.BASE_URL, ROBOTS_AGENT, proxy=proxy)
        except RobotsUnavailableError as exc:
            is_socks = proxy is not None and parse_proxy(proxy).scheme.startswith("socks")
            if not is_socks or "httpx[socks]" not in str(exc) or self._session is None:
                raise
            robots_url = f"{twogis_site.BASE_URL}/robots.txt"
            try:
                status, text = await self._session.fetch_text(robots_url)
            except ProviderNoConnectionError:
                raise RobotsNoConnectionError(NO_CONNECTION_TEXT) from None
            except ProviderError as fetch_exc:
                raise RobotsUnavailableError(str(fetch_exc)) from None
            if status == 200:
                return parse_robots(text, ROBOTS_AGENT)
            if status is not None and 400 <= status < 500:
                return RobotsRules()
            if status is None or status < 0:
                raise RobotsNoConnectionError("не удалось загрузить robots.txt через браузер") from None
            raise RobotsUnavailableError(f"код {status}") from None

    async def _robots_rules(self) -> RobotsRules:
        if self._robots is None:
            try:
                self._robots = await self._robots_loader()
            except RobotsNoConnectionError:
                raise ProviderNoConnectionError(NO_CONNECTION_TEXT) from None
            except RobotsUnavailableError as exc:
                raise ProviderUnavailableError(f"не удалось прочитать robots.txt: {exc}") from None
        return self._robots
