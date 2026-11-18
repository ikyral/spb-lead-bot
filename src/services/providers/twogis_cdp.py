"""
2GIS through its public pages in a Chrome window, driven over the DevTools protocol.

The main source of leads: it runs on the schedule of the bot (COLLECT_PROVIDERS) and by hand
(collect_cli.py). On a server the window is a virtual screen (Xvfb); on a computer a person may sit at it.
The browser goes out through TWOGIS_PROXY (an IP of Russia), and before the first request to 2GIS it checks
the country of the exit IP: from the wrong one, or with a dead proxy, nothing is opened and the run stops.
That is a problem of the network of this side, not a refusal of the site, and it is reported as such.
The browser keeps its own defaults: nothing changes how it presents itself or hides that it is driven.

Every action that reaches the site goes through one gate, in this order: the limit of pages per
run (each opened page, pagination click and card counts), robots.txt for the exact URL (a card by
its canonical link, without stat=), a random pause of at least MIN_PAUSE_SECONDS before every
action but the first of a run. Only then the action.

At the first sign of a refusal (captcha, 401/403/429, an empty page, a page not of 2GIS, a search
page that is not understood) it stops: nothing waits for the captcha or tries to pass it. The block
is recorded by the collector like any other; a person may pass the check in the browser between runs.
"""

import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

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
from src.services.providers.twogis_scraper import ROBOTS_AGENT, call_owner, read_card_contacts
from src.services.scraper import twogis_site
from src.services.scraper.chrome.parsers import (
    BlockChecker,
    ChromeScraperSession,
    Gate,
    GateAction,
    GateDecision,
    ScrapedSearchResult,
)
from src.services.scraper.robots import RobotsNoConnectionError, RobotsRules, RobotsUnavailableError, fetch_robots
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


@dataclass(frozen=True)
class CdpOptions:
    """Limits of one manual run."""

    city_slug: str = "spb"
    min_delay_seconds: float = 3.0
    max_delay_seconds: float = 8.0
    max_pages_per_run: int = 60
    max_results_per_run: int = 30
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

    @classmethod
    def from_settings(cls, settings: Settings, ignore_interval: bool = False) -> "CdpOptions":
        chrome_path = settings.SCRAPER_CDP_CHROME_PATH.strip()
        return cls(
            city_slug=settings.SCRAPER_CITY_SLUG,
            min_delay_seconds=settings.SCRAPER_CDP_MIN_DELAY_SECONDS,
            max_delay_seconds=settings.SCRAPER_CDP_MAX_DELAY_SECONDS,
            max_pages_per_run=settings.SCRAPER_CDP_MAX_PAGES_PER_RUN,
            max_results_per_run=settings.SCRAPER_CDP_MAX_RESULTS_PER_RUN,
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
    ) -> ScrapedSearchResult: ...

    async def close(self) -> None: ...


RobotsLoader = Callable[[], Awaitable[RobotsRules]]


class TwoGisCdpProvider(BaseMapProvider):
    """Reads organization cards by clicking them on 2GIS search pages in a visible Chrome window."""

    name = PROVIDER_NAME

    def __init__(
        self,
        options: CdpOptions | None = None,
        session_factory: Callable[[Gate], CdpSession] | None = None,
        robots_loader: RobotsLoader | None = None,
        sleep: Callable[[float], None] = time.sleep,
        notify: Callable[[str], None] = call_owner,
    ) -> None:
        self._options = options or CdpOptions()
        self.min_hours_between_runs = self._options.min_hours_between_runs
        self.ignore_interval = self._options.ignore_interval
        self._session_factory = session_factory or self._chrome_session
        self._robots_loader = robots_loader or self._fetch_robots
        # Called in the worker thread of the browser, so it is a plain blocking sleep.
        self._sleep = sleep
        self._notify = notify
        self._session: CdpSession | None = None
        self._robots: RobotsRules | None = None
        self._pages = 0
        self._results = 0
        self._search_text = ""
        self._stop_error: ProviderError | None = None
        self._blocked: str | None = None
        self._budget_ended = False
        self._proxy_failure: ProviderProxyError | None = None
        self._empty_in_a_row = 0
        self.run_counted = False

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        """Not used for the manual search in the bot: it is slow, and the browser pauses between pages."""
        return []

    async def search(self, query: SearchQuery, area: SearchArea, page: int = 1, page_size: int = 50) -> SearchPage:
        """One page of search results: page 1 is opened, the next ones by their pagination link, then every card."""
        if self._proxy_failure is not None:
            raise self._proxy_failure  # the proxy does not work: nothing more is tried in this run
        if self._blocked is not None:
            raise ProviderBlockedError(self._blocked)
        if self._budget_ended:
            raise ProviderBudgetExhausted("лимит страниц за запуск")
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
            result = await session.read_search(
                url,
                page,
                max_items=self._options.max_results_per_run - self._results,
                block_checker=self._block_reason,
                continue_current=True,
            )
        except ProviderNoConnectionError:
            raise
        except ProviderError:
            # The site did answer, if anything was opened at all.
            self.run_counted = self.run_counted or self._pages > 0
            raise
        self.run_counted = self.run_counted or self._pages > 0

        places = self._places(result.items, query, area)
        reason = result.blocked_reason
        if reason is None and not result.stopped:
            reason = self._page_problem(result)
        if reason is None:
            reason = self._count_empty_searches(result)
        if reason is not None:
            self._stop_on_block(reason)
            raise ProviderBlockedError(reason, partial=places)

        self._results += len(places)
        if result.unreadable is not None:
            # Cards of this page could not be read, one after another: not a refusal of the site, but not to go on.
            logger.warning("2gis_cdp stopped: %s", result.unreadable)
            raise ProviderFormatError(result.unreadable, partial=places)
        stop = self._stop_error
        if stop is not None:
            if isinstance(stop, ProviderBudgetExhausted):
                self._budget_ended = True
                if places:
                    return SearchPage(places=places, has_more=False)
            raise stop
        has_more = result.has_more and not result.stopped and self._results < self._options.max_results_per_run
        return SearchPage(places=places, has_more=has_more)

    async def close(self) -> None:
        """End the run: the browser is closed and the limits start over next time."""
        session, self._session = self._session, None
        self._robots = None
        self._pages = self._results = 0
        self._stop_error = None
        self._blocked = None
        self._budget_ended = False
        self._proxy_failure = None
        self._empty_in_a_row = 0
        self.run_counted = False
        if session is not None:
            await session.close()

    # ---------- the gate: called in the worker thread before every navigation and click ----------

    def _gate(self, action: GateAction) -> GateDecision:
        if self._pages >= self._options.max_pages_per_run:
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
            return "открылась страница не с 2ГИС"
        return None

    def _page_problem(self, result: ScrapedSearchResult) -> str | None:
        """A page that is not a normal search page of 2GIS counts as a refusal: reading stops."""
        snapshot = result.snapshot
        blocked = self._block_reason(snapshot)
        if blocked is not None:
            return blocked
        if not snapshot.title.strip():
            return "страница пришла пустой"
        if not twogis_site.is_site_response(snapshot.final_url):
            return "открылась страница не с 2ГИС"
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
        )

    async def _started_session(self) -> CdpSession:
        if self._session is None:
            session = self._session_factory(self._gate)
            self._session = session  # close() must find it even if starting fails halfway
            await session.start()
        return self._session

    async def _fetch_robots(self) -> RobotsRules:
        proxy = self._options.proxy.get_secret_value() if self._options.proxy is not None else None
        return await fetch_robots(twogis_site.BASE_URL, ROBOTS_AGENT, proxy=proxy)

    async def _robots_rules(self) -> RobotsRules:
        if self._robots is None:
            try:
                self._robots = await self._robots_loader()
            except RobotsNoConnectionError:
                raise ProviderNoConnectionError(NO_CONNECTION_TEXT) from None
            except RobotsUnavailableError as exc:
                raise ProviderUnavailableError(f"не удалось прочитать robots.txt: {exc}") from None
        return self._robots
