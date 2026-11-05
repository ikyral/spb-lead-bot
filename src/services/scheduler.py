"""Runs lead collection on schedule and on request: never two at once, never fatal for the bot."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.models import CollectorState, ImportLog, ProviderState
from src.database.repo import (
    count_imported_leads,
    count_new_leads,
    enable_provider,
    get_collector_state,
    get_last_import,
    get_provider_states,
)
from src.services.collector import CollectorConfig, CollectResult, collect_leads
from src.services.providers.registry import ProviderSet

logger = logging.getLogger(__name__)

COLLECT_JOB_ID = "lead_collection"
POOL_CHECK_JOB_ID = "lead_pool_check"
POOL_CHECK_INTERVAL_MINUTES: int = 15
# Out-of-schedule collection because of a low pool happens at most this often.
TOPUP_COOLDOWN: timedelta = timedelta(hours=1)
# With an external collector admins are asked for a new file at most this often.
POOL_REMINDER_INTERVAL: timedelta = timedelta(days=1)
# A run missed by up to this much (bot busy or just restarted) still starts.
MISFIRE_GRACE_SECONDS: int = 3600
# Docker gives a container 10 s to stop; a run cancelled after this wait is safe,
# because leads are saved in one transaction and the next run finds the same places.
SHUTDOWN_WAIT_SECONDS: float = 5.0

TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"
TRIGGER_BATCH = "batch"
TRIGGER_LOW_POOL = "low_pool"
# Someone is waiting for the outcome of these runs, so it is reported even with no new leads.
_ANNOUNCE_ALWAYS: frozenset[str] = frozenset({TRIGGER_MANUAL, TRIGGER_BATCH})

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_DAILY_LIMIT = "daily_limit"


class TopupOutcome(Enum):
    """Why an out-of-schedule collection did or did not start."""

    STARTED = "started"
    RUNNING = "running"
    COOLDOWN = "cooldown"
    AUTO_OFF = "auto_off"
    DAILY_LIMIT = "daily_limit"
    EXTERNAL = "external"


@dataclass(frozen=True)
class TopupDecision:
    outcome: TopupOutcome
    retry_at: datetime | None = None


@dataclass
class RunReport:
    """What the bot layer needs to tell admins about a finished run."""

    trigger: str
    status: str
    duration_seconds: float
    result: CollectResult
    pool_size: int
    new_alerts: list[str] = field(default_factory=list)
    announce: bool = False


@dataclass
class CollectorStatus:
    """Snapshot for the status command."""

    state: CollectorState
    auto_enabled: bool
    is_running: bool
    next_run_at: datetime | None
    schedule_text: str
    pool_size: int
    active_providers: list[str]
    disabled_providers: dict[str, str]
    requests_today: int
    max_requests_per_day: int
    provider_states: dict[str, ProviderState] = field(default_factory=dict)
    external_mode: bool = False
    last_import: ImportLog | None = None
    imported_total: int = 0


ReportCallback = Callable[[RunReport], Awaitable[None]]
PoolReminderCallback = Callable[[int], Awaitable[None]]


class CollectorRunner:
    """Owns the scheduler and the single running collection."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        provider_set: ProviderSet,
        config: CollectorConfig,
        report: ReportCallback,
        enabled_by_default: bool = True,
        run_on_start: bool = False,
        min_pool: int = 0,
        external_mode: bool = False,
        pool_reminder: PoolReminderCallback | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._provider_set = provider_set
        self._config = config
        self._report = report
        self._enabled_by_default = enabled_by_default
        self._run_on_start = run_on_start
        self._min_pool = min_pool
        # Leads come from an external collector as files: this server never collects by itself.
        self.external_mode = external_mode
        self._pool_reminder = pool_reminder
        self._scheduler = AsyncIOScheduler(timezone=config.timezone)
        self._auto_enabled = enabled_by_default
        # asyncio is single-threaded: checking and setting this flag cannot interleave.
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the scheduler. Called when the bot starts."""
        async with self._session_maker() as session:
            state = await get_collector_state(session)
            if state.last_status == STATUS_RUNNING:
                # The previous process died in the middle of a run.
                state.last_status = STATUS_INTERRUPTED
            if state.auto_enabled is not None:
                self._auto_enabled = state.auto_enabled
            await session.commit()

        schedule_args: dict[str, datetime | None] = {}
        if not self._auto_enabled:
            schedule_args["next_run_time"] = None  # added paused
        elif self._run_on_start:
            schedule_args["next_run_time"] = datetime.now(self._config.timezone)
        if not self.external_mode:
            self._scheduler.add_job(
                self._run_scheduled,
                self._config.trigger,
                id=COLLECT_JOB_ID,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=MISFIRE_GRACE_SECONDS,
                **schedule_args,
            )
        if self._min_pool > 0:
            self._scheduler.add_job(
                self.check_pool,
                "interval",
                minutes=POOL_CHECK_INTERVAL_MINUTES,
                id=POOL_CHECK_JOB_ID,
                max_instances=1,
                coalesce=True,
            )
        self._scheduler.start()
        logger.info(
            "Collector scheduler started: %s, auto=%s, providers=%s, targets=%d",
            "external collector, no collection here" if self.external_mode else self._config.schedule_text,
            self._auto_enabled,
            [provider.name for provider in self._provider_set.providers],
            len(self._config.targets),
        )

    def add_interval_job(self, job: Callable[[], Awaitable[object]], seconds: int, job_id: str) -> None:
        """Run another periodic task of the bot here. Call before start()."""
        self._scheduler.add_job(job, "interval", seconds=seconds, id=job_id, max_instances=1, coalesce=True)

    async def shutdown(self, wait_seconds: float = SHUTDOWN_WAIT_SECONDS) -> None:
        """Stop with the bot: give a running collection a moment to finish, then cancel it."""
        if self._scheduler.running:
            self._scheduler.pause()
        task = self._task
        if self._running and task is not None and not task.done():
            done, _pending = await asyncio.wait({task}, timeout=wait_seconds)
            if not done:
                logger.info("Cancelling the running collection on shutdown")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            # AsyncIOScheduler performs the shutdown in the next iteration of the event loop.
            await asyncio.sleep(0)

    def start_in_background(self, trigger: str = TRIGGER_MANUAL) -> bool:
        """Start a collection without waiting for it. Returns False if one is already running."""
        if self._running or self.external_mode:
            return False
        self._running = True
        self._task = asyncio.create_task(self._guarded_run(trigger))
        return True

    async def run_once(self, trigger: str) -> bool:
        """Run a collection and wait for it. Returns False if one is already running."""
        if self.external_mode:
            return False
        if self._running:
            logger.info("Collection (%s) skipped: another one is still running", trigger)
            return False
        self._running = True
        self._task = asyncio.current_task()
        await self._guarded_run(trigger)
        return True

    async def _run_scheduled(self) -> None:
        await self.run_once(TRIGGER_SCHEDULE)

    async def check_pool(self) -> None:
        """If the pool of new leads is below the threshold, start an extra collection or ask for a file."""
        if self._min_pool <= 0 or (not self._auto_enabled and not self.external_mode):
            return
        try:
            async with self._session_maker() as session:
                pool_size = await count_new_leads(session)
            if pool_size >= self._min_pool:
                return
            if self.external_mode:
                await self._ask_for_new_file(pool_size)
            else:
                decision = await self.request_topup(TRIGGER_LOW_POOL)
                logger.info("Lead pool is low (%d < %d): %s", pool_size, self._min_pool, decision.outcome.value)
        except Exception:
            # Runs from the scheduler and after issuing leads: must not affect either.
            logger.exception("Lead pool check failed")

    async def _ask_for_new_file(self, pool_size: int) -> None:
        """Tell admins the pool is running low, at most once a day; the time survives restarts."""
        now = datetime.now(UTC)
        async with self._session_maker() as session:
            state = await get_collector_state(session)
            last = state.last_pool_reminder_at
            if last is not None and now - last < POOL_REMINDER_INTERVAL:
                await session.commit()
                return
            state.last_pool_reminder_at = now
            await session.commit()
        logger.info("Lead pool is low (%d < %d): asking for a new file", pool_size, self._min_pool)
        if self._pool_reminder is not None:
            await self._pool_reminder(pool_size)

    async def request_topup(self, trigger: str = TRIGGER_LOW_POOL) -> TopupDecision:
        """Start an out-of-schedule collection, unless the limits forbid it right now."""
        if self.external_mode:
            await self.check_pool()
            return TopupDecision(TopupOutcome.EXTERNAL)
        if not self._auto_enabled:
            return TopupDecision(TopupOutcome.AUTO_OFF)
        if self._running:
            return TopupDecision(TopupOutcome.RUNNING)
        async with self._session_maker() as session:
            state = await get_collector_state(session)
            last_started, used = state.last_started_at, self._requests_used_today(state)
            await session.commit()
        if used >= self._config.max_requests_per_day:
            return TopupDecision(TopupOutcome.DAILY_LIMIT)
        if last_started is not None:
            retry_at = last_started + TOPUP_COOLDOWN
            if datetime.now(UTC) < retry_at:
                return TopupDecision(TopupOutcome.COOLDOWN, retry_at=retry_at)
        if self.start_in_background(trigger):
            return TopupDecision(TopupOutcome.STARTED)
        return TopupDecision(TopupOutcome.RUNNING)

    async def set_auto_enabled(self, enabled: bool) -> None:
        """Switch automatic collection; the choice survives restarts."""
        async with self._session_maker() as session:
            state = await get_collector_state(session)
            state.auto_enabled = enabled
            await session.commit()
        self._auto_enabled = enabled
        if self._scheduler.running:
            job = self._scheduler.get_job(COLLECT_JOB_ID)
            if job is not None and enabled:
                job.resume()
            elif job is not None:
                job.pause()
        logger.info("Automatic collection %s", "enabled" if enabled else "disabled")

    def known_provider(self, name: str) -> bool:
        return name in {provider.name for provider in self._provider_set.providers}

    async def enable_provider(self, name: str) -> None:
        """An admin's decision after an automatic switch-off."""
        async with self._session_maker() as session:
            await enable_provider(session, name)
        logger.info("Provider %s enabled by an admin", name)

    async def get_status(self) -> CollectorStatus:
        async with self._session_maker() as session:
            state = await get_collector_state(session)
            pool_size = await count_new_leads(session)
            provider_states = await get_provider_states(session)
            last_import = await get_last_import(session)
            imported_total = await count_imported_leads(session)
            await session.commit()
        job = self._scheduler.get_job(COLLECT_JOB_ID) if self._scheduler.running else None
        return CollectorStatus(
            state=state,
            auto_enabled=self._auto_enabled,
            is_running=self._running,
            next_run_at=job.next_run_time if job is not None else None,
            schedule_text=self._config.schedule_text,
            pool_size=pool_size,
            active_providers=[provider.name for provider in self._provider_set.providers],
            disabled_providers=dict(self._provider_set.disabled),
            requests_today=self._requests_used_today(state),
            max_requests_per_day=self._config.max_requests_per_day,
            provider_states=provider_states,
            external_mode=self.external_mode,
            last_import=last_import,
            imported_total=imported_total,
        )

    def _today(self) -> str:
        return datetime.now(self._config.timezone).date().isoformat()

    def _requests_used_today(self, state: CollectorState) -> int:
        return state.requests_used if state.requests_day == self._today() else 0

    async def _guarded_run(self, trigger: str) -> None:
        """Whatever happens inside, the flag is released and the scheduler keeps going."""
        try:
            await self._collect(trigger)
        except Exception:
            logger.exception("Collection (%s) crashed", trigger)
        finally:
            self._running = False

    async def _collect(self, trigger: str) -> None:
        started = time.monotonic()
        logger.info("Collection started: trigger=%s", trigger)

        async with self._session_maker() as session:
            state = await get_collector_state(session)
            used_today = self._requests_used_today(state)
            cursor, previous_alert = state.cursor, state.last_alert
            state.requests_day, state.requests_used = self._today(), used_today
            state.last_status, state.last_trigger = STATUS_RUNNING, trigger
            state.last_started_at, state.last_finished_at = datetime.now(UTC), None
            await session.commit()

        budget = min(self._config.max_requests_per_run, self._config.max_requests_per_day - used_today)
        try:
            status, result = await self._search_and_save(cursor, budget)
        except asyncio.CancelledError:
            await self._save_outcome(STATUS_INTERRUPTED, CollectResult(next_cursor=cursor), previous_alert)
            logger.warning("Collection interrupted: trigger=%s", trigger)
            raise
        except Exception:
            logger.exception("Collection failed: trigger=%s", trigger)
            text = "внутренняя ошибка сбора, подробности в логе бота"
            status = STATUS_FAILED
            result = CollectResult(
                next_cursor=cursor, errors=1, last_error=text, alerts=[f"Сбор не выполнен: {text}"]
            )

        new_alerts = await self._save_outcome(status, result, previous_alert)
        duration = time.monotonic() - started
        logger.info(
            "Collection finished: trigger=%s status=%s duration=%.1fs found=%d qualified=%d new=%d "
            "duplicates=%d errors=%d requests=%d",
            trigger, status, duration, result.found, result.qualified, result.saved_new,
            result.duplicates, result.errors, result.requests,
        )

        async with self._session_maker() as session:
            pool_size = await count_new_leads(session)
        report = RunReport(
            trigger=trigger,
            status=status,
            duration_seconds=duration,
            result=result,
            pool_size=pool_size,
            new_alerts=new_alerts,
            announce=trigger in _ANNOUNCE_ALWAYS or result.saved_new > 0,
        )
        try:
            await self._report(report)
        except Exception:
            logger.exception("Failed to report the collection result")

    async def _search_and_save(self, cursor: int, budget: int) -> tuple[str, CollectResult]:
        """Do the collection. Returns the run status and its result."""
        if budget <= 0:
            logger.warning(
                "Collection skipped: daily request limit reached (%d)", self._config.max_requests_per_day
            )
            return STATUS_DAILY_LIMIT, CollectResult(next_cursor=cursor)
        if not self._provider_set.providers:
            reasons = "; ".join(f"{name}: {why}" for name, why in self._provider_set.disabled.items())
            text = f"Нет доступных источников ({reasons or 'COLLECT_PROVIDERS пуст'})"
            logger.error("Collection impossible: %s", text)
            return STATUS_FAILED, CollectResult(next_cursor=cursor, errors=1, last_error=text, alerts=[text])

        result = await collect_leads(
            self._session_maker, self._provider_set.providers, self._config, cursor=cursor, max_requests=budget
        )
        if not result.errors:
            return STATUS_OK, result
        return (STATUS_PARTIAL if result.found else STATUS_FAILED), result

    async def _save_outcome(self, status: str, result: CollectResult, previous_alert: str | None) -> list[str]:
        """Store the result. Returns alerts that differ from the last reported ones."""
        alert_text = "\n".join(result.alerts) or None
        async with self._session_maker() as session:
            state = await get_collector_state(session)
            state.last_status = status
            state.last_finished_at = datetime.now(UTC)
            state.found = result.found
            state.qualified = result.qualified
            state.saved_new = result.saved_new
            state.duplicates = result.duplicates
            state.errors = result.errors
            state.last_error = result.last_error[:1024] if result.last_error else None
            # An interrupted run says nothing about whether the reported problem is gone.
            if status != STATUS_INTERRUPTED:
                state.last_alert = alert_text[:1024] if alert_text else None
            state.cursor = result.next_cursor
            state.requests_used += result.requests
            await session.commit()
        if alert_text is None or alert_text[:1024] == previous_alert:
            return []
        return list(result.alerts)
