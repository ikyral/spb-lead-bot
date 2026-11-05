"""Bot middlewares for security and access control."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.bot.chats import is_group_chat
from src.config import settings

logger = logging.getLogger(__name__)

NO_ACCESS_TEXT: str = "⛔ Нет доступа"
# In a group the refusal is seen by everyone, so one person gets it this rarely.
_REPLY_COOLDOWN_SECONDS: float = 300.0
# Bounds the memory of who was answered; entries older than the cooldown are useless anyway.
_MAX_REMEMBERED_REPLIES: int = 1000


class AdminOnlyMiddleware(BaseMiddleware):
    """
    Lets only configured admins (ADMIN_TG_ID and ADMIN_TG_IDS) through, in any chat.

    The ID of the user who pressed or wrote is checked, never the chat's. Registered as an inner
    middleware it runs only for updates a handler matched, so ordinary group chatter never gets here.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._replied_at: dict[tuple[int, int], float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = None
        if isinstance(event, (Message, CallbackQuery)):
            from_user = event.from_user
        elif "event_from_user" in data:
            from_user = data["event_from_user"]

        if from_user and from_user.id in settings.admin_ids:
            return await handler(event, data)

        if from_user:
            await self._refuse(event, from_user.id)
        return None

    async def _refuse(self, event: TelegramObject, user_id: int) -> None:
        try:
            if isinstance(event, CallbackQuery):
                # Only the person who pressed sees an alert, so it cannot flood a group.
                await event.answer(NO_ACCESS_TEXT, show_alert=True)
            elif isinstance(event, Message) and self._should_reply(event, user_id):
                await event.reply(NO_ACCESS_TEXT)
        except TelegramAPIError as exc:
            logger.warning("Could not refuse access to user %s: %s", user_id, exc)

    def _should_reply(self, message: Message, user_id: int) -> bool:
        """Only commands and buttons in groups: the bot stays silent to strangers in private, as always."""
        if not is_group_chat(message) or not message.text:
            return False
        key = (message.chat.id, user_id)
        now = self._clock()
        last = self._replied_at.get(key)
        if last is not None and now - last < _REPLY_COOLDOWN_SECONDS:
            return False
        if len(self._replied_at) >= _MAX_REMEMBERED_REPLIES:
            self._replied_at.clear()
        self._replied_at[key] = now
        return True
