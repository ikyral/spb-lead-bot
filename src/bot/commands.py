"""The command menu of Telegram: a short one for everyone, the full one in the private chat of each admin."""

import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from src.bot.texts import HELP_SECTIONS

logger = logging.getLogger(__name__)


def public_commands() -> list[BotCommand]:
    return [
        BotCommand(command=item.command, description=item.menu)
        for section in HELP_SECTIONS
        for item in section.commands
        if not item.admin
    ]


def admin_commands() -> list[BotCommand]:
    return [
        BotCommand(command=item.command, description=item.menu)
        for section in HELP_SECTIONS
        for item in section.commands
    ]


async def register_bot_commands(bot: Bot, admin_ids: Iterable[int]) -> None:
    """Set both menus. A failure is only logged: the bot works without a menu, and a chat may not exist yet."""
    await _set_commands(bot, public_commands(), BotCommandScopeDefault(), "everyone")
    full = admin_commands()
    for admin_id in sorted(admin_ids):
        # 'chat not found' until the admin has pressed /start: the menu appears after the next restart.
        await _set_commands(bot, full, BotCommandScopeChat(chat_id=admin_id), f"admin {admin_id}")


async def _set_commands(
    bot: Bot, commands: list[BotCommand], scope: BotCommandScopeDefault | BotCommandScopeChat, who: str
) -> None:
    try:
        await bot.set_my_commands(commands, scope=scope)
    except TelegramAPIError as exc:
        logger.warning("Could not set the command menu for %s: %s", who, exc)
    except Exception:
        logger.exception("Unexpected error while setting the command menu for %s", who)
