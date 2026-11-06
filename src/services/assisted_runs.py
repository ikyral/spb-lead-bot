"""
Two small jobs of the page reader for a person at a computer: a diagnostic of one page and reading cards of leads.

Both only look at pages through the provider, so pauses, robots.txt, waiting for a person at a
captcha and the state of blocks are the ones of ordinary collection. Neither writes leads anywhere
on this computer: the database gets only the counters of limits and blocks.
"""

import asyncio
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.repo import (
    claim_scraper_check,
    get_provider_states,
    record_provider_block,
    record_provider_run,
    record_provider_success,
    release_scraper_check,
    restore_provider_run,
)
from src.services.api_client import (
    FORMAT_NOT_RECOGNIZED_TEXT,
    ProviderBlockedError,
    ProviderBudgetExhausted,
    ProviderError,
    ProviderNoConnectionError,
    ProviderTemporaryError,
)
from src.services.collector import REST_REASON, SearchTarget, interval_ends_at, unavailable_reason
from src.services.lead_export import write_enrich_result
from src.services.lead_file import (
    EnrichRequest,
    EnrichRequestItem,
    enrich_result_record,
    fit_result_lists,
    is_valid_result_record,
)
from src.services.providers.base import RawPlace
from src.services.providers.twogis import parse_catalog_items
from src.services.providers.twogis_scraper import (
    MAX_UNRECOGNIZED_IN_A_ROW,
    CardContacts,
    TwoGisScraperProvider,
    read_card_contacts,
)
from src.services.scraper import twogis_site
from src.services.scraper.browser import PageSnapshot
from src.services.scraper.diagnostics import inline_state_names, json_shape, masked_url, state_outline

logger = logging.getLogger(__name__)

# The diagnostics skip the rest between runs, so they are limited by themselves: SCRAPER_CHECK_MAX_PER_DAY.
CHECK_MAX_RUNS: int = 3
CHECK_WINDOW: timedelta = timedelta(hours=24)
CHECK_MAX_PAGES: int = 2
ENRICH_MAX_CARDS: int = 30
ENRICH_MAX_UNRECOGNIZED: int = MAX_UNRECOGNIZED_IN_A_ROW
# In a row: the browser or the connection does not work, for example the window was closed.
ENRICH_MAX_FAILURES: int = 3


# ---------- the diagnostics ----------


@dataclass
class CheckReport:
    """What one run of the diagnostics found out."""

    # Nothing was opened, and why. `retry_at` is set if the limit of runs was the reason.
    refused: str | None = None
    retry_at: datetime | None = None
    runs_left: int = 0
    blocked: str | None = None
    # Something other than the site stopped the run: robots.txt, the browser.
    failed: str | None = None
    search_found: int | None = None
    search_problem: str | None = None
    card_problem: str | None = None
    recognized: dict[str, Any] = field(default_factory=dict)
    pages: list[dict[str, Any]] = field(default_factory=list)
    path: Path | None = None
    sample_files: list[Path] = field(default_factory=list)


def _describe_page(kind: str, snapshot: PageSnapshot) -> dict[str, Any]:
    """Only the structure: this goes to a file that will be shown to someone."""
    return {
        "kind": kind,
        "status": snapshot.status,
        "final_url": masked_url(snapshot.final_url),
        "html_length": len(snapshot.html),
        "inline_state_variables": inline_state_names(snapshot.html),
        "page_state": {name: state_outline(value) for name, value in snapshot.page_state.items()},
        "page_state_problems": snapshot.page_state_problems,
        "responses": [
            {
                "url": masked_url(url),
                "expected_by_parser": (
                    twogis_site.is_search_response(url) if kind == "search" else twogis_site.is_card_response(url)
                ),
                "structure": json_shape(body),
            }
            for url, body in zip(snapshot.json_urls, snapshot.json_bodies, strict=True)
        ],
    }


def _write_sample(sample_dir: Path, stamp: str, kind: str, snapshot: PageSnapshot) -> list[Path]:
    """The page as it is, data of organizations included: this stays on this computer, see .gitignore."""
    sample_dir.mkdir(parents=True, exist_ok=True)
    html_path = sample_dir / f"{stamp}_{kind}.html"
    state_path = sample_dir / f"{stamp}_{kind}_state.json"
    html_path.write_text(snapshot.html, encoding="utf-8", newline="\n")
    document = {"values": snapshot.page_state, "problems": snapshot.page_state_problems}
    state_path.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    return [html_path, state_path]


def _recognized_fields(place: RawPlace, contacts: CardContacts | None) -> dict[str, Any]:
    return {
        "name": place.name,
        "address": place.address,
        "phones": contacts.phones if contacts else [],
        "website": contacts.website if contacts else None,
        "socials": contacts.socials if contacts else [],
        "email": contacts.email if contacts else None,
        "source_url": place.source_url,
        "contacts_recognized": contacts is not None,
    }


def _recognized_item(item: dict[str, Any], city_slug: str) -> dict[str, Any] | None:
    places = parse_catalog_items({"result": {"items": [item]}}, None, "check", False, city_slug=city_slug)
    return _recognized_fields(places[0], read_card_contacts(item)) if places else None


def _write_check_file(path: Path, report: CheckReport, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "checked_at": now.isoformat(),
        "blocked": report.blocked,
        "failed": report.failed,
        "search_found": report.search_found,
        "search_problem": report.search_problem,
        "card_problem": report.card_problem,
        "recognized": report.recognized,
        "pages": report.pages,
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    report.path = path


async def _inspect(
    provider: TwoGisScraperProvider, target: SearchTarget, report: CheckReport, city_slug: str, sample_dir: Path | None
) -> None:
    """One search page and one card of what it shows. Anything that went wrong stays in the report."""

    async def keep(kind: str, page: PageSnapshot) -> None:
        report.pages.append(_describe_page(kind, page))
        if sample_dir is not None:
            stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
            report.sample_files += await asyncio.to_thread(_write_sample, sample_dir, stamp, kind, page)

    snapshot = await provider.inspect_search(target.query, target.area)
    await keep("search", snapshot)

    data = twogis_site.read_search_page(snapshot)
    firm_number: str | None = None
    if data is None:
        report.search_problem = "нет ответа в ожидаемом формате: см. структуру ответов в файле"
    else:
        report.search_found = len(data.items)
        branches = [item for item in data.items if item.get("type") in (None, "branch") and item.get("id")]
        if branches:
            firm_number = str(branches[0]["id"]).split("_", 1)[0]
            report.recognized = _recognized_item(branches[0], city_slug) or {}
        else:
            report.search_problem = "в ответе нет организаций"
    # The card is worth seeing even if the list was not understood: that is what the diagnostics are for.
    firm_number = firm_number or twogis_site.first_firm_number(snapshot.html)
    if firm_number is None:
        report.card_problem = "на странице поиска нет ни одной организации, открывать нечего"
        return

    snapshot = await provider.inspect_card(firm_number)
    await keep("card", snapshot)
    card = twogis_site.read_card_page(snapshot, firm_number)
    if card is None:
        report.card_problem = "нет ответа в ожидаемом формате: см. структуру ответов в файле"
        return
    report.recognized = _recognized_item(card, city_slug) or report.recognized
    if read_card_contacts(card) is None:
        report.card_problem = "карточка найдена, но контакты в ней не распознаны"


async def run_check(
    session_maker: async_sessionmaker[AsyncSession],
    provider: TwoGisScraperProvider,
    target: SearchTarget,
    log_path: Path,
    backoff_hours: float,
    city_slug: str,
    max_runs: int = CHECK_MAX_RUNS,
    sample_dir: Path | None = None,
) -> CheckReport:
    """
    Look at one live search page and one card, and leave a description of them in a file.

    `provider` must be limited to CHECK_MAX_PAGES. The rest between runs does not apply: the runs have
    a limit of their own and this is not a collection, so the time of the last run stays untouched.
    A pause or a switch-off after blocks does apply, and a block here counts as any other.
    """
    now = datetime.now(UTC)
    report = CheckReport()
    async with session_maker() as session:
        state = (await get_provider_states(session)).get(provider.name)
        report.refused = unavailable_reason(provider, state, now)
        if report.refused is not None:
            return report
        report.retry_at, report.runs_left = await claim_scraper_check(session, max_runs, CHECK_WINDOW, now)
    if report.retry_at is not None:
        report.refused = f"не больше {max_runs} проверок за 24 часа"
        return report

    try:
        await _inspect(provider, target, report, city_slug, sample_dir)
    except ProviderBlockedError as exc:
        report.blocked = str(exc)
        async with session_maker() as session:
            await record_provider_block(session, provider.name, str(exc), backoff_hours)
    except ProviderNoConnectionError as exc:
        report.failed = str(exc)
        if not report.pages:
            async with session_maker() as session:
                await release_scraper_check(session, now)
            report.runs_left += 1
    except ProviderBudgetExhausted:
        report.card_problem = (
            f"лимит проверки {CHECK_MAX_PAGES} страницы исчерпан: страница после капчи открывалась заново. "
            "Запустите проверку ещё раз: cookies сохранены, капчи может не быть"
        )
    except ProviderTemporaryError as exc:
        report.card_problem = f"страница не прочитана: {exc}"
    except ProviderError as exc:
        report.failed = str(exc)
    finally:
        # Before closing the browser: closing may take a while, and a person may interrupt that.
        _write_check_file(log_path, report, now)
        await provider.close()
    return report


# ---------- reading cards ----------


@dataclass
class EnrichRun:
    """What one run of reading cards did. Filled as it goes: an interrupted run must still be told about."""

    refused: str | None = None
    # When the interval between runs ends, if that was the reason of the refusal.
    retry_at: datetime | None = None
    blocked: str | None = None
    # Why reading stopped before the end, if not because of a block.
    stopped: str | None = None
    failed: str | None = None
    rejected: Counter[str] = field(default_factory=Counter)
    records: list[dict[str, Any]] = field(default_factory=list)
    planned: int = 0
    over_limit: int = 0
    path: Path | None = None

    @property
    def read(self) -> int:
        return sum(1 for record in self.records if record["parsed"])

    @property
    def not_opened(self) -> int:
        return self.planned - len(self.records)


def _without_query(url: str) -> str:
    """A link to a messenger may carry a ready text in its query: long, and not a part of the address."""
    return url.split("?", 1)[0].split("#", 1)[0]


def _result_record(item: EnrichRequestItem, card: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """
    A card that was not understood is 'parsed: false', and never a card without a site.

    The same if the bot would refuse what was read: for example a site in another scheme is not 'no site'.
    """
    empty = enrich_result_record(item.source, item.source_id, now)
    contacts = read_card_contacts(card) if card is not None else None
    if contacts is None:
        return empty
    phones, socials = fit_result_lists(contacts.phones, [_without_query(url) for url in contacts.socials])
    record = enrich_result_record(
        item.source,
        item.source_id,
        now,
        parsed=True,
        phones=phones,
        website=contacts.website,
        socials=socials,
        email=contacts.email,
    )
    return record if is_valid_result_record(record) else empty


async def _read_cards(provider: TwoGisScraperProvider, items: list[EnrichRequestItem], run: EnrichRun) -> None:
    unrecognized = failures = 0
    for item in items:
        try:
            card = await provider.read_card(item.source_url, item.firm_number)
        except ProviderTemporaryError as exc:
            run.records.append(enrich_result_record(item.source, item.source_id, datetime.now(UTC)))
            failures += 1
            if failures >= ENRICH_MAX_FAILURES:
                run.stopped = f"{failures} страницы подряд не открылись: {exc}"
                return
            continue
        failures = 0
        run.records.append(_result_record(item, card, datetime.now(UTC)))
        unrecognized = 0 if run.records[-1]["parsed"] else unrecognized + 1
        if unrecognized >= ENRICH_MAX_UNRECOGNIZED:
            run.failed = FORMAT_NOT_RECOGNIZED_TEXT
            return


async def run_enrich(
    session_maker: async_sessionmaker[AsyncSession],
    provider: TwoGisScraperProvider,
    request: EnrichRequest,
    export_dir: Path,
    backoff_hours: float,
    run: EnrichRun,
) -> None:
    """
    Read the cards of a request and write what was read to a file, whatever stops the run.

    The rules are those of a collection: it waits out the rest between runs and a pause after
    blocks, counts as a run itself, and only a run with no captcha resets the counter of blocks.
    """
    run.rejected.update(request.rejected)
    items = request.items[:ENRICH_MAX_CARDS]
    run.planned, run.over_limit = len(items), len(request.items) - len(items)

    async with session_maker() as session:
        state = (await get_provider_states(session)).get(provider.name)
        run.refused = unavailable_reason(provider, state, datetime.now(UTC))
        if run.refused == REST_REASON:
            run.retry_at = interval_ends_at(provider, state)
        if run.refused is not None or not items:
            return
        # Taken in advance and given back only by a run that ended normally: a killed run stays counted.
        previous_run = state.last_run_at if state is not None else None
        await record_provider_run(session, provider.name)

    human_passed = False
    counted = True
    try:
        try:
            await _read_cards(provider, items, run)
        finally:
            human_passed = provider.human_check_passed
            counted = provider.run_counted
            if run.records:
                # Without waiting for anything: this also runs while the run is being cancelled.
                run.path = write_enrich_result(export_dir, run.records)
    except ProviderBlockedError as exc:
        run.blocked = str(exc)
        async with session_maker() as session:
            await record_provider_block(session, provider.name, str(exc), backoff_hours)
    except ProviderBudgetExhausted as exc:
        run.stopped = str(exc)
    except ProviderError as exc:
        run.failed = str(exc)
    finally:
        await provider.close()

    if not counted:
        async with session_maker() as session:
            await restore_provider_run(session, provider.name, previous_run)
    if run.read and not (run.blocked or run.failed or run.stopped or human_passed or provider.ignore_interval):
        async with session_maker() as session:
            await record_provider_success(session, provider.name)
