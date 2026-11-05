"""Automatic lead collection: what to search for (from settings) and one collection pass."""

import logging
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings
from src.services.api_client import ProviderAuthError, ProviderError, ProviderQuotaError
from src.services.normalize import normalize_place
from src.services.pipeline import save_collected_places
from src.services.providers.base import (
    SPB_DISTRICTS,
    TARGET_CATEGORIES,
    BaseMapProvider,
    RawPlace,
    SearchArea,
    SearchQuery,
)
from src.services.providers.registry import KNOWN_PROVIDERS
from src.services.scorer import is_high_margin_category

logger = logging.getLogger(__name__)

# A provider failing this many times in a row is skipped for the rest of the run.
MAX_CONSECUTIVE_FAILURES: int = 3
# Cron numbers the weekdays from Sunday (0 or 7), APScheduler from Monday.
_CRON_WEEKDAYS: tuple[str, ...] = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_NUMBER_RE = re.compile(r"(?<![/\d])[0-7](?!\d)")


@dataclass(frozen=True)
class SearchTarget:
    """One unit of collection: a niche in an area."""

    query: SearchQuery
    area: SearchArea


@dataclass(frozen=True)
class CollectorConfig:
    """Validated collection settings."""

    trigger: BaseTrigger
    schedule_text: str
    timezone: ZoneInfo
    providers: list[str]
    targets: list[SearchTarget]
    max_pages: int
    page_size: int
    max_results_per_query: int
    max_requests_per_run: int
    max_requests_per_day: int


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_cron(expression: str, timezone: ZoneInfo) -> CronTrigger:
    """Parse a standard 5-field crontab, whose weekday numbers APScheduler would misread."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"COLLECT_CRON: нужно 5 полей (мин час день месяц день_недели), получено {expression!r}")
    fields[4] = _WEEKDAY_NUMBER_RE.sub(lambda match: _CRON_WEEKDAYS[int(match.group())], fields[4])
    try:
        return CronTrigger.from_crontab(" ".join(fields), timezone=timezone)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"COLLECT_CRON: неверное выражение {expression!r}: {exc}") from exc


def _parse_queries(value: str) -> list[SearchQuery]:
    queries: list[SearchQuery] = []
    for item in _split(value) or list(TARGET_CATEGORIES):
        category = TARGET_CATEGORIES.get(item)
        if category is None:
            queries.append(SearchQuery(text=item, category=item, high_margin=is_high_margin_category(item)))
        else:
            queries.append(
                SearchQuery(
                    text=category["query_2gis"],
                    category=item,
                    high_margin=bool(category.get("high_margin", False)),
                    osm_tags=tuple(category.get("osm_tags", ())),
                )
            )
    return queries


def _parse_areas(districts: str, bbox: str) -> list[SearchArea]:
    if bbox.strip():
        try:
            south, west, north, east = (float(part) for part in bbox.split(","))
        except ValueError as exc:
            raise ValueError(f"COLLECT_BBOX: нужно 'юг,запад,север,восток', получено {bbox!r}") from exc
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            raise ValueError(f"COLLECT_BBOX: неверные координаты {bbox!r}")
        return [SearchArea(bbox=(south, west, north, east))]

    names = _split(districts) or list(SPB_DISTRICTS)
    unknown = [name for name in names if name not in SPB_DISTRICTS]
    if unknown:
        raise ValueError(f"COLLECT_DISTRICTS: неизвестные районы {unknown}; доступны: {list(SPB_DISTRICTS)}")
    return [SearchArea(bbox=SPB_DISTRICTS[name], district=name) for name in names]


def load_collector_config(settings: Settings) -> CollectorConfig:
    """Validate collection settings. Raises ValueError with a message for the bot owner."""
    try:
        timezone = ZoneInfo(settings.COLLECT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"COLLECT_TIMEZONE: неизвестный часовой пояс {settings.COLLECT_TIMEZONE!r}") from exc

    trigger: BaseTrigger
    if settings.COLLECT_INTERVAL_MINUTES is not None:
        trigger = IntervalTrigger(minutes=settings.COLLECT_INTERVAL_MINUTES, timezone=timezone)
        schedule_text = f"каждые {settings.COLLECT_INTERVAL_MINUTES} мин"
    else:
        trigger = _parse_cron(settings.COLLECT_CRON, timezone)
        schedule_text = f"cron {settings.COLLECT_CRON}"

    providers = [name.lower() for name in _split(settings.COLLECT_PROVIDERS)]
    unknown = sorted(set(providers) - KNOWN_PROVIDERS)
    if unknown:
        raise ValueError(f"COLLECT_PROVIDERS: неизвестные провайдеры {unknown}; доступны: {sorted(KNOWN_PROVIDERS)}")

    areas = _parse_areas(settings.COLLECT_DISTRICTS, settings.COLLECT_BBOX)
    return CollectorConfig(
        trigger=trigger,
        schedule_text=f"{schedule_text} ({settings.COLLECT_TIMEZONE})",
        timezone=timezone,
        providers=providers,
        targets=[
            SearchTarget(query, area)
            for query in _parse_queries(settings.COLLECT_QUERIES)
            for area in areas
        ],
        max_pages=settings.COLLECT_MAX_PAGES,
        page_size=settings.COLLECT_PAGE_SIZE,
        max_results_per_query=settings.COLLECT_MAX_RESULTS_PER_QUERY,
        max_requests_per_run=settings.COLLECT_MAX_REQUESTS_PER_RUN,
        max_requests_per_day=settings.COLLECT_MAX_REQUESTS_PER_DAY,
    )


@dataclass
class CollectResult:
    """Outcome of one collection pass."""

    found: int = 0
    qualified: int = 0
    saved_new: int = 0
    duplicates: int = 0
    errors: int = 0
    requests: int = 0
    next_cursor: int = 0
    targets_processed: int = 0
    # Problems that need the owner's attention (key, quota, no access to contacts).
    alerts: list[str] = field(default_factory=list)
    last_error: str | None = None


class _Collection:
    """Mutable state of one pass over the search targets."""

    def __init__(self, providers: list[BaseMapProvider], config: CollectorConfig, max_requests: int) -> None:
        self.active = list(providers)
        self.config = config
        self.max_requests = max_requests
        self.result = CollectResult()
        self.places: list[RawPlace] = []
        self.seen_ids: set[str] = set()
        self.failures_in_a_row: dict[str, int] = {}

    def alert(self, text: str) -> None:
        if text not in self.result.alerts:
            self.result.alerts.append(text)

    def record_error(self, text: str) -> None:
        self.result.errors += 1
        self.result.last_error = text

    def drop_provider(self, provider: BaseMapProvider) -> None:
        if provider in self.active:
            self.active.remove(provider)

    async def collect_target(self, provider: BaseMapProvider, target: SearchTarget) -> None:
        """Walk the result pages of one target. Results received before a failure are kept."""
        collected = 0
        for page in range(1, self.config.max_pages + 1):
            if self.result.requests >= self.max_requests:
                return
            self.result.requests += 1
            try:
                search_page = await provider.search(target.query, target.area, page, self.config.page_size)
            except (ProviderAuthError, ProviderQuotaError) as exc:
                # Repeating will not help and could get the key suspended.
                logger.error("Provider %s stopped for this run: %s", provider.name, exc)
                self.record_error(str(exc))
                self.alert(str(exc))
                self.drop_provider(provider)
                return
            except ProviderError as exc:
                logger.warning("Provider %s failed on %s: %s", provider.name, target.query.category, exc)
                self.record_error(str(exc))
                self._count_failure(provider, str(exc))
                return
            except Exception:
                # A bug in one provider must not stop the others.
                logger.exception("Provider %s crashed on %s", provider.name, target.query.category)
                text = f"{provider.name}: внутренняя ошибка, подробности в логе"
                self.record_error(text)
                self._count_failure(provider, text)
                return

            self.failures_in_a_row[provider.name] = 0
            if search_page.warning:
                logger.warning("Provider %s: %s", provider.name, search_page.warning)
                self.alert(search_page.warning)

            for place in search_page.places:
                if collected >= self.config.max_results_per_query:
                    return
                collected += 1
                if place.external_id not in self.seen_ids:
                    self.seen_ids.add(place.external_id)
                    self.places.append(normalize_place(place))
            if not search_page.has_more:
                return

    def _count_failure(self, provider: BaseMapProvider, error: str) -> None:
        count = self.failures_in_a_row.get(provider.name, 0) + 1
        self.failures_in_a_row[provider.name] = count
        if count >= MAX_CONSECUTIVE_FAILURES:
            logger.warning("Provider %s skipped for this run after %d failures in a row", provider.name, count)
            # A source that stopped working (or rejects every request) must not fail silently.
            self.alert(f"Источник {provider.name} пропущен до следующего сбора после {count} ошибок подряд: {error}")
            self.drop_provider(provider)


async def collect_leads(
    session_maker: async_sessionmaker[AsyncSession],
    providers: list[BaseMapProvider],
    config: CollectorConfig,
    cursor: int = 0,
    max_requests: int | None = None,
) -> CollectResult:
    """
    Search the targets starting from `cursor` until the request budget runs out, then save new leads.

    Rotating the start keeps a limited budget fair: otherwise the last targets are never reached.
    Provider failures are recorded in the result; only a database failure raises.
    """
    budget = config.max_requests_per_run if max_requests is None else max_requests
    collection = _Collection(providers, config, budget)
    result = collection.result
    targets = config.targets
    start = cursor % len(targets) if targets else 0

    for offset in range(len(targets)):
        if result.requests >= budget or not collection.active:
            break
        target = targets[(start + offset) % len(targets)]
        for provider in list(collection.active):
            await collection.collect_target(provider, target)
        result.targets_processed = offset + 1
    result.next_cursor = (start + result.targets_processed) % len(targets) if targets else 0

    async with session_maker() as session:
        stats = await save_collected_places(session, collection.places)
    result.found = stats["total_found"]
    result.qualified = stats["qualified"]
    result.saved_new = stats["saved_new"]
    result.duplicates = stats["duplicates_skipped"]
    return result
