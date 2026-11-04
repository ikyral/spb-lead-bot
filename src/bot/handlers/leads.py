"""Leads browsing, CRM status management, and CSV export handlers."""

import csv
import html
import io
import logging
import re
from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from src.bot.formatters import format_lead_card, generate_pitch_text
from src.bot.keyboards import STALE_BUTTON_TEXT, get_export_keyboard, get_lead_card_keyboard
from src.database.models import Lead
from src.database.repo import (
    async_session_maker,
    export_leads_by_status,
    get_leads,
    get_leads_stats,
    update_crm_status,
)

logger = logging.getLogger(__name__)
router = Router(name="leads")

LEAD_ACTIONS: frozenset[str] = frozenset({"work", "reject", "pitch", "prev", "next"})
# Statuses accepted by export_leads_by_status ('all' exports everything).
EXPORT_STATUSES: frozenset[str] = frozenset({"new", "in_work", "rejected", "all"})

# Spreadsheet apps treat cells starting with these characters as formulas (CSV injection).
_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
# Phone-like values ("+7 (812) 555-01-01, +7 ...") contain no function names and are left as is.
_PHONE_LIKE_RE = re.compile(r"[+\-][\d\s().,+\-]*")


def _csv_safe(value: object) -> object:
    """Neutralize spreadsheet formula injection in exported cells."""
    if not isinstance(value, str) or not value.startswith(_FORMULA_PREFIXES):
        return value
    if _PHONE_LIKE_RE.fullmatch(value):
        return value
    return "'" + value


async def _reply(target: Message | CallbackQuery, text: str, kb=None, edit: bool = False) -> None:
    """Helper to reply to message or edit callback message."""
    # Messages older than 48h arrive as InaccessibleMessage (no edit_text): send a new one instead.
    if edit and isinstance(target, CallbackQuery) and isinstance(target.message, Message):
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    elif isinstance(target, Message):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    elif isinstance(target, CallbackQuery) and target.message:
        await target.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def render_lead_page(
    target: Message | CallbackQuery,
    session: AsyncSession,
    crm_status: str,
    offset: int,
    edit: bool = False,
) -> None:
    """Render a single lead card with pagination controls."""
    leads = await get_leads(session, crm_status=crm_status, limit=1, offset=offset)
    if not leads:
        msg = f"📋 Лидов со статусом «{crm_status}» пока нет." if offset == 0 else "🏁 Больше нет лидов в списке."
        await _reply(target, msg, edit=edit)
        return

    lead = leads[0]
    next_leads = await get_leads(session, crm_status=crm_status, limit=1, offset=offset + 1)
    status_title = "📋 Новый лид" if crm_status == "new" else "💼 В работе"
    card_text = f"<b>{status_title} #{offset + 1}</b>\n\n" + format_lead_card(lead)
    kb = get_lead_card_keyboard(
        lead_id=lead.id,
        offset=offset,
        map_url=lead.map_url,
        has_prev=offset > 0,
        has_next=len(next_leads) > 0,
    )
    await _reply(target, card_text, kb=kb, edit=edit)


@router.message(F.text == "📋 Новые лиды")
async def handle_new_leads(message: Message) -> None:
    """Show the first new lead card."""
    async with async_session_maker() as session:
        await render_lead_page(message, session, crm_status="new", offset=0)


@router.message(F.text == "💼 В работе")
async def handle_in_work_leads(message: Message) -> None:
    """Show the first lead currently in work."""
    async with async_session_maker() as session:
        await render_lead_page(message, session, crm_status="in_work", offset=0)


@router.callback_query(F.data.startswith("lead:"))
async def handle_lead_action(callback: CallbackQuery) -> None:
    """Handle card actions: work, reject, pitch, prev, next."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 4
        or not parts[1].isdigit()
        or parts[2] not in LEAD_ACTIONS
        or not parts[3].isdigit()
    ):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    _, lead_id_str, action, offset_str = parts
    lead_id, offset = int(lead_id_str), int(offset_str)

    async with async_session_maker() as session:
        lead = await session.get(Lead, lead_id)
        if not lead:
            await callback.answer("Лид не найден в базе", show_alert=True)
            return

        current_status = lead.crm_status

        if action == "pitch":
            pitch = generate_pitch_text(lead)
            await callback.answer()
            if callback.message:
                await callback.message.answer(
                    f"💡 <b>Текст КП для «{html.escape(lead.name)}»:</b>\n\n<code>{html.escape(pitch)}</code>",
                    parse_mode="HTML",
                )
            return

        if action == "work":
            await update_crm_status(session, lead_id, "in_work")
            await callback.answer("Лид переведен в статус «В работе»!")
            pitch = generate_pitch_text(lead)
            if callback.message:
                await callback.message.answer(
                    f"💼 <b>Лид «{html.escape(lead.name)}» взят в работу!</b>\n\n"
                    f"💡 <b>Текст КП:</b>\n\n<code>{html.escape(pitch)}</code>",
                    parse_mode="HTML",
                )
            # Stay in the list the card was opened from ("В работе" cards keep their position).
            list_status = "in_work" if current_status == "in_work" else "new"
            await render_lead_page(callback, session, crm_status=list_status, offset=offset, edit=True)
            return

        if action == "reject":
            await update_crm_status(session, lead_id, "rejected")
            await callback.answer("Лид перенесен в отказ")
            await render_lead_page(callback, session, crm_status=current_status, offset=offset, edit=True)
            return

        if action in ("prev", "next"):
            await callback.answer()
            await render_lead_page(callback, session, crm_status=current_status, offset=offset, edit=True)


@router.message(F.text == "📊 Статистика и CSV")
async def handle_stats(message: Message) -> None:
    """Show CRM statistics and export buttons."""
    async with async_session_maker() as session:
        stats = await get_leads_stats(session)
    total = sum(stats.values())
    text = (
        "📊 <b>Статистика базы лидов:</b>\n\n"
        f"• 📋 Новые: <b>{stats['new']}</b>\n"
        f"• 💼 В работе: <b>{stats['in_work']}</b>\n"
        f"• 🗑 В отказе: <b>{stats['rejected']}</b>\n"
        f"• 📈 Всего в базе: <b>{total}</b>\n\n"
        "Выберите категорию для выгрузки в CSV:"
    )
    await message.answer(text, reply_markup=get_export_keyboard(), parse_mode="HTML")


@router.callback_query(F.data.startswith("export:"))
async def handle_export_csv(callback: CallbackQuery) -> None:
    """Export selected leads to Excel-friendly CSV file."""
    crm_status = (callback.data or "").removeprefix("export:")
    if crm_status not in EXPORT_STATUSES:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    async with async_session_maker() as session:
        leads = await export_leads_by_status(session, crm_status)

    if not leads:
        await callback.answer(f"Нет лидов со статусом «{crm_status}»", show_alert=True)
        return

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "ID", "Источник", "Название", "Категория", "Район", "Адрес", "Телефон",
        "Соцсети", "Сайт", "Статус сайта", "Оценка", "CRM статус", "Карта", "Дата добавления"
    ])
    for lead in leads:
        writer.writerow([_csv_safe(value) for value in (
            lead.id, lead.source, lead.name, lead.category, lead.district or "", lead.address or "",
            lead.phone or "", lead.socials or "", lead.raw_website or "", lead.site_status,
            lead.score, lead.crm_status, lead.map_url or "",
            lead.created_at.isoformat() if lead.created_at else "",
        )])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    file = BufferedInputFile(csv_bytes, filename=f"spb_leads_{crm_status}.csv")
    if callback.message:
        await callback.message.answer_document(
            document=file,
            caption=f"📥 Выгрузка лидов: <b>{crm_status}</b> ({len(leads)} шт.)",
            parse_mode="HTML",
        )
    await callback.answer()
