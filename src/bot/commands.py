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


def admin_commands(owner: bool = True) -> list[BotCommand]:
    """Everything for the owner of the bot; without what changes shared data for an operator."""
    return [
        BotCommand(command=item.command, description=item.menu)
        for section in HELP_SECTIONS
        for item in section.commands
        if owner or not item.owner_only
    ]


async def register_bot_commands(bot: Bot, owner_id: int, operator_ids: Iterable[int] = ()) -> None:
    """Set the menus. A failure is only logged: the bot works without a menu, and a chat may not exist yet."""
    await _set_commands(bot, public_commands(), BotCommandScopeDefault(), "everyone")
    # 'chat not found' until an admin has pressed /start: the menu appears after the next restart.
    await _set_commands(bot, admin_commands(), BotCommandScopeChat(chat_id=owner_id), f"owner {owner_id}")
    for operator_id in sorted(set(operator_ids) - {owner_id}):
        await _set_commands(
            bot, admin_commands(owner=False), BotCommandScopeChat(chat_id=operator_id), f"operator {operator_id}"
        )


async def _set_commands(
    bot: Bot, commands: list[BotCommand], scope: BotCommandScopeDefault | BotCommandScopeChat, who: str
) -> None:
    try:
        await bot.set_my_commands(commands, scope=scope)
    except TelegramAPIError as exc:
        logger.warning("Could not set the command menu for %s: %s", who, exc)
    except Exception:
        logger.exception("Unexpected error while setting the command menu for %s", who)
