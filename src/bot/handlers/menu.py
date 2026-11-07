"""The bottom menu, /help and the step-by-step guide to enrichment. The work itself is done by existing handlers."""

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, ReplyKeyboardMarkup

from src.bot.chats import is_group_chat
from src.bot.formatters import format_help
from src.bot.handlers.imports import EXPORT_UNKNOWN_DEFAULT, handle_niches, send_enrich_request
from src.bot.handlers.issue import handle_leads_stats
from src.bot.keyboards import (
    ENRICH_REQUEST_CALLBACK,
    STALE_BUTTON_TEXT,
    get_enrich_request_keyboard,
    get_main_menu_keyboard,
)
from src.bot.middlewares import PUBLIC_FLAG, is_admin
from src.bot.texts import ENRICH_BUTTON_TEXT, ENRICH_GUIDE_TEXT, HELP_BUTTON_TEXT, STATS_BUTTON_TEXT

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


@router.message(F.text == ENRICH_BUTTON_TEXT)
async def handle_enrich_guide(message: Message) -> None:
    """Five steps from a request file to the leads, with the command for the terminal."""
    await message.answer(ENRICH_GUIDE_TEXT, parse_mode="HTML", reply_markup=get_enrich_request_keyboard())


@router.callback_query(F.data == ENRICH_REQUEST_CALLBACK)
async def handle_enrich_request_button(callback: CallbackQuery) -> None:
    """The same as /export_unknown 30. The press is answered first: building the file takes a moment."""
    if not isinstance(callback.message, Message):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    await callback.answer("Готовлю файл запроса…")
    await send_enrich_request(callback.message, EXPORT_UNKNOWN_DEFAULT)


@router.message(F.text == STATS_BUTTON_TEXT)
async def handle_stats_menu(message: Message) -> None:
    """The funnel with its CSV export, then the table of niches."""
    await handle_leads_stats(message)
    await handle_niches(message)
