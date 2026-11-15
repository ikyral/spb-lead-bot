"""The morning plan for admins: what is in the pool and whom to remind. Only reads; never writes to businesses."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.models import Lead
from src.database.repo import DigestCounts, OwnerSummary, get_digest_counts, get_followups, get_owner_summary
from src.services.scheduler import RequestUsage

# Leads to remind about in one plan, each in a message of its own.
FOLLOWUPS_SHOWN: int = 10
# A plan missed while the bot was off is not sent after this time: it would come too late for the day.
DIGEST_LATEST: time = time(12, 0)
# With DIGEST_TIME at noon or later the plan may still come this long after it.
_LATE_DIGEST_WINDOW: timedelta = timedelta(hours=2)

UsageSource = Callable[[], Awaitable[RequestUsage]]


@dataclass(frozen=True)
class DigestData:
    day: date
    counts: DigestCounts
    followups: list[Lead]
    followup_after_days: int
    usage: RequestUsage | None
    # The leads of the person who asked (/today); None for the morning plan, which is common to everyone.
    owner: OwnerSummary | None = None


def digest_bounds(now: datetime, timezone: ZoneInfo, followup_after_days: int) -> tuple[datetime, datetime]:
    """Reminders due before the start of tomorrow, and 'wrote to them' long enough ago to ask again."""
    tomorrow = now.astimezone(timezone).date() + timedelta(days=1)
    due_before = datetime.combine(tomorrow, time(0), timezone)
    return due_before, now - timedelta(days=followup_after_days)


def digest_window_open(now: datetime, timezone: ZoneInfo, start: time) -> bool:
    """May the plan of today go out now? From DIGEST_TIME until noon (or two hours later for a late DIGEST_TIME)."""
    local = now.astimezone(timezone)
    if start < DIGEST_LATEST:
        end = DIGEST_LATEST
    else:
        latest = datetime.combine(local.date(), start) + _LATE_DIGEST_WINDOW
        end = latest.time() if latest.date() == local.date() else time.max
    return start <= local.time() < end


async def load_digest(
    session_maker: async_sessionmaker[AsyncSession],
    now: datetime,
    timezone: ZoneInfo,
    followup_after_days: int,
    usage: UsageSource | None = None,
    owner_id: int | None = None,
) -> DigestData:
    """The plan for everyone, or with owner_id only the reminders and leads of that person (the pool is shared)."""
    due_before, stale_before = digest_bounds(now, timezone, followup_after_days)
    owner: OwnerSummary | None = None
    async with session_maker() as session:
        counts = await get_digest_counts(session, due_before, stale_before, owner_id)
        followups = await get_followups(session, due_before, stale_before, FOLLOWUPS_SHOWN, owner_id)
        if owner_id is not None:
            owner = await get_owner_summary(session, owner_id, now)
    return DigestData(
        day=now.astimezone(timezone).date(),
        counts=counts,
        followups=followups,
        followup_after_days=followup_after_days,
        usage=await usage() if usage is not None else None,
        owner=owner,
    )
