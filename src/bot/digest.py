"""The morning plan: built from the database, sent to admins and notification chats once a day, or on /today."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.bot.formatters import format_digest_summary, format_followup
from src.bot.keyboards import get_followup_keyboard, get_next_batch_keyboard
from src.bot.notifier import AdminNotifier, Outgoing
from src.database.repo import claim_digest_day
from src.services.digest import UsageSource, digest_window_open, load_digest

logger = logging.getLogger(__name__)

DIGEST_JOB_ID = "daily_digest"


class DailyDigest:
    """Owns the morning plan. Only admins and the notification chats get it; nothing goes to businesses."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        notifier: AdminNotifier,
        timezone: ZoneInfo,
        start: time,
        followup_after_days: int,
        usage: UsageSource | None = None,
        enabled: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        bot_owner_id: int | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._notifier = notifier
        self._timezone = timezone
        self.start = start
        self._followup_after_days = followup_after_days
        self._usage = usage
        self.enabled = enabled
        self._clock = clock
        self._bot_owner_id = bot_owner_id

    async def build(
        self, now: datetime | None = None, user_id: int | None = None, personal: bool = True
    ) -> list[Outgoing]:
        """
        The summary with the 'next batch' button, then a message with buttons for each lead to remind about.

        Of `user_id`: their reminders, and each person's leads if it is the owner of the bot. Not `personal`:
        the plan of a group, the pool and the people without anybody's reminders.
        """
        with_people = not personal or (user_id is not None and user_id == self._bot_owner_id)
        data = await load_digest(
            self._session_maker,
            now or self._clock(),
            self._timezone,
            self._followup_after_days,
            self._usage,
            user_id=user_id,
            with_people=with_people,
            personal=personal,
        )
        messages: list[Outgoing] = [(format_digest_summary(data), get_next_batch_keyboard())]
        messages += [(format_followup(lead), get_followup_keyboard(lead.id)) for lead in data.followups]
        return messages

    async def send_scheduled(self) -> None:
        """
        The run of the scheduler: at DIGEST_TIME, and once right after the start to catch up on a missed day.

        Not before DIGEST_TIME, not after noon, and once a day: the day is marked in the database before
        sending, so a restart after a deployment does not send it again. Never raises: the schedule goes on.
        """
        try:
            if not self.enabled:
                return
            now = self._clock()
            local_time = now.astimezone(self._timezone).strftime("%H:%M")
            if not digest_window_open(now, self._timezone, self.start):
                logger.info("Morning plan not sent at %s: outside its time from %s", local_time, self.start)
                return
            # Built before the day is marked: a plan that could not be built is tried again at the next start.
            plans = {admin: await self.build(now, user_id=admin) for admin in self._notifier.admin_targets()}
            shared = await self.build(now, personal=False) if self._notifier.chat_targets() else []
            async with self._session_maker() as session:
                claimed = await claim_digest_day(session, now.astimezone(self._timezone).date().isoformat())
            if not claimed:
                logger.info("Morning plan not sent at %s: it went out today already", local_time)
                return
            logger.info(
                "Sending the morning plan to %d admin(s) and %d chat(s)", len(plans), len(self._notifier.chat_targets())
            )
            # Each admin gets their own plan; a group gets what is shared. One that refuses does not stop the rest.
            await asyncio.gather(
                *(self._notifier.send_sequence_to(admin, messages) for admin, messages in plans.items()),
                *(self._notifier.send_sequence_to(chat, shared) for chat in self._notifier.chat_targets()),
            )
        except Exception:
            logger.exception("The morning plan failed")
