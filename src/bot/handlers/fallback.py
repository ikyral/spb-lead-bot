"""A hint instead of silence: text no other handler took. Included last, works in private chats only."""

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from src.bot.handlers.menu import private_admin_keyboard
from src.bot.middlewares import PUBLIC_FLAG, is_admin
from src.bot.texts import NOT_UNDERSTOOD_ADMIN_TEXT, NOT_UNDERSTOOD_USER_TEXT

logger = logging.getLogger(__name__)
router = Router(name="fallback")


# Groups are left alone: with privacy mode off the bot receives every message there.
@router.message(F.chat.type == "private", F.text, flags={PUBLIC_FLAG: True})
async def handle_not_understood(message: Message) -> None:
    """An unknown command or any other text: say so and show what can be done."""
    admin = is_admin(message)
    try:
        await message.answer(
            NOT_UNDERSTOOD_ADMIN_TEXT if admin else NOT_UNDERSTOOD_USER_TEXT,
            reply_markup=private_admin_keyboard(message),
        )
    except TelegramAPIError as exc:
        logger.warning("Could not answer an unclear message: %s", exc)
