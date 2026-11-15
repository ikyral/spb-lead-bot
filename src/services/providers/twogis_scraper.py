"""
2GIS through its public web pages via Chrome DevTools Protocol, without an API key.

Polite by design: one page at a time, a random pause of several seconds between pages, a limit
of pages and results per run, robots.txt, no more than one run a day. At the first sign of a
refusal (captcha, 401/403/429, a page that came empty) it stops; there is no code that tries to
get past any of these. Reacting to a block over time is the job of the collector. A page that
loaded normally but is not understood is not a refusal: reading stops, and nothing counts against
the site or uses up the rest between runs. The same goes for no connection at all.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from src.config import Settings, resolve_path
from src.services.api_client import (
    FORMAT_NOT_RECOGNIZED_TEXT,
    NO_CONNECTION_TEXT,
    ProviderBlockedError,
    ProviderBudgetExhausted,
    ProviderError,
    ProviderFormatError,
    ProviderNoConnectionError,
    ProviderStoppedError,
    ProviderTemporaryError,
    ProviderUnavailableError,
)
from src.services.providers.base import BaseMapProvider, RawPlace, SearchArea, SearchPage, SearchQuery
from src.services.providers.twogis import extract_contacts, parse_catalog_items
from src.services.scraper.chrome.parsers import ChromeScraperSession, ScrapedSearchResult, classify_2gis_url
from src.services.scraper.robots import RobotsNoConnectionError, RobotsRules, RobotsUnavailableError, fetch_robots
from src.services.scraper.snapshot import PageSnapshot
from src.services.twogis_links import (
    BASE_URL,
    FROM_LINKS_KEY,
    build_card_url,
    build_search_url,
    detect_block,
    is_card_response,
    read_card_body,
    read_card_links,
    refusal_in_bodies,
)

logger = logging.getLogger(__name__)

# The token for robots.txt: the rules for '*' apply unless the site names this bot.
ROBOTS_AGENT: str = "spb-lead-bot"

# In a row: the format of the site has changed, and all the other cards would load it for nothing.
MAX_UNRECOGNIZED_IN_A_ROW: int = 3
_SERVER_ERROR: int = 500

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class ScraperOptions:
    """Limits and configuration of one run."""

    city_slug: str = "spb"
    min_delay_seconds: float = 3.0
    max_delay_seconds: float = 8.0
    max_pages_per_run: int = 200
    max_results_per_run: int = 300
    min_hours_between_runs: float = 20.0
    page_timeout_seconds: float = 45.0
    headless: bool = True
    profile_dir: Path | None = None
    chrome_path: Path | None = None
    disable_images: bool = True
    silent_browser: bool = True
    click_delay_ms: int = 0
    use_gc: bool = False
    ignore_interval: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> ScraperOptions:
        raw_chrome_path = settings.SCRAPER_CHROME_PATH.strip()
        return cls(
            city_slug=settings.SCRAPER_CITY_SLUG,
            min_delay_seconds=settings.SCRAPER_MIN_DELAY_SECONDS,
            max_delay_seconds=settings.SCRAPER_MAX_DELAY_SECONDS,
            max_pages_per_run=settings.SCRAPER_MAX_PAGES_PER_RUN,
            max_results_per_run=settings.SCRAPER_MAX_RESULTS_PER_RUN,
            min_hours_between_runs=settings.SCRAPER_MIN_HOURS_BETWEEN_RUNS,
            page_timeout_seconds=settings.SCRAPER_PAGE_TIMEOUT_SECONDS,
            headless=settings.SCRAPER_HEADLESS,
            chrome_path=resolve_path(raw_chrome_path) if raw_chrome_path else None,
            disable_images=settings.SCRAPER_DISABLE_IMAGES,
            click_delay_ms=settings.SCRAPER_CLICK_DELAY_MS,
            use_gc=settings.SCRAPER_USE_GC,
        )


@dataclass(frozen=True)
class CardContacts:
    """What the card of an organization lists."""

    phones: list[str]
    website: str | None
    socials: list[str]
    email: str | None


def read_card_contacts(card: dict[str, Any]) -> CardContacts | None:
    """
    The contacts of a card, or None if the card does not say what they are.

    The difference matters: without the field, or with contacts of unknown types, nothing is
    known, and only a card that really lists its contacts can say that a site is not among them.
    """
    groups = card.get("contact_groups")
    if not isinstance(groups, list):
        return None
    phones, websites, socials, emails = extract_contacts(card)
    if groups and not (phones or websites or socials or emails):
        return None
    listed_as_site = _contacts_listed_as_site(groups)
    # A social network given as the site is what the card calls a site: the check will say what it is.
    website = websites[0] if websites else _social_among(listed_as_site)
    # A site is listed but its address cannot be read, or links of a page do not show a site: both say nothing.
    if website is None and (listed_as_site or card.get(FROM_LINKS_KEY)):
        return None
    return CardContacts(phones=phones, website=website, socials=socials, email=emails[0] if emails else None)


def _contacts_listed_as_site(groups: list[Any]) -> list[dict[str, Any]]:
    return [
        contact
        for group in groups
        if isinstance(group, dict) and isinstance(group.get("contacts"), list)
        for contact in group["contacts"]
        if isinstance(contact, dict) and str(contact.get("type") or "").lower().strip() in ("website", "url")
    ]


def _social_among(listed_as_site: list[dict[str, Any]]) -> str | None:
    # Of these only social networks end up among the socials.
    return next(iter(extract_contacts({"contact_groups": [{"contacts": listed_as_site}]})[2]), None)


class BrowserSession(Protocol):
    async def start(self) -> None: ...

    async def visit(self, url: str, wants_body: Callable[[str], bool]) -> PageSnapshot: ...

    async def read_page(self) -> PageSnapshot | None: ...

    async def bring_to_front(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class _SearchReaderSession(BrowserSession, Protocol):
    async def read_search(
        self,
        url: str,
        page: int = 1,
        *,
        max_items: int | None = None,
        block_checker: Callable[[PageSnapshot], str | None] | None = None,
        reset: bool = False,
        continue_current: bool = False,
    ) -> ScrapedSearchResult: ...


RobotsLoader = Callable[[], Awaitable[RobotsRules]]


@dataclass
class _CardResults:
    """Organizations with known contacts, and why reading stopped early, if it did."""

    complete: list[dict[str, Any]] = field(default_factory=list)
    budget_ended: bool = False
    blocked: str | None = None
    stopped: ProviderStoppedError | None = None


class TwoGisScraperProvider(BaseMapProvider):
    """Reads organizations from the JSON that 2gis.ru pages receive by themselves."""

    name = "2gis_scraper"

    def __init__(
        self,
        options: ScraperOptions | None = None,
        session_factory: Callable[[], BrowserSession] | None = None,
        robots_loader: RobotsLoader | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._options = options or ScraperOptions()
        self.min_hours_between_runs = self._options.min_hours_between_runs
        self.ignore_interval = self._options.ignore_interval
        self._session_factory = session_factory or (
            lambda: ChromeScraperSession(
                page_timeout_seconds=self._options.page_timeout_seconds,
                headless=self._options.headless,
                profile_dir=self._options.profile_dir,
                chrome_path=self._options.chrome_path,
                disable_images=self._options.disable_images,
                silent_browser=self._options.silent_browser,
                delay_between_clicks_ms=self._options.click_delay_ms,
                use_gc=self._options.use_gc,
            )
        )
        self._robots_loader = robots_loader or (lambda: fetch_robots(BASE_URL, ROBOTS_AGENT))
        self._sleep = sleep
        self._clock = clock
        self._session: BrowserSession | None = None
        self._robots: RobotsRules | None = None
        self._pages = 0
        self._results = 0
        self._unrecognized_in_a_row = 0
        self.run_counted = False

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        """Not used for manual search: an admin waiting for a reply and long pauses do not go together."""
        return []

    async def search(self, query: SearchQuery, area: SearchArea, page: int = 1, page_size: int = 50) -> SearchPage:
        """Read one page of search results, opening cards of organizations listed without contacts."""
        if self._results >= self._options.max_results_per_run:
            raise ProviderBudgetExhausted("лимит результатов за запуск")

        url = self._search_url(query, area, page)
        if url.startswith(("http://", "https://")) and classify_2gis_url(url) == "firm":
            return await self._search_single_firm_url(url, query, area)

        session = await self._prepare_session_open(url)
        if isinstance(session, _SearchReaderSession):
            return await self._search_via_chrome(session, url, query, area, page)

        snapshot = await self._visit_opened_session(session, url, is_card_response)
        card = self._recognized_card(snapshot, "")
        if card is None:
            return SearchPage(places=[], has_more=False)
        places = self._parse_items_to_places([card], query, area)
        self._results += len(places)
        return SearchPage(places=places, has_more=False)

    async def _search_via_chrome(
        self,
        session: _SearchReaderSession,
        url: str,
        query: SearchQuery,
        area: SearchArea,
        page: int,
    ) -> SearchPage:
        remaining_budget = max(1, self._options.max_results_per_run - self._results)
        try:
            scraped = await session.read_search(
                url,
                page,
                max_items=remaining_budget,
                block_checker=self._detect_snapshot_block,
            )
            raw_items = self._extract_byid_items(scraped.items)
        except ProviderNoConnectionError:
            raise
        except ProviderError:
            self.run_counted = True
            raise

        if scraped.not_found:
            self.run_counted = True
            return SearchPage(places=[], has_more=False)

        if scraped.blocked_reason is not None:
            self.run_counted = True
            partial = self._parse_items_to_places(
                [item for item in raw_items if isinstance(item.get("contact_groups"), list)],
                query,
                area,
            )
            self._results += len(partial)
            raise ProviderBlockedError(scraped.blocked_reason, partial=partial)

        if raw_items:
            self.run_counted = True
            items = await self._with_contacts(raw_items, trust_empty_contacts=True)
            can_continue = (
                scraped.has_more
                and not items.budget_ended
                and (self._results + len(items.complete)) < self._options.max_results_per_run
            )
            return self._finish_search_items(items, query, area, has_more=can_continue)

        self.run_counted = True
        return SearchPage(places=[], has_more=False)

    def _finish_search_items(
        self,
        items: _CardResults,
        query: SearchQuery,
        area: SearchArea,
        *,
        has_more: bool,
    ) -> SearchPage:
        places = self._parse_items_to_places(items.complete, query, area)
        self._results += len(places)
        if items.blocked is not None:
            raise ProviderBlockedError(items.blocked, partial=places)
        if items.stopped is not None:
            items.stopped.partial = places
            raise items.stopped
        return SearchPage(places=places, has_more=has_more)

    def _parse_items_to_places(
        self,
        items: list[dict[str, Any]],
        query: SearchQuery,
        area: SearchArea,
    ) -> list[RawPlace]:
        return parse_catalog_items(
            {"result": {"items": items}},
            area.district,
            query.category,
            query.high_margin,
            city_slug=self._options.city_slug,
        )

    async def _search_single_firm_url(
        self,
        url: str,
        query: SearchQuery,
        area: SearchArea,
    ) -> SearchPage:
        match = re.search(r"/firm/([^/?#]+)", urlsplit(url).path)
        firm_id = match.group(1) if match else ""
        snapshot = await self._open(url, is_card_response)
        card = self._recognized_card(snapshot, firm_id)
        if card is None:
            return SearchPage(places=[], has_more=False)
        places = self._parse_items_to_places([card], query, area)
        self._results += len(places)
        return SearchPage(places=places, has_more=False)

    @staticmethod
    def _extract_byid_items(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        extracted: list[dict[str, Any]] = []
        for payload in payloads:
            result = payload.get("result")
            if not isinstance(result, dict):
                continue
            items = result.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    extracted.append(item)
        return extracted

    @staticmethod
    def _merge_byid_items(prior: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in [*prior, *current]:
            item_id = str(item.get("id") or "")
            if item_id:
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
            merged.append(item)
        return merged

    @staticmethod
    def _detect_snapshot_block(snapshot: PageSnapshot) -> str | None:
        return detect_block(
            snapshot.status, snapshot.final_url, snapshot.title, snapshot.html
        ) or refusal_in_bodies(snapshot.json_bodies)

    async def read_card(self, card_url: str, firm_id: str) -> dict[str, Any] | None:
        """One organization card, by the rules of any other page. None if it was not recognized."""
        snapshot = await self._open(card_url, is_card_response)
        return self._recognized_card(snapshot, firm_id)

    async def close(self) -> None:
        """End the run: the browser is closed and the limits start over next time."""
        session, self._session = self._session, None
        self._robots = None
        self._pages = self._results = self._unrecognized_in_a_row = 0
        self.human_check_passed = False
        self.run_counted = False
        if session is not None:
            await session.close()

    def _search_url(self, query: SearchQuery, area: SearchArea, page: int = 1) -> str:
        raw = query.text.strip()
        parsed_host = (urlsplit(raw).hostname or "").lower()
        if raw.startswith(("http://", "https://")) and re.match(r"^(?:www\.)?2gis\.[^.]+$", parsed_host):
            return raw
        text = f"{query.text} {area.district}" if area.district else query.text
        return build_search_url(self._options.city_slug, text, page)

    async def _prepare_session_open(self, url: str) -> BrowserSession:
        """Enforce run page budget & robots.txt, initialize session if needed, and pause between pages."""
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
            await self._pause_between_pages()

        self._pages += 1
        return self._session

    async def _visit_opened_session(
        self,
        session: BrowserSession,
        url: str,
        wants_body: Callable[[str], bool],
        refusals_from: Callable[[str], bool] | None = None,
    ) -> PageSnapshot:
        try:
            snapshot = await session.visit(url, wants_body)
            bodies = snapshot.json_bodies if refusals_from is None else snapshot.bodies_from(refusals_from)
            reason = detect_block(
                snapshot.status, snapshot.final_url, snapshot.title, snapshot.html
            ) or refusal_in_bodies(bodies)
            if reason:
                raise ProviderBlockedError(reason)
        except ProviderNoConnectionError:
            raise
        except ProviderError:
            # The site did answer: this was a visit, whatever the answer means.
            self.run_counted = True
            raise
        return snapshot

    async def _open(
        self,
        url: str,
        wants_body: Callable[[str], bool],
        refusals_from: Callable[[str], bool] | None = None,
    ) -> PageSnapshot:
        """
        Open a page if the rules allow it, stopping at any sign of a refusal.

        `refusals_from` narrows down whose refusal counts, when more responses are read than the
        page depends on: a side service of the site that answers 403 is not a block.
        """
        session = await self._prepare_session_open(url)
        return await self._visit_opened_session(session, url, wants_body, refusals_from=refusals_from)

    def _recognized_card(self, snapshot: PageSnapshot, firm_id: str) -> dict[str, Any] | None:
        """The card if the code understands what it lists. Without it, the page itself may be what is wrong."""
        if snapshot.status == 404:
            self.run_counted = True
            return None
        card = read_card_body(snapshot.bodies_from_or_all(is_card_response), firm_id)
        if card is None:
            card = read_card_links(snapshot, firm_id)
        if card is None or read_card_contacts(card) is None:
            self._refuse_if_not_loaded(snapshot)
            return None
        self.run_counted = True
        return card

    def _refuse_if_not_loaded(self, snapshot: PageSnapshot) -> None:
        """Only a page that loaded normally can have a format that was not understood."""
        if snapshot.status is not None and snapshot.status >= _SERVER_ERROR:
            self.run_counted = True
            raise ProviderTemporaryError(f"сайт ответил кодом {snapshot.status}")
        if not snapshot.title.strip():
            self.run_counted = True
            raise ProviderBlockedError("страница пришла пустой")

    async def _pause_between_pages(self) -> None:
        # Not cryptography: only spreads requests out in time.
        delay = random.uniform(self._options.min_delay_seconds, self._options.max_delay_seconds)  # noqa: S311
        await self._sleep(delay)

    async def _robots_rules(self) -> RobotsRules:
        if self._robots is None:
            try:
                self._robots = await self._robots_loader()
            except RobotsNoConnectionError:
                raise ProviderNoConnectionError(NO_CONNECTION_TEXT) from None
            except RobotsUnavailableError as exc:
                raise ProviderUnavailableError(f"не удалось прочитать robots.txt: {exc}") from None
        return self._robots

    async def _with_contacts(
        self,
        items: list[dict[str, Any]],
        *,
        trust_empty_contacts: bool = False,
    ) -> _CardResults:
        """
        Keep only organizations whose contacts are known.

        An organization listed without contacts would look like a business with no website,
        so its card is opened; if that is impossible, it is not returned at all.
        """
        result = _CardResults()
        for item in items:
            if item.get("type") not in (None, "branch"):
                continue
            has_groups = isinstance(item.get("contact_groups"), list)
            if item.get("contact_groups") or (trust_empty_contacts and has_groups):
                result.complete.append(item)
                continue
            firm_id = str(item.get("id") or "")
            card_url = build_card_url(self._options.city_slug, firm_id)
            if card_url is None or result.budget_ended or result.blocked or result.stopped:
                continue
            try:
                snapshot = await self._open(card_url, is_card_response)
            except ProviderBudgetExhausted:
                result.budget_ended = True
                continue
            except ProviderBlockedError as exc:
                result.blocked = str(exc)
                continue
            except ProviderStoppedError as exc:
                result.stopped = exc
                continue
            try:
                card = self._recognized_card(snapshot, firm_id)
            except ProviderBlockedError as exc:
                result.blocked = str(exc)
                continue
            if card is None:
                # Not returned: without its contacts it would look like a business with no website.
                self._unrecognized_in_a_row += 1
                if self._unrecognized_in_a_row >= MAX_UNRECOGNIZED_IN_A_ROW:
                    result.stopped = ProviderFormatError(FORMAT_NOT_RECOGNIZED_TEXT)
                continue
            self._unrecognized_in_a_row = 0
            result.complete.append({**item, **card})
        return result
