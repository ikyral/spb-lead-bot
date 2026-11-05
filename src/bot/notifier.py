"""Messages to admins that are not replies to a command: collection results and alerts."""

import html
import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from src.bot.formatters import format_collection_report
from src.services.scheduler import TRIGGER_LOW_POOL, TRIGGER_SCHEDULE, RunReport

logger = logging.getLogger(__name__)

# Nobody is waiting for these runs and they may finish at night.
_SILENT_TRIGGERS: frozenset[str] = frozenset({TRIGGER_SCHEDULE, TRIGGER_LOW_POOL})


class AdminNotifier:
    """Sends a message to every admin; a failure for one of them does not affect the others."""

    def __init__(self, bot: Bot, admin_ids: Iterable[int]) -> None:
        self._bot = bot
        self._admin_ids = sorted(admin_ids)

    async def send(self, text: str, silent: bool = False) -> None:
        for admin_id in self._admin_ids:
            try:
                await self._bot.send_message(admin_id, text, parse_mode="HTML", disable_notification=silent)
            except TelegramAPIError as exc:
                logger.warning("Failed to notify admin %s: %s", admin_id, exc)

    async def report(self, report: RunReport) -> None:
        """Alerts always reach admins; the summary only if there is something to tell."""
        for alert in report.new_alerts:
            await self.send(f"🚨 <b>Сбор лидов:</b> {html.escape(alert)}")
        if report.announce:
            await self.send(format_collection_report(report), silent=report.trigger in _SILENT_TRIGGERS)
