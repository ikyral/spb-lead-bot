"""Handing out leads in batches: 'next batch' button, status buttons and the funnel statistics."""

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message

from src.bot.chats import is_group_chat
from src.bot.formatters import format_funnel_stats, format_issued_lead, format_msk_time, format_user_label
from src.bot.keyboards import (
    LEAD_OUTCOMES,
    STALE_BUTTON_TEXT,
    get_full_export_keyboard,
    get_lead_status_keyboard,
    get_next_batch_keyboard,
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
# Telegram asks not to send more than one message per second to a chat...
MESSAGE_PAUSE_SECONDS: float = 1.0
# ...and no more than 20 messages per minute to a group.
GROUP_MESSAGE_PAUSE_SECONDS: float = 3.1
MAX_SEND_ATTEMPTS: int = 3
# Chats receiving a batch right now: a double tap must not hand out a second batch, and two
# batches at once would break the limit of a group. Leads never overlap in any case: see issue_next_leads.
_issuing_to: set[int] = set()


async def _answer_safely(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """A closing message must not fail the handler: the leads are already sorted out by now."""
    try:
        await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramMigrateToChat as exc:
        logger.warning(
            "Chat %s has become supergroup %s: the batch stops here", message.chat.id, exc.migrate_to_chat_id
        )
        if message.bot is not None:
            try:
                await message.bot.send_message(
                    exc.migrate_to_chat_id, f"{text}\n\nГруппа стала супергруппой, нажмите кнопку ещё раз.",
                    parse_mode="HTML",
                )
            except TelegramAPIError as send_error:
                logger.warning("Could not write to the new supergroup: %s", send_error)
    except TelegramAPIError as exc:
        logger.warning("Could not send a message to chat %s: %s", message.chat.id, exc)


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

    in_group = is_group_chat(message)
    pause = GROUP_MESSAGE_PAUSE_SECONDS if in_group else MESSAGE_PAUSE_SECONDS
    undelivered: list[int] = [lead.id for lead in leads]
    try:
        if in_group and len(leads) > 1:
            minutes = max(1, round(len(leads) * pause / 60))
            try:
                await message.answer(f"⏳ Выдаю лидов: {len(leads)}. В группе это займёт около {minutes} мин.")
            except TelegramRetryAfter as exc:
                # Only a courtesy: the cards below wait for Telegram by themselves.
                logger.info("Skipped the batch notice: Telegram asks to wait %s s", exc.retry_after)
        for index, lead in enumerate(leads):
            if index:
                await asyncio.sleep(pause)
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
    await _answer_safely(message, "\n".join(lines), get_next_batch_keyboard() if in_group else None)


@router.message(F.text.regexp(NEXT_BATCH_BUTTON_RE))
@router.message(Command("next_leads"))
async def handle_next_batch(message: Message, collector_runner: CollectorRunner) -> None:
    """Hand out the next batch of leads."""
    chat_id = message.chat.id
    if chat_id in _issuing_to:
        await _answer_safely(message, "⏳ Предыдущая пачка ещё отправляется.")
        return
    _issuing_to.add(chat_id)
    try:
        await _issue_batch(message, collector_runner)
    finally:
        _issuing_to.discard(chat_id)


@router.callback_query(F.data == "panel:next")
async def handle_next_batch_button(callback: CallbackQuery, collector_runner: CollectorRunner) -> None:
    """The same from an inline button: it reaches the bot even with privacy mode on."""
    if not isinstance(callback.message, Message):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    if callback.message.chat.id in _issuing_to:
        await callback.answer("Предыдущая пачка ещё отправляется")
        return
    await callback.answer()
    await handle_next_batch(callback.message, collector_runner)


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
        user = callback.from_user
        changed_by = format_user_label(user.username, user.full_name)
        await set_contact_status(session, lead_id, status, user.id, changed_by)
        lead.contact_status, lead.contact_status_by = status, changed_by

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


@router.callback_query(F.data == "panel:stats")
async def handle_stats_button(callback: CallbackQuery) -> None:
    """The same as /leads_stats from an inline button."""
    if not isinstance(callback.message, Message):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    await callback.answer()
    await handle_leads_stats(callback.message)


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
