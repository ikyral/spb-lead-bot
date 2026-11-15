"""The bottom menu and /help. The work itself is done by existing handlers."""

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup

from src.bot.chats import is_group_chat
from src.bot.formatters import format_help
from src.bot.handlers.issue import handle_leads_stats
from src.bot.keyboards import get_main_menu_keyboard
from src.bot.middlewares import PUBLIC_FLAG, is_admin
from src.bot.texts import HELP_BUTTON_TEXT, STATS_BUTTON_TEXT
from src.services.scheduler import CollectorRunner

logger = logging.getLogger(__name__)
router = Router(name="menu")


def private_admin_keyboard(message: Message) -> ReplyKeyboardMarkup | None:
    """The bottom keyboard goes only to admins in private: in a group it would show up for everyone."""
    if is_group_chat(message) or not is_admin(message):
        return None
    return get_main_menu_keyboard()


@router.message(Command("help"), flags={PUBLIC_FLAG: True})
@router.message(F.text == HELP_BUTTON_TEXT, flags={PUBLIC_FLAG: True})
async def handle_help(message: Message) -> None:
    """Commands by group with examples. Admin commands are shown to admins only."""
    texts = format_help(is_admin(message))
    keyboard = private_admin_keyboard(message)
    try:
        for index, text in enumerate(texts):
            await message.answer(text, parse_mode="HTML", reply_markup=keyboard if index == len(texts) - 1 else None)
    except TelegramAPIError as exc:
        logger.warning("Could not send the help: %s", exc)


@router.message(F.text == STATS_BUTTON_TEXT)
async def handle_stats_menu(message: Message, collector_runner: CollectorRunner) -> None:
    """The funnel with its CSV export and the API requests."""
    await handle_leads_stats(message, collector_runner)
