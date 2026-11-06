"""Messages that are not replies to a command: collection results and alerts, to group chats and admins."""

import asyncio
import html
import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramMigrateToChat, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from src.bot.formatters import format_collection_report, format_import_messages
from src.services.lead_import import ImportResult
from src.services.scheduler import TRIGGER_LOW_POOL, TRIGGER_SCHEDULE, RunReport

logger = logging.getLogger(__name__)

# Nobody is waiting for these runs and they may finish at night.
_SILENT_TRIGGERS: frozenset[str] = frozenset({TRIGGER_SCHEDULE, TRIGGER_LOW_POOL})
# One message: the first try, a retry after a flood wait or a group migration, and one more.
_MAX_ATTEMPTS: int = 3
# A longer wait is not worth holding up the end of a collection run: the message is dropped.
_MAX_FLOOD_WAIT_SECONDS: float = 60.0


class AdminNotifier:
    """Sends a message to every target at once; a failure for one of them does not affect the others."""

    def __init__(
        self,
        bot: Bot,
        admin_ids: Iterable[int],
        chat_ids: Iterable[int] = (),
        notify_admins: bool = True,
    ) -> None:
        self._bot = bot
        self._admin_ids = sorted(admin_ids)
        self._chat_ids = list(chat_ids)
        self._notify_admins = notify_admins

    def _targets(self) -> list[int]:
        """Chats, then admins. Without chats admins are always told: otherwise nobody would be."""
        admins = self._admin_ids if self._notify_admins or not self._chat_ids else []
        return list(dict.fromkeys([*self._chat_ids, *admins]))

    async def send(
        self, text: str, silent: bool = False, exclude_chat: int | None = None, link_preview: bool = True
    ) -> None:
        targets = [target for target in self._targets() if target != exclude_chat]
        await asyncio.gather(*(self._send_to(target, text, silent, link_preview) for target in targets))

    async def _send_to(self, target: int, text: str, silent: bool, link_preview: bool = True) -> None:
        """Never raises: a problem with one chat must not reach other chats or the collection."""
        migrated = False
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                await self._bot.send_message(
                    target,
                    text,
                    parse_mode="HTML",
                    disable_notification=silent,
                    link_preview_options=None if link_preview else LinkPreviewOptions(is_disabled=True),
                )
            except TelegramMigrateToChat as exc:
                new_target = exc.migrate_to_chat_id
                if not self._remember_migration(target, new_target):
                    return
                target, migrated = new_target, True
            except TelegramRetryAfter as exc:
                if attempt == _MAX_ATTEMPTS or exc.retry_after > _MAX_FLOOD_WAIT_SECONDS:
                    logger.warning("Message to chat %s dropped: Telegram asks to wait %s s", target, exc.retry_after)
                    return
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramAPIError as exc:
                logger.warning("Failed to notify chat %s: %s", target, exc)
                return
            except Exception:
                logger.exception("Failed to notify chat %s", target)
                return
            else:
                if migrated:
                    await self._tell_about_migration(target)
                return
        logger.warning("Message to chat %s dropped after %d attempts", target, _MAX_ATTEMPTS)

    def _remember_migration(self, old_id: int, new_id: int) -> bool:
        """
        Use the new ID until restart; .env is the owner's file, so it is only reported.

        Returns False if the new chat already gets its own copy of the message.
        """
        logger.warning(
            "Chat %s has become supergroup %s: replace the ID in NOTIFY_CHAT_IDS, using the new one until restart",
            old_id,
            new_id,
        )
        already_known = new_id in self._targets()
        self._chat_ids = [chat_id for chat_id in self._chat_ids if chat_id != old_id]
        if not already_known:
            self._chat_ids.append(new_id)
        return not already_known

    async def _tell_about_migration(self, chat_id: int) -> None:
        text = (
            f"ℹ️ Группа стала супергруппой, её новый ID: <code>{chat_id}</code>. "
            "Укажите его в <code>NOTIFY_CHAT_IDS</code>, бот использует его только до перезапуска."
        )
        try:
            await self._bot.send_message(chat_id, text, parse_mode="HTML")
        except TelegramAPIError as exc:
            logger.warning("Failed to notify chat %s about its new ID: %s", chat_id, exc)

    async def report_import(self, result: ImportResult, exclude_chat: int | None = None) -> None:
        """The outcome of an imported file. The chat it was sent from sees the result there."""
        # One after another, or a long list would arrive out of order.
        for text in format_import_messages(result):
            await self.send(text, exclude_chat=exclude_chat, link_preview=False)

    async def remind_pool_low(self, pool_size: int) -> None:
        """Ask for a new file from the external collector."""
        await self.send(
            f"📉 В запасе осталось лидов: <b>{pool_size}</b>, нужен новый импорт. "
            "Загрузите файл с внешнего сборщика: отправьте его боту или положите в папку импорта на сервере."
        )

    async def report(self, report: RunReport) -> None:
        """Alerts always go out; the summary only if there is something to tell."""
        for alert in report.new_alerts:
            await self.send(f"🚨 <b>Сбор лидов:</b> {html.escape(alert)}")
        if report.announce:
            await self.send(format_collection_report(report), silent=report.trigger in _SILENT_TRIGGERS)
