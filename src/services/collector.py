"""Automatic lead collection: what to search for (from settings) and one collection pass."""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings
from src.database.models import ProviderState
from src.database.repo import (
    MAX_CONSECUTIVE_BLOCKS,
    disable_provider,
    get_provider_states,
    record_provider_block,
    record_provider_run,
    record_provider_success,
)
from src.services.api_client import (
    ProviderAuthError,
    ProviderBlockedError,
    ProviderBudgetExhausted,
    ProviderError,
    ProviderQuotaError,
    ProviderUnavailableError,
)
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
    block_backoff_hours: float = 6.0


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

    if settings.SCRAPER_MIN_DELAY_SECONDS > settings.SCRAPER_MAX_DELAY_SECONDS:
        raise ValueError("SCRAPER_MIN_DELAY_SECONDS не может быть больше SCRAPER_MAX_DELAY_SECONDS")

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
        block_backoff_hours=settings.SCRAPER_BLOCK_BACKOFF_HOURS,
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
    # Providers not used in this run, and why.
    skipped: dict[str, str] = field(default_factory=dict)


REST_REASON: str = "уже запускался сегодня: сайт не должен посещаться часто"


def unavailable_reason(provider: BaseMapProvider, state: ProviderState | None, now: datetime) -> str | None:
    """Why a provider must not be used right now, or None if it may."""
    if state is None:
        return None
    if state.disabled_at is not None:
        cause = "после блокировок " if state.consecutive_blocks else ""
        return f"отключён {cause}({state.last_block_reason}), нужно ручное включение"
    if state.blocked_until is not None and now < state.blocked_until:
        return f"пауза после блокировки ({state.last_block_reason})"
    if provider.min_hours_between_runs and state.last_run_at is not None:
        allowed_at = state.last_run_at + timedelta(hours=provider.min_hours_between_runs)
        if now < allowed_at:
            return REST_REASON
    return None


class _Collection:
    """Mutable state of one pass over the search targets."""

    def __init__(
        self,
        providers: list[BaseMapProvider],
        config: CollectorConfig,
        max_requests: int,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        self.active = list(providers)
        self.config = config
        self.session_maker = session_maker
        self.succeeded: set[str] = set()
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
            except ProviderBudgetExhausted as exc:
                logger.info("Provider %s finished for this run: %s", provider.name, exc)
                self.drop_provider(provider)
                return
            except ProviderBlockedError as exc:
                self._add_places(exc.partial, collected)
                await self._handle_block(provider, exc)
                return
            except ProviderAuthError as exc:
                if provider.auth_failure_hint is None:
                    self._stop_provider(provider, exc)
                else:
                    await self._handle_rejected_key(provider, exc, provider.auth_failure_hint)
                return
            except (ProviderQuotaError, ProviderUnavailableError) as exc:
                self._stop_provider(provider, exc)
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
            self.succeeded.add(provider.name)
            if search_page.warning:
                logger.warning("Provider %s: %s", provider.name, search_page.warning)
                self.alert(search_page.warning)

            collected = self._add_places(search_page.places, collected)
            if collected >= self.config.max_results_per_query or not search_page.has_more:
                return

    def _stop_provider(self, provider: BaseMapProvider, error: ProviderError) -> None:
        """Repeating will not help and could get the key suspended."""
        logger.error("Provider %s stopped for this run: %s", provider.name, error)
        self.record_error(str(error))
        self.alert(str(error))
        self.drop_provider(provider)

    async def _handle_rejected_key(self, provider: BaseMapProvider, error: ProviderError, hint: str) -> None:
        """A rejected key will not start working by itself: no more requests with it, the leads stay available."""
        async with self.session_maker() as session:
            await disable_provider(session, provider.name, hint)
        self.drop_provider(provider)
        self.succeeded.discard(provider.name)
        text = (
            f"{provider.name}: {hint}. Источник отключён, накопленные лиды выдаются как обычно. Что делать: укажите "
            f"новый ключ и перезапустите бота, источник включится сам; для прежнего ключа: /provider_on {provider.name}"
        )
        logger.error("Provider switched off: %s (%s)", text, error)
        self.record_error(text)
        self.alert(text)

    def _add_places(self, places: list[RawPlace], collected: int) -> int:
        """Add places up to the limit per query, without repeats. Returns the new count for this query."""
        for place in places:
            if collected >= self.config.max_results_per_query:
                break
            collected += 1
            if place.external_id not in self.seen_ids:
                self.seen_ids.add(place.external_id)
                self.places.append(normalize_place(place))
        return collected

    async def _handle_block(self, provider: BaseMapProvider, error: ProviderBlockedError) -> None:
        """Stop right away, tell the admins, and do not come back for hours."""
        async with self.session_maker() as session:
            state = await record_provider_block(
                session, provider.name, str(error), self.config.block_backoff_hours
            )
        self.drop_provider(provider)
        self.succeeded.discard(provider.name)
        if state.disabled_at is not None:
            text = (
                f"{provider.name}: {error}. Это блокировка {state.consecutive_blocks} подряд, источник отключён "
                f"до ручного включения: /provider_on {provider.name}"
            )
        else:
            text = (
                f"{provider.name}: {error}. Сбор из этого источника остановлен (блокировка "
                f"{state.consecutive_blocks} из {MAX_CONSECUTIVE_BLOCKS} до отключения), повтор не раньше "
                f"{_format_time(state.blocked_until)}."
            )
        logger.error("Provider blocked: %s", text)
        self.record_error(text)
        self.alert(text)

    def _count_failure(self, provider: BaseMapProvider, error: str) -> None:
        count = self.failures_in_a_row.get(provider.name, 0) + 1
        self.failures_in_a_row[provider.name] = count
        if count >= MAX_CONSECUTIVE_FAILURES:
            logger.warning("Provider %s skipped for this run after %d failures in a row", provider.name, count)
            # A source that stopped working (or rejects every request) must not fail silently.
            self.alert(f"Источник {provider.name} пропущен до следующего сбора после {count} ошибок подряд: {error}")
            self.drop_provider(provider)


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m %H:%M МСК")


async def _close_providers(providers: list[BaseMapProvider]) -> None:
    """Each provider is closed whatever happens to the others: no browser may be left running."""
    for provider in providers:
        try:
            await provider.close()
        except Exception:
            logger.warning("Closing provider %s failed", provider.name, exc_info=True)


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
    now = datetime.now(UTC)
    async with session_maker() as session:
        states = await get_provider_states(session)
    skipped = {provider.name: unavailable_reason(provider, states.get(provider.name), now) for provider in providers}
    usable = [provider for provider in providers if skipped[provider.name] is None]
    resting = {name for name, reason in skipped.items() if reason == REST_REASON}

    collection = _Collection(usable, config, budget, session_maker)
    result = collection.result
    result.skipped = {name: reason for name, reason in skipped.items() if reason is not None}
    for name, reason in result.skipped.items():
        logger.info("Provider %s skipped: %s", name, reason)
    targets = config.targets
    start = cursor % len(targets) if targets else 0

    try:
        async with session_maker() as session:
            for provider in usable:
                if provider.min_hours_between_runs:
                    await record_provider_run(session, provider.name)
        for offset in range(len(targets)):
            if result.requests >= budget or not collection.active:
                break
            target = targets[(start + offset) % len(targets)]
            for provider in list(collection.active):
                # A fallback works only while the provider it replaces is out of action;
                # one that is merely resting until tomorrow does not need replacing.
                if provider.fallback_for in {other.name for other in collection.active} | resting:
                    continue
                await collection.collect_target(provider, target)
            result.targets_processed = offset + 1
    finally:
        await _close_providers(providers)
    result.next_cursor = (start + result.targets_processed) % len(targets) if targets else 0

    async with session_maker() as session:
        for name in collection.succeeded:
            await record_provider_success(session, name)
        stats = await save_collected_places(session, collection.places)
    result.found = stats["total_found"]
    result.qualified = stats["qualified"]
    result.saved_new = stats["saved_new"]
    result.duplicates = stats["duplicates_skipped"]
    return result
