"""Telegram keyboard builders for reply and inline menus."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.config import settings
from src.database.models import STATUS_CONTACTED, STATUS_NO_ANSWER, STATUS_REJECTED, STATUS_REPLIED
from src.services.providers.base import SPB_DISTRICTS, TARGET_CATEGORIES

DISTRICT_NAMES: list[str] = list(SPB_DISTRICTS.keys())
CATEGORY_KEYS: list[str] = list(TARGET_CATEGORIES.keys())

# Shown when callback data is malformed, forged, or from a keyboard of an older bot version.
STALE_BUTTON_TEXT: str = "Кнопка устарела. Откройте меню заново."

# Statuses an admin can put on a handed-out lead, in the order of the buttons.
LEAD_OUTCOMES: dict[str, str] = {
    STATUS_CONTACTED: "Написал",
    STATUS_REPLIED: "Ответили",
    STATUS_REJECTED: "Отказ",
    STATUS_NO_ANSWER: "Нет ответа",
}


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Return the persistent bottom reply keyboard."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔍 Поиск по СПб"),
                KeyboardButton(text="📋 Новые лиды"),
            ],
            [
                KeyboardButton(text="💼 В работе"),
                KeyboardButton(text="📊 Статистика и CSV"),
            ],
            [KeyboardButton(text=f"📦 Следующие {settings.LEADS_BATCH_SIZE}")],
        ],
        resize_keyboard=True,
    )


def get_districts_keyboard() -> InlineKeyboardMarkup:
    """Generate inline keyboard for SPb districts selection."""
    builder = InlineKeyboardBuilder()
    # Stable keys (not list positions): reordering districts cannot retarget old buttons.
    for name in DISTRICT_NAMES:
        builder.button(text=name, callback_data=f"dist:{name}")
    builder.adjust(2)
    return builder.as_markup()


def get_categories_keyboard(district_name: str) -> InlineKeyboardMarkup:
    """Generate inline keyboard for business categories in selected district."""
    builder = InlineKeyboardBuilder()
    for cat_key in CATEGORY_KEYS:
        title = TARGET_CATEGORIES[cat_key]["title"]
        builder.button(text=title, callback_data=f"cat:{district_name}:{cat_key}")
    builder.button(text="⬅️ Назад к районам", callback_data="dist_back")
    builder.adjust(1)
    return builder.as_markup()


def get_lead_card_keyboard(
    lead_id: int,
    offset: int = 0,
    map_url: str | None = None,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    """Generate inline control buttons for lead card with pagination."""
    builder = InlineKeyboardBuilder()

    # Action buttons
    builder.button(text="✅ В работу", callback_data=f"lead:{lead_id}:work:{offset}")
    builder.button(text="🗑 В отказ", callback_data=f"lead:{lead_id}:reject:{offset}")
    builder.button(text="💡 Текст КП", callback_data=f"lead:{lead_id}:pitch:{offset}")

    # Pagination buttons
    nav_buttons: list[InlineKeyboardButton] = []
    if has_prev:
        prev_offset = max(0, offset - 1)
        nav_buttons.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"lead:{lead_id}:prev:{prev_offset}",
            )
        )
    if has_next:
        next_offset = offset + 1
        nav_buttons.append(
            InlineKeyboardButton(
                text="➡️ Далее",
                callback_data=f"lead:{lead_id}:next:{next_offset}",
            )
        )

    for btn in nav_buttons:
        builder.add(btn)

    # External map URL link button
    if map_url and map_url.startswith(("http://", "https://")):
        builder.row(InlineKeyboardButton(text="🗺 Открыть на карте", url=map_url))

    # Adjust rows: 2 for actions, 1 for pitch, 2 (or 1) for nav
    nav_count = len(nav_buttons)
    if nav_count > 0:
        builder.adjust(2, 1, nav_count)
    else:
        builder.adjust(2, 1)

    return builder.as_markup()


def get_lead_status_keyboard(lead_id: int, current_status: str) -> InlineKeyboardMarkup:
    """Status buttons of a handed-out lead; the current status is marked."""
    builder = InlineKeyboardBuilder()
    for status, title in LEAD_OUTCOMES.items():
        text = f"✅ {title}" if status == current_status else title
        builder.button(text=text, callback_data=f"ls:{lead_id}:{status}")
    builder.adjust(2)
    return builder.as_markup()


def get_full_export_keyboard() -> InlineKeyboardMarkup:
    """Export of all leads, including the handed-out ones."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 Выгрузить все лиды (CSV)", callback_data="export:all")
    return builder.as_markup()


def get_export_keyboard() -> InlineKeyboardMarkup:
    """Generate inline keyboard for CSV export options."""
    builder = InlineKeyboardBuilder()
    builder.button(text='📥 Выгрузить "Новые" (CSV)', callback_data="export:new")
    builder.button(text='📥 Выгрузить "В работе" (CSV)', callback_data="export:in_work")
    builder.adjust(1)
    return builder.as_markup()
