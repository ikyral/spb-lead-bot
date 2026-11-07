"""Bot middlewares for security and access control."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.bot.chats import is_group_chat
from src.bot.texts import ADMIN_ONLY_TEXT, NO_ACCESS_TEXT
from src.config import settings

logger = logging.getLogger(__name__)

__all__ = ["NO_ACCESS_TEXT", "PUBLIC_FLAG", "AdminOnlyMiddleware", "is_admin"]

# A handler with flags={PUBLIC_FLAG: True} answers anyone in a private chat; it decides itself what to show.
PUBLIC_FLAG: str = "public"
# A refusal to one person in one chat comes this rarely: in a group everyone sees it, in private it is a hint.
_REPLY_COOLDOWN_SECONDS: float = 300.0
# Bounds the memory of who was answered; entries older than the cooldown are useless anyway.
_MAX_REMEMBERED_REPLIES: int = 1000


class AdminOnlyMiddleware(BaseMiddleware):
    """
    Lets only configured admins (ADMIN_TG_ID and ADMIN_TG_IDS) through, in any chat.

    The ID of the user who pressed or wrote is checked, never the chat's. Registered as an inner
    middleware it runs only for updates a handler matched, so ordinary group chatter never gets here.
    Public handlers (/start, /help, the hint on unclear text) are let through in private chats only.
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
        if isinstance(event, Message) and not is_group_chat(event) and get_flag(data, PUBLIC_FLAG):
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
                if is_group_chat(event):
                    await event.reply(NO_ACCESS_TEXT)
                else:
                    await event.answer(ADMIN_ONLY_TEXT)
        except TelegramAPIError as exc:
            logger.warning("Could not refuse access to user %s: %s", user_id, exc)

    def _should_reply(self, message: Message, user_id: int) -> bool:
        """Only commands and buttons, not files; once in a while, so a refusal cannot be used to flood a chat."""
        if not getattr(message, "text", None):
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


def is_admin(message: Message) -> bool:
    """The same check as the middleware: by the ID of the user who wrote, never of the chat."""
    user = getattr(message, "from_user", None)
    return user is not None and user.id in settings.admin_ids
