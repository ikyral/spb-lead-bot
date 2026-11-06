"""
2GIS through its public web pages in a browser, without an API key.

Polite by design: one page at a time, a random pause of several seconds between pages, a limit
of pages and results per run, robots.txt, no more than one run a day. At the first sign of a
refusal (captcha, 401/403/429, a page that came empty) it stops; there is no code that tries to
get past any of these. Reacting to a block over time is the job of the collector. A page that
loaded normally but is not understood is not a refusal: reading stops, and nothing counts against
the site or uses up the rest between runs. The same goes for no connection at all.

The assisted mode of the collector on a computer with a screen changes one thing: at a captcha it
waits for the owner, who passes it by hand in the visible window, and goes on if they do. The code
never touches the captcha. All the limits stay, and a captcha nobody passed is an ordinary block.
"""

import asyncio
import logging
import random
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

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
from src.services.scraper import twogis_site
from src.services.scraper.browser import PageSnapshot, PlaywrightSession
from src.services.scraper.robots import RobotsNoConnectionError, RobotsRules, RobotsUnavailableError, fetch_robots
from src.services.twogis_links import build_card_url

logger = logging.getLogger(__name__)

# The token for robots.txt: the rules for '*' apply unless the site names this bot.
ROBOTS_AGENT: str = "spb-lead-bot"

# In a row: the format of the site has changed, and all the other cards would load it for nothing.
MAX_UNRECOGNIZED_IN_A_ROW: int = 3
_SERVER_ERROR: int = 500

# How often the window is checked while a person is passing the captcha.
CAPTCHA_POLL_SECONDS: float = 1.5
# With assisted runs a person sits and waits, so a run stays small whatever the general limits are:
# enough for a batch of 30 leads with a margin, plus a card of each organization and the search pages.
ASSISTED_MAX_RESULTS_PER_RUN: int = 60
ASSISTED_MAX_PAGES_PER_RUN: int = 80

Sleep = Callable[[float], Awaitable[None]]


def call_owner(message: str) -> None:
    """Tell the person at the computer: a line in the console and a sound."""
    # flush: run_collector.ps1 reads the output through a pipe, where it would wait in a buffer.
    print(message, flush=True)
    if sys.platform == "win32":
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    else:
        print("\a", end="", file=sys.stderr, flush=True)


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
    headless: bool = True
    assisted: bool = False
    profile_dir: Path | None = None
    assisted_wait_minutes: float = 5.0
    assisted_max_captchas: int = 2
    # Only the command line of the collector sets this, for one run: see --ignore-interval.
    ignore_interval: bool = False

    @classmethod
    def from_settings(cls, settings: Settings, assisted: bool = False) -> "ScraperOptions":
        """
        `assisted` is decided by the caller, not by SCRAPER_ASSISTED: only the collector on a
        computer with a screen asks for it, so on the server this setting changes nothing.
        """
        options = cls(
            city_slug=settings.SCRAPER_CITY_SLUG,
            min_delay_seconds=settings.SCRAPER_MIN_DELAY_SECONDS,
            max_delay_seconds=settings.SCRAPER_MAX_DELAY_SECONDS,
            max_pages_per_run=settings.SCRAPER_MAX_PAGES_PER_RUN,
            max_results_per_run=settings.SCRAPER_MAX_RESULTS_PER_RUN,
            min_hours_between_runs=settings.SCRAPER_MIN_HOURS_BETWEEN_RUNS,
            page_timeout_seconds=settings.SCRAPER_PAGE_TIMEOUT_SECONDS,
            headless=settings.SCRAPER_HEADLESS,
        )
        if not assisted:
            return options
        return replace(
            options,
            headless=False,
            assisted=True,
            profile_dir=resolve_path(settings.SCRAPER_PROFILE_DIR),
            assisted_wait_minutes=settings.SCRAPER_ASSISTED_WAIT_MINUTES,
            assisted_max_captchas=settings.SCRAPER_ASSISTED_MAX_CAPTCHAS,
            max_pages_per_run=min(options.max_pages_per_run, ASSISTED_MAX_PAGES_PER_RUN),
            max_results_per_run=min(options.max_results_per_run, ASSISTED_MAX_RESULTS_PER_RUN),
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
    if website is None and (listed_as_site or card.get(twogis_site.FROM_LINKS_KEY)):
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
        notify: Callable[[str], None] = call_owner,
    ) -> None:
        self._options = options or ScraperOptions()
        self.min_hours_between_runs = self._options.min_hours_between_runs
        self.ignore_interval = self._options.ignore_interval
        self._session_factory = session_factory or (
            lambda: PlaywrightSession(
                page_timeout_seconds=self._options.page_timeout_seconds,
                headless=self._options.headless,
                profile_dir=self._options.profile_dir,
            )
        )
        self._robots_loader = robots_loader or (lambda: fetch_robots(twogis_site.BASE_URL, ROBOTS_AGENT))
        self._sleep = sleep
        self._clock = clock
        self._notify = notify
        self._captchas = 0
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
        snapshot = await self._open(self._search_url(query, area, page), twogis_site.is_search_response)

        data = twogis_site.read_search_page(snapshot)
        if data is None:
            self._refuse_if_not_loaded(snapshot)
            raise ProviderFormatError(FORMAT_NOT_RECOGNIZED_TEXT)
        self.run_counted = True

        items = await self._with_contacts(data.items)
        places = parse_catalog_items(
            {"result": {"items": items.complete}},
            area.district,
            query.category,
            query.high_margin,
            city_slug=self._options.city_slug,
        )
        self._results += len(places)
        if items.blocked is not None:
            raise ProviderBlockedError(items.blocked, partial=places)
        if items.stopped is not None:
            items.stopped.partial = places
            raise items.stopped

        shown = page * len(data.items)
        has_more = bool(data.items) and not items.budget_ended and (data.total is None or shown < data.total)
        return SearchPage(places=places, has_more=has_more)

    async def read_card(self, card_url: str, firm_id: str) -> dict[str, Any] | None:
        """One organization card, by the rules of any other page. None if it was not recognized."""
        snapshot = await self._open(card_url, twogis_site.is_card_response)
        return self._recognized_card(snapshot, firm_id)

    async def inspect_search(self, query: SearchQuery, area: SearchArea) -> PageSnapshot:
        """A search page with everything 2GIS sent to it, for the diagnostics."""
        return await self._open(
            self._search_url(query, area), twogis_site.is_site_response, twogis_site.is_search_response
        )

    async def inspect_card(self, firm_id: str) -> PageSnapshot:
        """A card with everything 2GIS sent to it, for the diagnostics."""
        card_url = build_card_url(self._options.city_slug, firm_id)
        if card_url is None:
            raise ProviderUnavailableError("не удалось составить ссылку на карточку")
        return await self._open(card_url, twogis_site.is_site_response, twogis_site.is_card_response)

    async def close(self) -> None:
        """End the run: the browser is closed and the limits start over next time."""
        session, self._session = self._session, None
        self._robots = None
        self._pages = self._results = self._captchas = self._unrecognized_in_a_row = 0
        self.human_check_passed = False
        self.run_counted = False
        if session is not None:
            await session.close()

    def _search_url(self, query: SearchQuery, area: SearchArea, page: int = 1) -> str:
        text = f"{query.text} {area.district}" if area.district else query.text
        return twogis_site.build_search_url(self._options.city_slug, text, page)

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
        try:
            snapshot = await self._session.visit(url, wants_body)
            if self._options.assisted and twogis_site.is_captcha(snapshot.final_url, snapshot.title, snapshot.html):
                self.run_counted = True
                snapshot = await self._after_human_check(self._session, url, wants_body)
            bodies = snapshot.json_bodies if refusals_from is None else snapshot.bodies_from(refusals_from)
            reason = twogis_site.detect_block(
                snapshot.status, snapshot.final_url, snapshot.title, snapshot.html
            ) or twogis_site.refusal_in_bodies(bodies)
            if reason:
                raise ProviderBlockedError(reason)
        except ProviderNoConnectionError:
            raise
        except ProviderError:
            # The site did answer: this was a visit, whatever the answer means.
            self.run_counted = True
            raise
        return snapshot

    def _recognized_card(self, snapshot: PageSnapshot, firm_id: str) -> dict[str, Any] | None:
        """The card if the code understands what it lists. Without it, the page itself may be what is wrong."""
        card = twogis_site.read_card_page(snapshot, firm_id)
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

    async def _after_human_check(
        self, session: BrowserSession, url: str, wants_body: Callable[[str], bool]
    ) -> PageSnapshot:
        """Ask the owner to pass the captcha, then open the page again as any other page: pause, limit, checks."""
        if self._captchas >= self._options.assisted_max_captchas:
            raise ProviderBlockedError(
                f"сайт снова показал капчу, а запросов к человеку за запуск уже {self._captchas}"
            )
        self._captchas += 1
        await self._wait_for_human(session)
        self.human_check_passed = True

        if self._pages >= self._options.max_pages_per_run:
            raise ProviderBudgetExhausted("лимит страниц за запуск")
        await self._pause_between_pages()
        self._pages += 1
        snapshot = await session.visit(url, wants_body)
        if twogis_site.is_captcha(snapshot.final_url, snapshot.title, snapshot.html):
            raise ProviderBlockedError("капча появилась снова сразу после прохождения")
        return snapshot

    async def _wait_for_human(self, session: BrowserSession) -> None:
        """Only look at the window until it stops showing the captcha. Everything on the page is up to the person."""
        minutes = self._options.assisted_wait_minutes
        await session.bring_to_front()
        self._notify(f"Сайт показал капчу. Пройдите проверку в окне браузера, жду до {minutes:g} мин.")
        deadline = self._clock() + minutes * 60
        while self._clock() < deadline:
            await self._sleep(CAPTCHA_POLL_SECONDS)
            try:
                shown = await session.read_page()
            except ProviderUnavailableError:
                raise ProviderBlockedError("окно браузера закрыто, капча не пройдена") from None
            # An empty window during a navigation is not yet a passed check.
            if (
                shown is not None
                and shown.final_url.startswith("http")
                and not twogis_site.is_captcha(shown.final_url, shown.title, shown.html)
            ):
                self._notify("Проверка пройдена, продолжаю сбор.")
                return
        raise ProviderBlockedError(f"капча не пройдена за {minutes:g} мин")

    async def _robots_rules(self) -> RobotsRules:
        if self._robots is None:
            try:
                self._robots = await self._robots_loader()
            except RobotsNoConnectionError:
                raise ProviderNoConnectionError(NO_CONNECTION_TEXT) from None
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
            card_url = build_card_url(self._options.city_slug, firm_id)
            if card_url is None or result.budget_ended or result.blocked or result.stopped:
                continue
            try:
                snapshot = await self._open(card_url, twogis_site.is_card_response)
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
