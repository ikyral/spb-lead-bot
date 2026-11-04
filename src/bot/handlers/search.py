"""Search handlers for finding leads in SPb and importing CSV data."""

import html
import io
import logging
from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from src.bot.keyboards import (
    CATEGORY_KEYS,
    DISTRICT_NAMES,
    get_categories_keyboard,
    get_districts_keyboard,
    get_main_menu_keyboard,
)
from src.config import settings
from src.database.repo import async_session_maker
from src.services.pipeline import import_csv_harvest, run_lead_harvest
from src.services.providers.base import TARGET_CATEGORIES

logger = logging.getLogger(__name__)
router = Router(name="search")


async def _edit_or_send(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the callback message, or send a new one if it is too old to edit (InaccessibleMessage)."""
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    elif callback.message:
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Handle /start command, welcome admin, and display main menu."""
    text = (
        "👋 <b>Добро пожаловать в SPb Lead Finder!</b>\n\n"
        "Бот ищет потенциальных клиентов на разработку сайтов в Санкт-Петербурге:\n"
        "• ❌ Бизнесы без сайта\n"
        "• 📱 Ссылки только на соцсети (VK, Telegram, Taplink)\n"
        "• ⚠️ Мертвые и неработающие сайты\n\n"
        "Выберите действие в меню или отправьте CSV-файл для анализа:"
    )
    await message.answer(text, reply_markup=get_main_menu_keyboard(), parse_mode="HTML")


@router.message(F.text == "🔍 Поиск по СПб")
async def handle_search_menu(message: Message) -> None:
    """Prompt user to select an administrative district of Saint Petersburg."""
    await message.answer(
        "📍 <b>Выберите район Санкт-Петербурга:</b>",
        reply_markup=get_districts_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "dist_back")
async def handle_dist_back(callback: CallbackQuery) -> None:
    """Return to districts selection list."""
    await callback.answer()
    await _edit_or_send(
        callback,
        "📍 <b>Выберите район Санкт-Петербурга:</b>",
        reply_markup=get_districts_keyboard(),
    )


@router.callback_query(F.data.startswith("dist:"))
async def handle_district_chosen(callback: CallbackQuery) -> None:
    """Show business categories for the chosen district."""
    await callback.answer()
    dist_idx = int(callback.data.split(":")[1])
    district_name = DISTRICT_NAMES[dist_idx]

    await _edit_or_send(
        callback,
        f"📍 Район: <b>{district_name}</b>\n\n"
        "Выберите категорию бизнеса для сбора лидов:",
        reply_markup=get_categories_keyboard(dist_idx),
    )


@router.callback_query(F.data.startswith("cat:"))
async def handle_category_chosen(callback: CallbackQuery) -> None:
    """Trigger lead harvest for the selected district and category."""
    parts = callback.data.split(":")
    dist_idx = int(parts[1])
    cat_idx = int(parts[2])
    district_name = DISTRICT_NAMES[dist_idx]
    category_key = CATEGORY_KEYS[cat_idx]
    category_title = TARGET_CATEGORIES[category_key]["title"]

    await callback.answer()
    await _edit_or_send(
        callback,
        f"⏳ Сканирую район <b>{district_name}</b> ({category_title}) "
        "и проверяю сайты...\nЭто может занять 10-30 секунд.",
    )

    try:
        async with async_session_maker() as session:
            stats = await run_lead_harvest(
                session=session,
                district_name=district_name,
                category_key=category_key,
                twogis_api_key=settings.TWOGIS_API_KEY,
            )
        report = (
            "🏁 <b>Сбор завершен!</b>\n\n"
            f"📍 <b>Район:</b> {district_name}\n"
            f"🏷 <b>Ниша:</b> {category_title}\n\n"
            "📊 <b>Результаты:</b>\n"
            f"• Найдено: <b>{stats['total_found']}</b>\n"
            f"• Подходят: <b>{stats['qualified']}</b>\n"
            f"• Новых сохранено: <b>{stats['saved_new']}</b>\n"
            f"• Дубликатов пропущено: <b>{stats['duplicates_skipped']}</b>"
        )
    except Exception as exc:
        logger.exception("Error during harvest")
        report = f"⚠️ Ошибка при выполнении сбора: <code>{html.escape(str(exc))}</code>"

    await _edit_or_send(callback, report)


@router.message(F.document, F.document.file_name.lower().endswith(".csv"))
async def handle_csv_upload(message: Message, bot: Bot) -> None:
    """Download and process uploaded CSV file with raw places."""
    if not message.document:
        return

    status_msg = await message.answer("⏳ Загружаю и анализирую CSV файл...")
    try:
        file_io = io.BytesIO()
        await bot.download(message.document, destination=file_io)
        file_bytes = file_io.getvalue()

        async with async_session_maker() as session:
            stats = await import_csv_harvest(session, file_bytes)

        report = (
            "📁 <b>Импорт CSV завершен!</b>\n\n"
            f"• Найдено: <b>{stats['total_found']}</b>\n"
            f"• Подходят: <b>{stats['qualified']}</b>\n"
            f"• Новых сохранено: <b>{stats['saved_new']}</b>\n"
            f"• Дубликатов пропущено: <b>{stats['duplicates_skipped']}</b>"
        )
        await status_msg.edit_text(report, parse_mode="HTML")
    except Exception as exc:
        logger.exception("Error during CSV import")
        await status_msg.edit_text(
            f"⚠️ Ошибка при обработке CSV: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )
