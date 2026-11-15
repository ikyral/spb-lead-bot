"""Periodic return of leads whose ownership has run out. Only the database; nothing is sent to anyone."""

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.repo import release_expired_leads

logger = logging.getLogger(__name__)

RELEASE_JOB_ID = "release_expired_ownership"
# Every 15 minutes: a lead is back in the pool at most this late after its owner's time is over.
RELEASE_INTERVAL_SECONDS: int = 900


async def release_expired_job(session_maker: async_sessionmaker[AsyncSession], now: datetime | None = None) -> int:
    """The scheduler's run: returns how many leads went back. Never raises, so the schedule goes on."""
    try:
        async with session_maker() as session:
            released = await release_expired_leads(session, now)
    except Exception:
        logger.exception("Returning expired leads to the pool failed")
        return 0
    if released:
        logger.info("Returned to the pool after their ownership ran out: %d lead(s)", released)
    return released
