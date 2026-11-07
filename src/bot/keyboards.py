"""Telegram keyboard builders for reply and inline menus."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.texts import (
    BATCH_BUTTON_TEXT,
    CRM_STATS_BUTTON_TEXT,
    DIGEST_DONE_BUTTON_TEXT,
    DIGEST_LATER_BUTTON_TEXT,
    DIGEST_REJECT_BUTTON_TEXT,
    ENRICH_BUTTON_TEXT,
    HELP_BUTTON_TEXT,
    IN_WORK_BUTTON_TEXT,
    MORE_ENRICH_BUTTON_TEXT,
    NEW_LEADS_BUTTON_TEXT,
    REMIND_BUTTON_TEXT,
    REMIND_DAYS,
    REQUEST_FILE_BUTTON_TEXT,
    SEARCH_MENU_BUTTON_TEXT,
    STATS_BUTTON_TEXT,
)
from src.config import settings
from src.database.models import CONTACT_CONTACTED, CONTACT_NO_ANSWER, CONTACT_REJECTED, CONTACT_REPLIED, Lead
from src.services.providers.base import SPB_DISTRICTS, TARGET_CATEGORIES
from src.services.site_status import (
    NO_SITE_STATUSES,
    SITE_DEAD,
    SITE_FOUND,
    SITE_NO_SITE,
    SOURCE_MANUAL,
    yandex_search_url,
)
from src.services.twogis_links import twogis_card_url, twogis_search_url

DISTRICT_NAMES: list[str] = list(SPB_DISTRICTS.keys())
CATEGORY_KEYS: list[str] = list(TARGET_CATEGORIES.keys())

ENRICH_REQUEST_CALLBACK: str = "enrich:request"
# Shown when callback data is malformed, forged, or from a keyboard of an older bot version.
STALE_BUTTON_TEXT: str = "Кнопка устарела. Откройте меню заново."
OPEN_CARD_BUTTON_TEXT: str = "🔎 Открыть в 2ГИС"
FIND_IN_2GIS_BUTTON_TEXT: str = "🔎 Найти в 2ГИС"
ADD_PHONE_BUTTON_TEXT: str = "✏️ Добавить телефон"
SITE_YES_BUTTON_TEXT: str = "🌐 Сайт есть"
SITE_NO_BUTTON_TEXT: str = "✅ Сайта нет"
SEARCH_BUTTON_TEXT: str = "🔎 Проверить в Яндексе"

# Statuses an admin can put on a handed-out lead, in the order of the buttons.
LEAD_OUTCOMES: dict[str, str] = {
    CONTACT_CONTACTED: "Написал",
    CONTACT_REPLIED: "Ответили",
    CONTACT_REJECTED: "Отказ",
    CONTACT_NO_ANSWER: "Нет ответа",
}


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Return the persistent bottom reply keyboard, for private chats only: the main buttons, then the older menu."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BATCH_BUTTON_TEXT), KeyboardButton(text=ENRICH_BUTTON_TEXT)],
            [KeyboardButton(text=STATS_BUTTON_TEXT), KeyboardButton(text=HELP_BUTTON_TEXT)],
            [KeyboardButton(text=SEARCH_MENU_BUTTON_TEXT), KeyboardButton(text=NEW_LEADS_BUTTON_TEXT)],
            [KeyboardButton(text=IN_WORK_BUTTON_TEXT), KeyboardButton(text=CRM_STATS_BUTTON_TEXT)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_enrich_request_keyboard() -> InlineKeyboardMarkup:
    """Under the instruction on enrichment: the same as /export_unknown 30."""
    builder = InlineKeyboardBuilder()
    builder.button(text=REQUEST_FILE_BUTTON_TEXT, callback_data=ENRICH_REQUEST_CALLBACK)
    return builder.as_markup()


def get_after_enrichment_keyboard() -> InlineKeyboardMarkup:
    """Under the result of an enrichment: look at the leads, or take the next file to check."""
    builder = InlineKeyboardBuilder()
    builder.button(text=BATCH_BUTTON_TEXT, callback_data="panel:next")
    builder.button(text=MORE_ENRICH_BUTTON_TEXT, callback_data=ENRICH_REQUEST_CALLBACK)
    builder.adjust(1)
    return builder.as_markup()


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
    search_url: str | None = None,
    site_verdict: bool | None = None,
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

    if search_url:
        yes_text, no_text = _site_verdict_titles(site_verdict)
        builder.button(text=yes_text, callback_data=f"lead:{lead_id}:site_yes:{offset}")
        builder.button(text=no_text, callback_data=f"lead:{lead_id}:site_no:{offset}")
        builder.button(text=SEARCH_BUTTON_TEXT, url=search_url)

    # Rows: 2 for actions, 1 for pitch, then nav, the links and the verdict about the site.
    rows = [2, 1]
    if nav_buttons:
        rows.append(len(nav_buttons))
    has_map = bool(map_url and map_url.startswith(("http://", "https://")))
    rows += [1] * (int(has_map) + int(bool(card_url)))
    if search_url:
        rows += [2, 1]
    builder.adjust(*rows)

    return builder.as_markup()


def _site_verdict_titles(site_verdict: bool | None) -> tuple[str, str]:
    """The buttons of a verdict, the one already given marked."""
    return (
        f"☑️ {SITE_YES_BUTTON_TEXT}" if site_verdict is True else SITE_YES_BUTTON_TEXT,
        f"☑️ {SITE_NO_BUTTON_TEXT}" if site_verdict is False else SITE_NO_BUTTON_TEXT,
    )


def manual_site_verdict(lead: Lead) -> bool | None:
    """What a person has said about the site of this lead: True for a site, False for none, None if nothing."""
    if lead.site_source != SOURCE_MANUAL:
        return None
    return {SITE_FOUND: True, SITE_NO_SITE: False}.get(lead.site_status)


def get_lead_status_keyboard(
    lead_id: int,
    current_status: str | None,
    card_url: str | None = None,
    can_add_phone: bool = False,
    search_url: str | None = None,
    site_verdict: bool | None = None,
    twogis_search: str | None = None,
) -> InlineKeyboardMarkup:
    """
    Buttons of a handed-out lead: its card or a search, statuses with the current one marked, its site, a phone.

    After 'Написал' a row of reminders: in 1, 3 or 7 days.
    """
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    if card_url:
        builder.button(text=OPEN_CARD_BUTTON_TEXT, url=card_url)
        rows.append(1)
    elif twogis_search:
        builder.button(text=FIND_IN_2GIS_BUTTON_TEXT, url=twogis_search)
        rows.append(1)
    for status, title in LEAD_OUTCOMES.items():
        text = f"✅ {title}" if status == current_status else title
        builder.button(text=text, callback_data=f"ls:{lead_id}:{status}")
    rows += [2, 2]
    if current_status == CONTACT_CONTACTED:
        for days in REMIND_DAYS:
            builder.button(text=REMIND_BUTTON_TEXT.format(days=days), callback_data=f"rm:{lead_id}:{days}")
        rows.append(len(REMIND_DAYS))
    if search_url:
        yes_text, no_text = _site_verdict_titles(site_verdict)
        builder.button(text=yes_text, callback_data=f"sv:{lead_id}:yes")
        builder.button(text=no_text, callback_data=f"sv:{lead_id}:no")
        builder.button(text=SEARCH_BUTTON_TEXT, url=search_url)
        rows += [2, 1]
    if can_add_phone:
        builder.button(text=ADD_PHONE_BUTTON_TEXT, callback_data=f"lp:{lead_id}")
        rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


def get_issued_lead_keyboard(lead: Lead) -> InlineKeyboardMarkup:
    """The buttons of a card of this lead: the same when it is sent and whenever it is updated."""
    # A lead that needs a site must be easy to look at, even if it came from a source without a card on 2GIS.
    needs_a_look = lead.site_status in (*NO_SITE_STATUSES, SITE_DEAD)
    return get_lead_status_keyboard(
        lead.id,
        lead.contact_status,
        card_url=twogis_card_url(lead.source_url),
        can_add_phone=not lead.phone,
        search_url=yandex_search_url(lead.name, lead.address),
        site_verdict=manual_site_verdict(lead),
        twogis_search=twogis_search_url(lead.name, lead.address, settings.SCRAPER_CITY_SLUG) if needs_a_look else None,
    )


def get_followup_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    """Under a lead of the morning plan: reminded, refused, or later."""
    builder = InlineKeyboardBuilder()
    builder.button(text=DIGEST_DONE_BUTTON_TEXT, callback_data=f"dg:{lead_id}:done")
    builder.button(text=DIGEST_REJECT_BUTTON_TEXT, callback_data=f"dg:{lead_id}:reject")
    builder.button(text=DIGEST_LATER_BUTTON_TEXT, callback_data=f"dg:{lead_id}:later")
    builder.adjust(2, 1)
    return builder.as_markup()


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
