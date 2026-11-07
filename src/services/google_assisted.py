"""
Looking for the site and contacts of leads on Google, with a person at the computer.

Only the collector on the owner's computer runs this, in a visible window with a profile of its own,
one search at a time, with long random pauses and small limits per run and per day. Nothing here
hides automation or touches a captcha. At the first sign of a refusal the run stops and keeps what
it found: a captcha waits for the owner only so that the window is left in order, and still counts
as a block. Three blocks in a row switch Google off until `--enable-provider google`.

On a site found this way one page is read, the main one, if its robots.txt allows: one request
per organization, with the same protection from internal addresses as the check of sites.
"""

import asyncio
import logging
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings, resolve_path
from src.database.repo import (
    get_provider_states,
    record_provider_block,
    record_provider_run,
    record_provider_success,
    restore_provider_run,
    spend_daily_request,
)
from src.services.api_client import (
    FORMAT_NOT_RECOGNIZED_TEXT,
    ProviderError,
    ProviderNoConnectionError,
    ProviderTemporaryError,
    ProviderUnavailableError,
)
from src.services.checker import create_check_client
from src.services.collector import REST_REASON, interval_ends_at, unavailable_reason
from src.services.lead_export import write_enrich_result
from src.services.lead_file import (
    ENRICH_GOOGLE_SCHEMA_VERSION,
    FOUND_BY_GOOGLE,
    EnrichRequest,
    EnrichRequestItem,
    enrich_result_record,
    fit_result_lists,
    is_valid_result_record,
)
from src.services.providers.base import niche_title
from src.services.providers.twogis_scraper import ROBOTS_AGENT, BrowserSession, call_owner
from src.services.scraper import google_site
from src.services.scraper.browser import PageSnapshot, PlaywrightSession
from src.services.scraper.diagnostics import masked_url
from src.services.scraper.robots import RobotsUnavailableError, fetch_robots

logger = logging.getLogger(__name__)

GOOGLE_PROVIDER_NAME: str = "google"
# In the code, not in the settings, so that they cannot be made shorter.
MIN_PAUSE_SECONDS: float = 10.0
MAX_PAUSE_SECONDS: float = 25.0
OWNER_POLL_SECONDS: float = 1.5
SITE_TIMEOUT_SECONDS: float = 20.0
SITE_MAX_BYTES: int = 2 * 1024 * 1024

REASON_NO_NAME = "нет названия: искать нечего"
NO_CONNECTION_TEXT = "Нет связи с Google: проверьте сеть и VPN, это не засчитано как запуск"
CAPTCHA_TEXT = "Google показал капчу или страницу о необычном трафике"
CONSENT_TEXT = "Google попросил согласия на cookies"

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class GoogleOptions:
    """Limits of one run."""

    max_per_run: int = 15
    max_per_day: int = 60
    min_hours_between_runs: float = 3.0
    name_match_threshold: float = 0.6
    profile_dir: Path | None = None
    wait_minutes: float = 5.0
    page_timeout_seconds: float = 45.0
    backoff_hours: float = 6.0
    # Only the command line sets this, for one run: see --ignore-interval.
    ignore_interval: bool = False

    @classmethod
    def from_settings(cls, settings: Settings, ignore_interval: bool = False) -> "GoogleOptions":
        return cls(
            max_per_run=settings.GOOGLE_MAX_PER_RUN,
            max_per_day=settings.GOOGLE_MAX_PER_DAY,
            min_hours_between_runs=settings.GOOGLE_MIN_HOURS_BETWEEN_RUNS,
            name_match_threshold=settings.GOOGLE_NAME_MATCH_THRESHOLD,
            profile_dir=resolve_path(settings.GOOGLE_PROFILE_DIR),
            wait_minutes=settings.SCRAPER_ASSISTED_WAIT_MINUTES,
            page_timeout_seconds=settings.SCRAPER_PAGE_TIMEOUT_SECONDS,
            backoff_hours=settings.SCRAPER_BLOCK_BACKOFF_HOURS,
            ignore_interval=ignore_interval,
        )


@dataclass(frozen=True)
class SiteVisit:
    """What the main page of a site gave. Without contacts `problem` says why it was not read."""

    contacts: google_site.SiteContacts | None
    problem: str | None = None


SiteFetcher = Callable[[str], Awaitable[SiteVisit]]


@dataclass
class GoogleRun:
    """What one run did. Filled as it goes: an interrupted run must still be told about."""

    refused: str | None = None
    retry_at: datetime | None = None
    blocked: str | None = None
    # Why the run stopped before the end, if not because of a block or a failure.
    stopped: str | None = None
    failed: str | None = None
    rejected: Counter[str] = field(default_factory=Counter)
    records: list[dict[str, Any]] = field(default_factory=list)
    planned: int = 0
    over_limit: int = 0
    # Pages of Google that came back, whatever they showed.
    searched: int = 0
    owner_passed: bool = False
    day_left: int | None = None
    path: Path | None = None
    # For logs/collector_errors.log: what went wrong, without pages or contacts.
    problems: list[str] = field(default_factory=list)

    def count(self, wanted: Callable[[dict[str, Any]], bool]) -> int:
        return sum(1 for record in self.records if record["parsed"] and wanted(record))


async def _no_wait(_: float) -> None:
    return None


async def _read_html(client: httpx.AsyncClient, url: str) -> str | None:
    async with client.stream("GET", url) as response:
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code >= 400 or "html" not in content_type:
            return None
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) >= SITE_MAX_BYTES:
                break
        return bytes(body[:SITE_MAX_BYTES]).decode(response.encoding or "utf-8", errors="replace")


async def fetch_site_contacts(url: str) -> SiteVisit:
    """One ordinary request of the main page, if robots.txt allows. Whether the site works the bot checks itself."""
    async with create_check_client() as client:
        try:
            # Without pauses between attempts: a site that does not answer is the bot's business, not a wait here.
            rules = await fetch_robots(url, ROBOTS_AGENT, client, sleep=_no_wait)
        except RobotsUnavailableError:
            return SiteVisit(None, "robots.txt сайта не прочитан")
        if not rules.is_allowed(url):
            return SiteVisit(None, "robots.txt сайта запрещает открывать главную страницу")
        try:
            html = await asyncio.wait_for(_read_html(client, url), SITE_TIMEOUT_SECONDS)
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError):
            return SiteVisit(None, "сайт не ответил")
    if html is None:
        return SiteVisit(None, "сайт ответил не страницей")
    return SiteVisit(google_site.extract_site_contacts(html))


def _result_record(
    item: EnrichRequestItem, choice: google_site.Choice, contacts: google_site.SiteContacts | None, now: datetime
) -> dict[str, Any]:
    """A searched lead: what was found, or that nothing was. Never a record the bot would refuse."""
    socials = list(dict.fromkeys([*choice.socials, *(contacts.socials if contacts else [])]))
    phones, socials = fit_result_lists(contacts.phones if contacts else [], socials)
    record = enrich_result_record(
        item.source,
        item.source_id,
        now,
        parsed=True,
        phones=phones,
        website=choice.website,
        socials=socials,
        email=contacts.email if contacts else None,
        found_by=FOUND_BY_GOOGLE,
    )
    if is_valid_result_record(record):
        return record
    return enrich_result_record(item.source, item.source_id, now, found_by=FOUND_BY_GOOGLE)


class _Rules:
    """The rest between runs of Google, for the same checks a provider gets."""

    def __init__(self, options: GoogleOptions) -> None:
        self.min_hours_between_runs = options.min_hours_between_runs
        self.ignore_interval = options.ignore_interval


class _Search:
    """One pass over the leads of a run, stopping at the first sign of trouble."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        browser: BrowserSession,
        options: GoogleOptions,
        run: GoogleRun,
        fetch_site: SiteFetcher,
        sleep: Sleep,
        clock: Callable[[], float],
        notify: Callable[[str], None],
        today: Callable[[], date],
    ) -> None:
        self._session_maker = session_maker
        self._browser = browser
        self._options = options
        self._run = run
        self._fetch_site = fetch_site
        self._sleep = sleep
        self._clock = clock
        self._notify = notify
        self._today = today

    async def all(self, items: list[EnrichRequestItem]) -> None:
        await self._browser.start()
        for item in items:
            if not await self._spend_request():
                return
            if self._run.searched:
                # Not cryptography: only spreads requests out in time.
                await self._sleep(random.uniform(MIN_PAUSE_SECONDS, MAX_PAUSE_SECONDS))  # noqa: S311
            page = await self._search(item)
            if page is None:
                return
            choice = google_site.choose(
                page.results,
                item.name or "",
                self._options.name_match_threshold,
                niche_title(item.category) if item.category else None,
            )
            contacts: google_site.SiteContacts | None = None
            if choice.website is not None:
                visit = await self._fetch_site(choice.website)
                contacts = visit.contacts
                if visit.problem:
                    logger.info("The main page of a site found on Google was not read: %s", visit.problem)
            self._run.records.append(_result_record(item, choice, contacts, datetime.now(UTC)))

    async def _spend_request(self) -> bool:
        async with self._session_maker() as session:
            left = await spend_daily_request(
                session, GOOGLE_PROVIDER_NAME, self._today().isoformat(), self._options.max_per_day
            )
        if left is None:
            self._run.stopped = f"за сутки уже отправлено {self._options.max_per_day} запросов в Google"
            return False
        self._run.day_left = left
        return True

    async def _search(self, item: EnrichRequestItem) -> google_site.ResultsPage | None:
        """The results of one search, or None if the run has to stop: then the reason is in the run."""
        url = google_site.build_search_url(google_site.search_text(item.name or "", item.address, item.district))
        try:
            snapshot = await self._browser.visit(url, lambda _: False)
        except ProviderNoConnectionError:
            self._run.failed = NO_CONNECTION_TEXT
            return None
        except ProviderTemporaryError as exc:
            self._run.stopped = f"страница поиска не открылась: {exc}"
            return None
        self._run.searched += 1

        page = google_site.read_results_page(snapshot.status, snapshot.final_url, snapshot.title, snapshot.html)
        if page.kind in (google_site.PAGE_RESULTS, google_site.PAGE_NOTHING_FOUND):
            return page
        where = f"{masked_url(snapshot.final_url)}, код {snapshot.status}"
        if page.kind == google_site.PAGE_CAPTCHA:
            self._run.blocked = CAPTCHA_TEXT
            self._problem(f"{CAPTCHA_TEXT} ({where})")
            self._run.owner_passed = await self._wait_for_owner(
                page.kind,
                f"{CAPTCHA_TEXT}. Поиск остановлен, найденное сохраняется. Пройдите проверку в окне браузера, "
                f"жду до {self._options.wait_minutes:g} мин. Это всё равно засчитано как блокировка.",
            )
        elif page.kind == google_site.PAGE_CONSENT:
            self._run.stopped = f"{CONSENT_TEXT}: примите или отклоните их в окне и запустите поиск ещё раз"
            self._problem(f"{CONSENT_TEXT} ({where})")
            self._run.owner_passed = await self._wait_for_owner(
                page.kind,
                f"{CONSENT_TEXT}. Поиск остановлен, найденное сохраняется. Ответьте в окне браузера, "
                f"жду до {self._options.wait_minutes:g} мин. Блокировкой это не считается.",
            )
        elif page.kind == google_site.PAGE_REFUSED:
            self._run.blocked = f"Google ответил кодом {snapshot.status}"
            self._problem(f"{self._run.blocked} ({where})")
        elif page.kind == google_site.PAGE_SERVER_ERROR:
            self._run.stopped = f"Google ответил кодом {snapshot.status}"
            self._problem(f"{self._run.stopped} ({where})")
        else:
            self._run.failed = FORMAT_NOT_RECOGNIZED_TEXT
            self._problem(
                f"Страница Google не распознана ({where}, заголовок {len(snapshot.title)} симв., "
                f"HTML {len(snapshot.html)} симв.)"
            )
        return None

    def _problem(self, text: str) -> None:
        logger.warning("Google: %s", text)
        self._run.problems.append(text)

    async def _wait_for_owner(self, kind: str, message: str) -> bool:
        """Only look at the window until it shows something else. Everything on the page is up to the person."""
        await self._browser.bring_to_front()
        self._notify(message)
        deadline = self._clock() + self._options.wait_minutes * 60
        while self._clock() < deadline:
            await self._sleep(OWNER_POLL_SECONDS)
            try:
                shown = await self._browser.read_page()
            except ProviderUnavailableError:
                self._notify("Окно браузера закрыто.")
                return False
            if shown is not None and shown.final_url.startswith("http") and not self._still(kind, shown):
                self._notify("Готово, окно можно не трогать: запуск завершается.")
                return True
        self._notify(f"За {self._options.wait_minutes:g} мин окно не изменилось, запуск завершается.")
        return False

    @staticmethod
    def _still(kind: str, shown: PageSnapshot) -> bool:
        return google_site.read_results_page(None, shown.final_url, shown.title, shown.html).kind == kind


async def run_google_enrich(
    session_maker: async_sessionmaker[AsyncSession],
    request: EnrichRequest,
    options: GoogleOptions,
    export_dir: Path,
    run: GoogleRun,
    session_factory: Callable[[], BrowserSession] | None = None,
    fetch_site: SiteFetcher = fetch_site_contacts,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    notify: Callable[[str], None] = call_owner,
    today: Callable[[], date] = date.today,
) -> None:
    """
    Search for the leads of a request and write what was found to a file, whatever stops the run.

    The rules are those of reading cards: the rest between runs, a pause after blocks, and only a
    run with no trouble, no person at a captcha and no skipped interval resets the count of blocks.
    """
    run.rejected.update(request.rejected)
    named = [item for item in request.items if item.name]
    if len(named) < len(request.items):
        run.rejected[REASON_NO_NAME] += len(request.items) - len(named)
    items = named[: options.max_per_run]
    run.planned, run.over_limit = len(items), len(named) - len(items)

    rules = _Rules(options)
    async with session_maker() as session:
        state = (await get_provider_states(session)).get(GOOGLE_PROVIDER_NAME)
        run.refused = unavailable_reason(rules, state, datetime.now(UTC))
        if run.refused == REST_REASON:
            run.retry_at = interval_ends_at(rules, state)
        if run.refused is not None or not items:
            return
        # Taken in advance and given back only if Google was never reached: a killed run stays counted.
        previous_run = state.last_run_at if state is not None else None
        await record_provider_run(session, GOOGLE_PROVIDER_NAME)

    browser = (session_factory or _default_session(options))()
    search = _Search(session_maker, browser, options, run, fetch_site, sleep, clock, notify, today)
    try:
        try:
            await search.all(items)
        finally:
            if run.records:
                # Without waiting for anything: this also runs while the run is being cancelled.
                run.path = write_enrich_result(
                    export_dir, run.records, prefix="google_enrich", schema_version=ENRICH_GOOGLE_SCHEMA_VERSION
                )
    except ProviderError as exc:
        run.failed = str(exc)
        run.problems.append(str(exc))
    finally:
        await browser.close()

    async with session_maker() as session:
        if run.blocked:
            await record_provider_block(session, GOOGLE_PROVIDER_NAME, run.blocked, options.backoff_hours)
        elif not run.searched:
            await restore_provider_run(session, GOOGLE_PROVIDER_NAME, previous_run)
        elif not (run.failed or run.stopped or run.owner_passed or options.ignore_interval):
            await record_provider_success(session, GOOGLE_PROVIDER_NAME)


def _default_session(options: GoogleOptions) -> Callable[[], BrowserSession]:
    # Always visible and with its own profile: this is only ever run by a person at the computer.
    return lambda: PlaywrightSession(
        page_timeout_seconds=options.page_timeout_seconds, headless=False, profile_dir=options.profile_dir
    )
