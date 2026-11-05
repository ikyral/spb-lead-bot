"""Admin commands for automatic lead collection. Access is restricted by AdminOnlyMiddleware."""

import html

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from src.bot.formatters import format_collector_status
from src.services.scheduler import TRIGGER_MANUAL, CollectorRunner

router = Router(name="collector")


@router.message(Command("collect_now"))
async def handle_collect_now(message: Message, collector_runner: CollectorRunner) -> None:
    """Start a collection in the background; the result is sent when it finishes."""
    if collector_runner.start_in_background(TRIGGER_MANUAL):
        await message.answer("▶️ Сбор запущен. Итог пришлю, когда он закончится.")
    else:
        await message.answer("⏳ Сбор уже выполняется. Итог придёт, когда он закончится.")


@router.message(Command("collect_status"))
async def handle_collect_status(message: Message, collector_runner: CollectorRunner) -> None:
    """Show the schedule, the sources and the result of the last run."""
    await message.answer(format_collector_status(await collector_runner.get_status()), parse_mode="HTML")


@router.message(Command("collect_on"))
async def handle_collect_on(message: Message, collector_runner: CollectorRunner) -> None:
    """Enable automatic collection."""
    await collector_runner.set_auto_enabled(True)
    await message.answer("✅ Автосбор включён.")


@router.message(Command("collect_off"))
async def handle_collect_off(message: Message, collector_runner: CollectorRunner) -> None:
    """Disable automatic collection: both the schedule and refilling the pool."""
    await collector_runner.set_auto_enabled(False)
    await message.answer("⏸ Автосбор выключен. Сбор вручную по-прежнему работает: /collect_now")


@router.message(Command("provider_on"))
async def handle_provider_on(message: Message, command: CommandObject, collector_runner: CollectorRunner) -> None:
    """Turn a source back on after blocks. This is the only way out of an automatic switch-off."""
    name = (command.args or "2gis_scraper").strip()
    if not collector_runner.known_provider(name):
        await message.answer(f"Неизвестный источник: {html.escape(name)}. Список: /collect_status", parse_mode="HTML")
        return
    await collector_runner.enable_provider(name)
    await message.answer(
        f"✅ Источник {html.escape(name)} включён, счётчик блокировок сброшен. "
        "Если сайт снова откажет, источник остановится сам.",
        parse_mode="HTML",
    )

