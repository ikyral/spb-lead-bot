"""
2GIS through its public web pages in a headless browser, without an API key.

Polite by design: one page at a time, a random pause of several seconds between pages, a limit
of pages and results per run, robots.txt, no more than one run a day. At the first sign of a
refusal (captcha, 401/403/429, an unrecognized page) it stops; there is no code that tries to
get past any of these. Reacting to a block over time is the job of the collector.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.services.api_client import (
    ProviderBlockedError,
    ProviderBudgetExhausted,
    ProviderUnavailableError,
)
from src.services.providers.base import BaseMapProvider, RawPlace, SearchArea, SearchPage, SearchQuery
from src.services.providers.twogis import parse_catalog_items
from src.services.scraper import twogis_site
from src.services.scraper.browser import PageSnapshot, PlaywrightSession
from src.services.scraper.robots import RobotsRules, RobotsUnavailableError, fetch_robots

logger = logging.getLogger(__name__)

# The token for robots.txt: the rules for '*' apply unless the site names this bot.
ROBOTS_AGENT: str = "spb-lead-bot"

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class ScraperOptions:
    """Limits of one run."""

    city_slug: str = "spb"
    min_delay_seconds: float = 3.0
    max_delay_seconds: float = 8.0
    max_pages_per_run: int = 200
    max_results_per_run: int = 300
    min_hours_between_runs: float = 20.0
    page_timeout_seconds: float = 45.0


class BrowserSession(Protocol):
    async def start(self) -> None: ...

    async def visit(self, url: str, wants_body: Callable[[str], bool]) -> PageSnapshot: ...

    async def close(self) -> None: ...


RobotsLoader = Callable[[], Awaitable[RobotsRules]]


@dataclass
class _CardResults:
    """Organizations with known contacts, and why reading stopped early, if it did."""

    complete: list[dict[str, Any]] = field(default_factory=list)
    budget_ended: bool = False
    blocked: str | None = None


class TwoGisScraperProvider(BaseMapProvider):
    """Reads organizations from the JSON that 2gis.ru pages receive by themselves."""

    name = "2gis_scraper"

    def __init__(
        self,
        options: ScraperOptions | None = None,
        session_factory: Callable[[], BrowserSession] | None = None,
        robots_loader: RobotsLoader | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._options = options or ScraperOptions()
        self.min_hours_between_runs = self._options.min_hours_between_runs
        self._session_factory = session_factory or (
            lambda: PlaywrightSession(page_timeout_seconds=self._options.page_timeout_seconds)
        )
        self._robots_loader = robots_loader or (lambda: fetch_robots(twogis_site.BASE_URL, ROBOTS_AGENT))
        self._sleep = sleep
        self._session: BrowserSession | None = None
        self._robots: RobotsRules | None = None
        self._pages = 0
        self._results = 0

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        """Not used for manual search: an admin waiting for a reply and long pauses do not go together."""
        return []

    async def search(
        self, query: SearchQuery, area: SearchArea, page: int = 1, page_size: int = 50
    ) -> SearchPage:
        """Read one page of search results, opening cards of organizations listed without contacts."""
        if self._results >= self._options.max_results_per_run:
            raise ProviderBudgetExhausted("лимит результатов за запуск")
        text = f"{query.text} {area.district}" if area.district else query.text
        url = twogis_site.build_search_url(self._options.city_slug, text, page)
        snapshot = await self._open(url, twogis_site.is_search_response)

        data = twogis_site.read_search_bodies(snapshot.json_bodies)
        if data is None:
            raise ProviderBlockedError("страница не распознана: нет данных в ожидаемом формате")

        items = await self._with_contacts(data.items)
        places = parse_catalog_items(
            {"result": {"items": items.complete}}, area.district, query.category, query.high_margin
        )
        self._results += len(places)
        if items.blocked is not None:
            raise ProviderBlockedError(items.blocked, partial=places)

        shown = page * len(data.items)
        has_more = bool(data.items) and not items.budget_ended and (data.total is None or shown < data.total)
        return SearchPage(places=places, has_more=has_more)

    async def close(self) -> None:
        """End the run: the browser is closed and the limits start over next time."""
        session, self._session = self._session, None
        self._robots = None
        self._pages = self._results = 0
        if session is not None:
            await session.close()

    async def _open(self, url: str, wants_body: Callable[[str], bool]) -> PageSnapshot:
        """Open a page if the rules allow it, stopping at any sign of a refusal."""
        if self._pages >= self._options.max_pages_per_run:
            raise ProviderBudgetExhausted("лимит страниц за запуск")
        rules = await self._robots_rules()
        if not rules.is_allowed(url):
            raise ProviderUnavailableError("robots.txt сайта запрещает открывать такие страницы")

        if self._session is None:
            session = self._session_factory()
            self._session = session  # close() must find it even if starting fails halfway
            await session.start()
        elif self._pages:
            # Not cryptography: only spreads requests out in time.
            delay = random.uniform(self._options.min_delay_seconds, self._options.max_delay_seconds)  # noqa: S311
            await self._sleep(delay)

        self._pages += 1
        snapshot = await self._session.visit(url, wants_body)
        reason = twogis_site.detect_block(
            snapshot.status, snapshot.final_url, snapshot.title, snapshot.html
        ) or twogis_site.refusal_in_bodies(snapshot.json_bodies)
        if reason:
            raise ProviderBlockedError(reason)
        return snapshot

    async def _robots_rules(self) -> RobotsRules:
        if self._robots is None:
            try:
                self._robots = await self._robots_loader()
            except RobotsUnavailableError as exc:
                raise ProviderUnavailableError(f"не удалось прочитать robots.txt: {exc}") from None
        return self._robots

    async def _with_contacts(self, items: list[dict[str, Any]]) -> _CardResults:
        """
        Keep only organizations whose contacts are known.

        An organization listed without contacts would look like a business with no website,
        so its card is opened; if that is impossible, it is not returned at all.
        """
        result = _CardResults()
        for item in items:
            if item.get("type") not in (None, "branch"):
                continue
            if item.get("contact_groups"):
                result.complete.append(item)
                continue
            firm_id = str(item.get("id") or "")
            if not firm_id or result.budget_ended or result.blocked:
                continue
            try:
                snapshot = await self._open(
                    twogis_site.build_card_url(self._options.city_slug, firm_id), twogis_site.is_card_response
                )
            except ProviderBudgetExhausted:
                result.budget_ended = True
                continue
            except ProviderBlockedError as exc:
                result.blocked = str(exc)
                continue
            card = twogis_site.read_card_body(snapshot.json_bodies, firm_id)
            if card is None:
                result.blocked = "карточка организации не распознана: нет данных в ожидаемом формате"
                continue
            result.complete.append({**item, **card})
        return result

