"""Bot middlewares for security and access control."""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.config import settings


class AdminOnlyMiddleware(BaseMiddleware):
    """Restricts bot access strictly to the configured ADMIN_TG_ID."""

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

        if not from_user or from_user.id != settings.ADMIN_TG_ID:
            return None

        return await handler(event, data)
