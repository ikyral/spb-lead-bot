"""Telegram keyboard builders for reply and inline menus."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.config import settings
from src.database.models import CONTACT_CONTACTED, CONTACT_NO_ANSWER, CONTACT_REJECTED, CONTACT_REPLIED, Lead
from src.services.providers.base import SPB_DISTRICTS, TARGET_CATEGORIES
from src.services.twogis_links import twogis_card_url

DISTRICT_NAMES: list[str] = list(SPB_DISTRICTS.keys())
CATEGORY_KEYS: list[str] = list(TARGET_CATEGORIES.keys())

# Shown when callback data is malformed, forged, or from a keyboard of an older bot version.
STALE_BUTTON_TEXT: str = "Кнопка устарела. Откройте меню заново."
OPEN_CARD_BUTTON_TEXT: str = "🔎 Открыть в 2ГИС"
ADD_PHONE_BUTTON_TEXT: str = "✏️ Добавить телефон"

# Statuses an admin can put on a handed-out lead, in the order of the buttons.
LEAD_OUTCOMES: dict[str, str] = {
    CONTACT_CONTACTED: "Написал",
    CONTACT_REPLIED: "Ответили",
    CONTACT_REJECTED: "Отказ",
    CONTACT_NO_ANSWER: "Нет ответа",
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
    card_url: str | None = None,
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

    if card_url:
        builder.row(InlineKeyboardButton(text=OPEN_CARD_BUTTON_TEXT, url=card_url))

    # Adjust rows: 2 for actions, 1 for pitch, 2 (or 1) for nav
    nav_count = len(nav_buttons)
    if nav_count > 0:
        builder.adjust(2, 1, nav_count)
    else:
        builder.adjust(2, 1)

    return builder.as_markup()


def get_lead_status_keyboard(
    lead_id: int,
    current_status: str | None,
    card_url: str | None = None,
    can_add_phone: bool = False,
) -> InlineKeyboardMarkup:
    """Buttons of a handed-out lead: its card, statuses with the current one marked, adding a phone."""
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    if card_url:
        builder.button(text=OPEN_CARD_BUTTON_TEXT, url=card_url)
        rows.append(1)
    for status, title in LEAD_OUTCOMES.items():
        text = f"✅ {title}" if status == current_status else title
        builder.button(text=text, callback_data=f"ls:{lead_id}:{status}")
    rows += [2, 2]
    if can_add_phone:
        builder.button(text=ADD_PHONE_BUTTON_TEXT, callback_data=f"lp:{lead_id}")
        rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


def get_issued_lead_keyboard(lead: Lead) -> InlineKeyboardMarkup:
    """The buttons of a card of this lead: the same when it is sent and whenever it is updated."""
    return get_lead_status_keyboard(
        lead.id, lead.contact_status, card_url=twogis_card_url(lead.source_url), can_add_phone=not lead.phone
    )


def get_next_batch_keyboard() -> InlineKeyboardMarkup:
    """The 'next batch' button for groups, where the bottom keyboard does not suit (see README)."""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"📦 Следующие {settings.LEADS_BATCH_SIZE}", callback_data="panel:next")
    return builder.as_markup()


def get_group_panel_keyboard() -> InlineKeyboardMarkup:
    """Controls for groups. Inline buttons reach the bot even with privacy mode on."""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"📦 Следующие {settings.LEADS_BATCH_SIZE}", callback_data="panel:next")
    builder.button(text="📊 Статистика", callback_data="panel:stats")
    builder.button(text="🤖 Статус сбора", callback_data="panel:status")
    builder.button(text="▶️ Запустить сбор", callback_data="panel:collect")
    builder.adjust(1, 2, 1)
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
