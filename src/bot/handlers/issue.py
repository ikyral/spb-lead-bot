"""Handing out leads in batches: 'next batch' button, status buttons and the funnel statistics."""

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from src.bot.formatters import format_funnel_stats, format_issued_lead, format_msk_time
from src.bot.keyboards import (
    LEAD_OUTCOMES,
    STALE_BUTTON_TEXT,
    get_full_export_keyboard,
    get_lead_status_keyboard,
)
from src.config import settings
from src.database.models import Lead
from src.database.repo import (
    async_session_maker,
    count_issued_leads,
    count_new_leads,
    get_contact_status_counts,
    get_status_counts,
    issue_next_leads,
    return_undelivered_leads,
    set_contact_status,
)
from src.services.scheduler import TRIGGER_BATCH, CollectorRunner, TopupDecision, TopupOutcome

logger = logging.getLogger(__name__)
router = Router(name="issue")

# The bottom button shows the batch size; any size matches, so an old keyboard keeps working.
NEXT_BATCH_BUTTON_RE = r"^📦 Следующие \d+$"
# Telegram asks not to send more than one message per second to a chat.
MESSAGE_PAUSE_SECONDS: float = 1.0
MAX_SEND_ATTEMPTS: int = 3
# Who is receiving a batch right now: a double tap must not hand out a second batch.
_issuing_to: set[int] = set()


async def _send_card(message: Message, lead: Lead) -> None:
    """Send one lead with its status buttons, waiting as long as Telegram asks to."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            await message.answer(
                format_issued_lead(lead),
                reply_markup=get_lead_status_keyboard(lead.id, lead.contact_status),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return
        except TelegramRetryAfter as exc:
            if attempt == MAX_SEND_ATTEMPTS:
                raise
            logger.warning("Telegram asked to wait %s s while sending leads", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)


def _describe_topup(decision: TopupDecision) -> str:
    """Tell the admin what happens with the pool after a short batch."""
    if decision.outcome is TopupOutcome.STARTED:
        return "🔄 Запустил сбор в фоне, итог пришлю."
    if decision.outcome is TopupOutcome.RUNNING:
        return "🔄 Сбор уже идёт, итог пришлю."
    if decision.outcome is TopupOutcome.COOLDOWN and decision.retry_at is not None:
        return f"⏳ Внеочередной сбор возможен не чаще раза в час, следующий: {format_msk_time(decision.retry_at)}."
    if decision.outcome is TopupOutcome.DAILY_LIMIT:
        return "🛑 Суточный лимит запросов исчерпан, сбор продолжится завтра."
    return "⏸ Автосбор выключен. Запустить вручную: /collect_now"


async def _issue_batch(message: Message, collector_runner: CollectorRunner) -> None:
    size = settings.LEADS_BATCH_SIZE
    async with async_session_maker() as session:
        leads = await issue_next_leads(session, size)

    undelivered: list[int] = [lead.id for lead in leads]
    try:
        for index, lead in enumerate(leads):
            if index:
                await asyncio.sleep(MESSAGE_PAUSE_SECONDS)
            try:
                await _send_card(message, lead)
            except TelegramBadRequest:
                # A card Telegram refuses must not block the leads behind it.
                logger.exception("Telegram rejected the card of lead %s", lead.id)
                continue
            undelivered.remove(lead.id)
    except TelegramAPIError:
        logger.exception("Sending the batch stopped, %d leads go back to the pool", len(undelivered))
    finally:
        # Also on cancellation: a lead the admin has not received is not issued.
        if undelivered:
            async with async_session_maker() as session:
                await return_undelivered_leads(session, undelivered)

    async with async_session_maker() as session:
        pool_size = await count_new_leads(session)

    delivered = len(leads) - len(undelivered)
    lines: list[str] = []
    if delivered:
        lines.append(f"✅ Выдано лидов: <b>{delivered}</b>. В запасе: <b>{pool_size}</b>.")
    if undelivered:
        lines.append(f"⚠️ Не удалось отправить: <b>{len(undelivered)}</b>, они остались в запасе.")
    if len(leads) < size:
        if not leads:
            lines.append("📭 Новых лидов в запасе нет.")
        else:
            lines.append(f"В запасе было меньше {size}, выдал сколько есть.")
        lines.append(_describe_topup(await collector_runner.request_topup(TRIGGER_BATCH)))
    else:
        await collector_runner.check_pool()
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text.regexp(NEXT_BATCH_BUTTON_RE))
@router.message(Command("next_leads"))
async def handle_next_batch(message: Message, collector_runner: CollectorRunner) -> None:
    """Hand out the next batch of leads."""
    user_id = message.from_user.id if message.from_user else message.chat.id
    if user_id in _issuing_to:
        await message.answer("⏳ Предыдущая пачка ещё отправляется.")
        return
    _issuing_to.add(user_id)
    try:
        await _issue_batch(message, collector_runner)
    finally:
        _issuing_to.discard(user_id)


@router.callback_query(F.data.startswith("ls:"))
async def handle_lead_status(callback: CallbackQuery) -> None:
    """Save the status chosen on a handed-out lead and show it on the card."""
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in LEAD_OUTCOMES:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    lead_id, status = int(parts[1]), parts[2]

    async with async_session_maker() as session:
        lead = await session.get(Lead, lead_id)
        # Status buttons exist only on leads that were handed out.
        if lead is None or lead.issued_at is None:
            await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
            return
        if lead.contact_status == status:
            await callback.answer("Этот статус уже стоит")
            return
        await set_contact_status(session, lead_id, status)
        lead.contact_status = status

    await callback.answer(f"Статус: {LEAD_OUTCOMES[status]}")
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                format_issued_lead(lead),
                reply_markup=get_lead_status_keyboard(lead.id, status),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            # The status is saved; only the card on the screen is out of date.
            logger.warning("Could not update the card of lead %s: %s", lead_id, exc)


@router.message(Command("leads_stats"))
async def handle_leads_stats(message: Message) -> None:
    """Show how many leads are in the pool and what happened to the handed-out ones."""
    async with async_session_maker() as session:
        counts = await get_status_counts(session)
        contact_counts = await get_contact_status_counts(session)
        issued_total = await count_issued_leads(session)
    await message.answer(
        format_funnel_stats(counts, contact_counts, issued_total),
        reply_markup=get_full_export_keyboard(),
        parse_mode="HTML",
    )
