"""Handing out leads in batches: 'next batch' button, status buttons and the funnel statistics."""

import asyncio
import html
import logging
import time
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardMarkup, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from src.bot.chats import is_group_chat
from src.bot.formatters import (
    format_funnel_stats,
    format_issued_lead,
    format_msk_time,
    format_request_usage,
    format_returned_lead,
    format_user_label,
    owned_by_text,
    site_verdict_text,
)
from src.bot.keyboards import (
    BATCH_NOOP_CALLBACK,
    LEAD_OUTCOMES,
    STALE_BUTTON_TEXT,
    BatchPosition,
    get_batch_nav_keyboard,
    get_full_export_keyboard,
    get_issued_lead_keyboard,
    get_next_batch_keyboard,
    parse_batch_token,
)
from src.bot.texts import (
    BATCH_BUSY_TEXT,
    BATCH_BUSY_TOAST,
    BATCH_STARTED_TOAST,
    NOT_YOUR_LEAD_TEXT,
    RESERVED_BY_OTHERS_LINE,
    RETURN_NOT_ALLOWED_TEXT,
    RETURNED_TOAST,
)
from src.config import settings
from src.database.models import Lead
from src.database.repo import (
    OUTCOME_MISSING,
    OUTCOME_OK,
    OUTCOME_OWNED,
    OUTCOME_SAME,
    IssueRules,
    async_session_maker,
    change_contact_status,
    count_issued_leads,
    count_new_leads,
    count_reserved_for_others,
    count_unknown_site_leads,
    create_issued_batch,
    delete_issued_batch,
    get_contact_status_counts,
    get_issued_batch,
    get_pool_stats,
    get_status_counts,
    issue_next_leads,
    release_lead,
    remove_lead_from_batch,
    return_undelivered_leads,
    set_lead_phone,
    set_site_verdict,
    update_issued_batch,
)
from src.services.chains import rescore_new_leads
from src.services.normalize import parse_manual_phone
from src.services.scheduler import TRIGGER_BATCH, CollectorRunner, TopupDecision, TopupOutcome

logger = logging.getLogger(__name__)
router = Router(name="issue")

# The bottom button shows the batch size; any size matches, so an old keyboard keeps working.
NEXT_BATCH_BUTTON_RE = r"^📦 Следующие \d+$"
MAX_SEND_ATTEMPTS: int = 3
# Chats receiving a batch right now: a double tap must not hand out a second batch.
# Leads never overlap in any case: see issue_next_leads.
_issuing_to: set[int] = set()
# People receiving a batch right now: one person in two chats must not run two batches at once either.
_issuing_users: set[int] = set()
# Batches being sent in the background; the set keeps a strong reference to each task.
_background_batches: set[asyncio.Task[None]] = set()
# Starts the question 'which phone?': a reply to such a message is the answer to it.
PHONE_PROMPT_PREFIX: str = "✏️ Телефон для лида"
_PHONE_PROMPT_TTL_SECONDS: float = 3600.0
_MAX_PHONE_PROMPTS: int = 200


@dataclass(frozen=True)
class _PhonePrompt:
    """A question waiting for a reply: which lead it is about and which message shows the lead."""

    lead_id: int
    card_message_id: int
    asked_at: float
    batch_id: int | None = None


# By (chat, message with the question). A restart loses them; the admin is then asked to press the button again.
_phone_prompts: dict[tuple[int, int], _PhonePrompt] = {}


def _is_uint(value: str) -> bool:
    return value.isascii() and value.isdigit()


def _parse_optional_batch(parts: list[str], base_len: int) -> tuple[bool, int | None]:
    """Check whether `parts` has `base_len` items or one trailing 'b<batch_id>' token."""
    if len(parts) == base_len:
        return True, None
    if len(parts) == base_len + 1:
        batch_id = parse_batch_token(parts[base_len])
        return batch_id is not None, batch_id
    return False, None


async def load_batch_position(session: AsyncSession, batch_id: int | None, lead_id: int) -> BatchPosition | None:
    """Find where `lead_id` sits in `batch_id`, or None for a standalone/missing batch."""
    if batch_id is None:
        return None
    lead_ids = await get_issued_batch(session, batch_id)
    if not lead_ids or lead_id not in lead_ids:
        return None
    return BatchPosition(batch_id=batch_id, index=lead_ids.index(lead_id), total=len(lead_ids))


def render_issued_card(
    lead: Lead | None, batch: BatchPosition | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render an issued lead (or its returned/missing placeholder) with optional batch navigation."""
    position = (batch.index + 1, batch.total) if batch is not None else None
    if lead is None:
        header = f"📦 <b>Лид {position[0]} из {position[1]}</b>\n\n" if position and position[1] > 1 else ""
        return f"{header}⚠️ Лид не найден.", get_batch_nav_keyboard(batch) if batch is not None else None
    if lead.issued_at is None:
        return format_returned_lead(lead.name, position), get_batch_nav_keyboard(batch) if batch is not None else None
    return format_issued_lead(lead, position=position), get_issued_lead_keyboard(lead, batch=batch)


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
                    exc.migrate_to_chat_id,
                    f"{text}\n\nГруппа стала супергруппой, нажмите кнопку ещё раз.",
                    parse_mode="HTML",
                )
            except TelegramAPIError as send_error:
                logger.warning("Could not write to the new supergroup: %s", send_error)
    except TelegramAPIError as exc:
        logger.warning("Could not send a message to chat %s: %s", message.chat.id, exc)


async def _send_card(message: Message, lead: Lead, batch: BatchPosition | None = None) -> None:
    """Send one lead with its status buttons, waiting as long as Telegram asks to."""
    text, markup = render_issued_card(lead, batch)
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            await message.answer(
                text,
                reply_markup=markup,
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
    if decision.outcome is TopupOutcome.EXTERNAL:
        return "📥 Лиды приходят с внешнего сборщика: нужен новый файл импорта."
    if decision.outcome is TopupOutcome.DAILY_LIMIT:
        return "🛑 Суточный лимит запросов исчерпан, сбор продолжится завтра."
    return "⏸ Автосбор выключен. Запустить вручную: /collect_now"


def _describe_held_back(held_back: int, unknown: int, rules: IssueRules, size: int) -> list[str]:
    """Why leads still in the pool did not go into a short batch."""
    lines: list[str] = []
    waiting = 0 if rules.issue_unknown else unknown
    if waiting:
        lines.append(f"⏳ <b>{waiting}</b> ждут проверки сайта: сейчас такие лиды не выдаются (LEADS_ISSUE_UNKNOWN).")
    if held_back > waiting:
        lines.append(
            f"⚖️ Ещё <b>{held_back - waiting}</b> не вошли в пачку: "
            f"одна ниша занимает не больше {rules.niche_cap(size)} из {size}."
        )
    return lines


async def _issue_batch(message: Message, collector_runner: CollectorRunner, user_id: int | None = None) -> None:
    """Hand out a batch to `user_id` as one browsable card message."""
    size = settings.LEADS_BATCH_SIZE
    rules = IssueRules(issue_unknown=settings.LEADS_ISSUE_UNKNOWN, max_niche_share=settings.LEADS_MAX_NICHE_SHARE)
    async with async_session_maker() as session:
        leads = await issue_next_leads(session, size, rules, user_id=user_id)

    in_group = is_group_chat(message)
    undelivered: list[int] = [lead.id for lead in leads]
    batch_id: int | None = None
    try:
        if leads:
            remaining = list(leads)
            async with async_session_maker() as session:
                batch_id = await create_issued_batch(session, [lead.id for lead in remaining])
            sent = False
            while remaining:
                lead = remaining[0]
                batch = BatchPosition(batch_id=batch_id, index=0, total=len(remaining))
                try:
                    await _send_card(message, lead, batch=batch)
                except TelegramBadRequest:
                    # A card Telegram refuses must not block the leads behind it.
                    logger.exception("Telegram rejected the card of lead %s", lead.id)
                    remaining.pop(0)
                    async with async_session_maker() as session:
                        await update_issued_batch(session, batch_id, [item.id for item in remaining])
                    continue
                sent = True
                break
            if sent:
                delivered_ids = {item.id for item in remaining}
                undelivered = [lead_id for lead_id in undelivered if lead_id not in delivered_ids]
    except TelegramAPIError:
        logger.exception("Sending the batch stopped, %d leads go back to the pool", len(undelivered))
    finally:
        # Also on cancellation: leads the admin has not received are not issued.
        if undelivered:
            async with async_session_maker() as session:
                await return_undelivered_leads(session, undelivered)
                if batch_id is not None and len(undelivered) == len(leads):
                    await delete_issued_batch(session, batch_id)

    async with async_session_maker() as session:
        pool_size = await count_new_leads(session)
        unknown = await count_unknown_site_leads(session)
        reserved = await count_reserved_for_others(session, user_id)

    delivered = len(leads) - len(undelivered)
    lines: list[str] = []
    if delivered:
        lines.append(f"✅ Выдано лидов: <b>{delivered}</b>. В запасе: <b>{pool_size}</b>.")
    if undelivered:
        lines.append(f"⚠️ Не удалось отправить: <b>{len(undelivered)}</b>, они остались в запасе.")
    # What was held back by the rules: leads that failed to be delivered are reported above.
    held_back = max(0, pool_size - len(undelivered) - reserved) if len(leads) < size else 0
    if reserved and len(leads) < size:
        lines.append(RESERVED_BY_OTHERS_LINE.format(count=reserved))
    if len(leads) < size:
        if not leads:
            lines.append("📭 Подходящих лидов сейчас нет." if held_back else "📭 Новых лидов в запасе нет.")
        else:
            lines.append(
                f"Подходящих было меньше {size}, выдал сколько есть."
                if held_back
                else f"В запасе было меньше {size}, выдал сколько есть."
            )
    if held_back > 0:
        # More leads would not change the rules: the pool is refilled only if it is really low.
        lines += _describe_held_back(held_back, unknown, rules, size)
        await collector_runner.check_pool()
    elif len(leads) < size:
        lines.append(_describe_topup(await collector_runner.request_topup(TRIGGER_BATCH)))
    else:
        await collector_runner.check_pool()
    await _answer_safely(message, "\n".join(lines), get_next_batch_keyboard() if in_group else None)


def _user_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user is not None else None


async def handle_next_batch(message: Message, collector_runner: CollectorRunner) -> None:
    """Hand out the next batch of leads and wait until it is sent."""
    chat_id, user_id = message.chat.id, _user_id(message)
    if not _reserve_chat(chat_id, user_id):
        await _answer_safely(message, BATCH_BUSY_TEXT)
        return
    try:
        await _issue_batch(message, collector_runner, user_id=user_id)
    finally:
        _release_chat(chat_id, user_id)


def _reserve_chat(chat_id: int, user_id: int | None = None) -> bool:
    """
    Take the chat and the person for one batch. No await between the check and the mark: a double tap finds them taken.

    Leads never overlap anyway (see issue_next_leads); this keeps one batch per chat and per person at a time.
    """
    if chat_id in _issuing_to or (user_id is not None and user_id in _issuing_users):
        return False
    _issuing_to.add(chat_id)
    if user_id is not None:
        _issuing_users.add(user_id)
    return True


def _release_chat(chat_id: int, user_id: int | None) -> None:
    _issuing_to.discard(chat_id)
    if user_id is not None:
        _issuing_users.discard(user_id)


def _start_reserved_batch(message: Message, collector_runner: CollectorRunner, user_id: int | None) -> None:
    """Send a batch to a chat already reserved, without waiting: about a second per card is Telegram's limit."""

    async def run() -> None:
        try:
            await _issue_batch(message, collector_runner, user_id=user_id)
        except Exception:
            # The leads that did not go out are back in the pool already (see _issue_batch).
            logger.exception("Handing out a batch to chat %s failed", message.chat.id)
        finally:
            _release_chat(message.chat.id, user_id)

    task = asyncio.create_task(run())
    _background_batches.add(task)
    task.add_done_callback(_background_batches.discard)


async def stop_background_batches() -> None:
    """On shutdown: stop sending, so the leads not yet sent go back to the pool before the database closes."""
    tasks = list(_background_batches)
    for task in tasks:
        task.cancel()
    if tasks:
        logger.info("Stopping %d batch(es) being sent", len(tasks))
        await asyncio.gather(*tasks, return_exceptions=True)


@router.message(F.text.regexp(NEXT_BATCH_BUTTON_RE))
@router.message(Command("next_leads"))
async def handle_next_batch_request(message: Message, collector_runner: CollectorRunner) -> None:
    """The bottom button or /next_leads: the batch goes out in the background, updates keep coming."""
    user_id = _user_id(message)
    if not _reserve_chat(message.chat.id, user_id):
        await _answer_safely(message, BATCH_BUSY_TEXT)
        return
    _start_reserved_batch(message, collector_runner, user_id)


@router.callback_query(F.data == "panel:next")
async def handle_next_batch_button(callback: CallbackQuery, collector_runner: CollectorRunner) -> None:
    """The same from an inline button: answered at once, the batch goes out in the background."""
    if not isinstance(callback.message, Message):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    # The person who pressed, not the author of the message with the button (the bot).
    user_id = callback.from_user.id
    reserved = _reserve_chat(callback.message.chat.id, user_id)
    try:
        await callback.answer(BATCH_STARTED_TOAST if reserved else BATCH_BUSY_TOAST)
    except TelegramAPIError as exc:
        # 'query is too old' and the like: the batch is still sent, only the spinner on the button stays.
        logger.warning("Could not answer the 'next batch' button: %s", exc)
    if reserved:
        _start_reserved_batch(callback.message, collector_runner, user_id)


@router.callback_query(F.data.startswith("lb:"))
async def handle_batch_page(callback: CallbackQuery) -> None:
    """Switch the browsable batch card left or right."""
    data = callback.data or ""
    if data == BATCH_NOOP_CALLBACK:
        await callback.answer()
        return
    parts = data.split(":")
    if (
        len(parts) != 3
        or not _is_uint(parts[1])
        or not _is_uint(parts[2])
        or not isinstance(callback.message, Message)
    ):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    batch_id, target_index = int(parts[1]), int(parts[2])

    async with async_session_maker() as session:
        lead_ids = await get_issued_batch(session, batch_id)
        if not lead_ids:
            await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
            return
        index = min(target_index, len(lead_ids) - 1)
        lead = await session.get(Lead, lead_ids[index])

    batch = BatchPosition(batch_id=batch_id, index=index, total=len(lead_ids))
    text, markup = render_issued_card(lead, batch)
    await callback.answer()
    try:
        await callback.message.edit_text(
            text,
            reply_markup=markup,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Could not switch batch %s to page %s: %s", batch_id, index, exc)
    except TelegramAPIError as exc:
        logger.warning("Could not switch batch %s to page %s: %s", batch_id, index, exc)


@router.callback_query(F.data.startswith("ls:"))
async def handle_lead_status(callback: CallbackQuery) -> None:
    """Save the status chosen on a handed-out lead and show it on the card."""
    parts = (callback.data or "").split(":")
    ok_batch, batch_id = _parse_optional_batch(parts, 3)
    if not ok_batch or not _is_uint(parts[1]) or parts[2] not in LEAD_OUTCOMES:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    lead_id, status = int(parts[1]), parts[2]
    user = callback.from_user
    changed_by = format_user_label(user.username, user.full_name)

    async with async_session_maker() as session:
        change = await change_contact_status(
            session, lead_id, status, user.id, changed_by, owner_role=settings.is_owner(user.id)
        )
        batch = await load_batch_position(session, batch_id, lead_id) if change.outcome == OUTCOME_OK else None
    lead = change.lead
    # Status buttons exist only on leads that were handed out; an old card may outlive that.
    if change.outcome == OUTCOME_MISSING or lead is None:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    if change.outcome == OUTCOME_SAME:
        await callback.answer("Этот статус уже стоит")
        return
    if change.outcome == OUTCOME_OWNED:
        await callback.answer(owned_by_text(lead), show_alert=True)
        return
    if change.outcome != OUTCOME_OK:
        await callback.answer(NOT_YOUR_LEAD_TEXT, show_alert=True)
        return

    await callback.answer(f"Статус: {LEAD_OUTCOMES[status]}")
    if isinstance(callback.message, Message):
        text, markup = render_issued_card(lead, batch)
        try:
            await callback.message.edit_text(
                text,
                reply_markup=markup,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            # The status is saved; only the card on the screen is out of date.
            logger.warning("Could not update the card of lead %s: %s", lead_id, exc)


@router.callback_query(F.data.startswith("lr:"))
async def handle_return_to_pool(callback: CallbackQuery) -> None:
    """'↩️ Вернуть в пул': the owner of a lead gives it back; an owner of the bot may return anyone's."""
    parts = (callback.data or "").split(":")
    ok_batch, batch_id = _parse_optional_batch(parts, 2)
    if not ok_batch or not _is_uint(parts[1]):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    lead_id = int(parts[1])
    user_id = callback.from_user.id
    next_lead: Lead | None = None
    next_batch: BatchPosition | None = None
    async with async_session_maker() as session:
        change = await release_lead(session, lead_id, user_id, owner_role=settings.is_owner(user_id))
        if change.outcome == OUTCOME_OK and batch_id is not None:
            removed = await remove_lead_from_batch(session, batch_id, lead_id)
            if removed is not None:
                remaining_ids, old_index = removed
                if remaining_ids:
                    next_index = min(old_index, len(remaining_ids) - 1)
                    next_lead = await session.get(Lead, remaining_ids[next_index])
                    next_batch = BatchPosition(batch_id=batch_id, index=next_index, total=len(remaining_ids))
    if change.outcome == OUTCOME_MISSING or change.lead is None:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    if change.outcome == OUTCOME_OWNED:
        await callback.answer(owned_by_text(change.lead), show_alert=True)
        return
    if change.outcome != OUTCOME_OK:
        await callback.answer(RETURN_NOT_ALLOWED_TEXT, show_alert=True)
        return

    await callback.answer(RETURNED_TOAST)
    if isinstance(callback.message, Message):
        if next_batch is not None:
            text, markup = render_issued_card(next_lead, next_batch)
        else:
            text, markup = format_returned_lead(change.lead.name), None
        try:
            await callback.message.edit_text(
                text,
                reply_markup=markup,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            # The lead is back in the pool; only the card on the screen is out of date.
            logger.warning("Could not update the card of lead %s: %s", lead_id, exc)


@router.callback_query(F.data.startswith("sv:"))
async def handle_site_verdict(callback: CallbackQuery) -> None:
    """Save what a person found out about the site of a handed-out lead and show it on the card."""
    parts = (callback.data or "").split(":")
    ok_batch, batch_id = _parse_optional_batch(parts, 3)
    if not ok_batch or not _is_uint(parts[1]) or parts[2] not in ("yes", "no"):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    lead_id, has_site = int(parts[1]), parts[2] == "yes"

    async with async_session_maker() as session:
        lead, changed = await set_site_verdict(session, lead_id, has_site)
        batch = await load_batch_position(session, batch_id, lead_id) if lead is not None and changed else None
    if lead is None:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    if not changed:
        await callback.answer("Эта отметка уже стоит")
        return

    await callback.answer(site_verdict_text(has_site))
    if isinstance(callback.message, Message):
        text, markup = render_issued_card(lead, batch)
        try:
            await callback.message.edit_text(
                text,
                reply_markup=markup,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            # The verdict is saved; only the card on the screen is out of date.
            logger.warning("Could not update the card of lead %s: %s", lead_id, exc)


def _remember_phone_prompt(
    chat_id: int,
    prompt_message_id: int,
    lead_id: int,
    card_message_id: int,
    batch_id: int | None = None,
) -> None:
    now = time.monotonic()
    for key, prompt in list(_phone_prompts.items()):
        if now - prompt.asked_at > _PHONE_PROMPT_TTL_SECONDS:
            del _phone_prompts[key]
    if len(_phone_prompts) >= _MAX_PHONE_PROMPTS:
        del _phone_prompts[next(iter(_phone_prompts))]
    _phone_prompts[(chat_id, prompt_message_id)] = _PhonePrompt(lead_id, card_message_id, now, batch_id)


@router.callback_query(F.data.startswith("lp:"))
async def handle_add_phone(callback: CallbackQuery) -> None:
    """Ask for the phone found in the card of a handed-out lead."""
    parts = (callback.data or "").split(":")
    ok_batch, batch_id = _parse_optional_batch(parts, 2)
    if not ok_batch or not _is_uint(parts[1]) or not isinstance(callback.message, Message):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    lead_id = int(parts[1])
    async with async_session_maker() as session:
        lead = await session.get(Lead, lead_id)
    if lead is None or lead.issued_at is None:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return

    user = callback.from_user
    # A selective reply field opens only for those mentioned: in a group, for the admin who pressed.
    mention = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    try:
        prompt = await callback.message.answer(
            f"{PHONE_PROMPT_PREFIX} «{html.escape(lead.name[:100])}»\n"
            f"{mention}, ответьте на это сообщение номером, например +7 921 000-00-02.",
            reply_markup=ForceReply(force_reply=True, selective=True, input_field_placeholder="+7 921 000-00-02"),
            parse_mode="HTML",
        )
    except TelegramAPIError as exc:
        logger.warning("Could not ask for the phone of lead %s: %s", lead_id, exc)
        await callback.answer("Не удалось отправить вопрос, попробуйте ещё раз", show_alert=True)
        return
    _remember_phone_prompt(callback.message.chat.id, prompt.message_id, lead_id, callback.message.message_id, batch_id)
    await callback.answer()


@router.message(F.text, F.reply_to_message.text.startswith(PHONE_PROMPT_PREFIX))
async def handle_phone_reply(message: Message) -> None:
    """Check the number, save it to the lead and show it on the card."""
    reply_to = message.reply_to_message
    key = (message.chat.id, reply_to.message_id if reply_to is not None else 0)
    prompt = _phone_prompts.get(key)
    if prompt is None or time.monotonic() - prompt.asked_at > _PHONE_PROMPT_TTL_SECONDS:
        _phone_prompts.pop(key, None)
        await _answer_safely(message, "Этот вопрос устарел. Нажмите «✏️ Добавить телефон» на лиде ещё раз.")
        return

    phone = parse_manual_phone(message.text)
    if phone is None:
        await _answer_safely(
            message,
            "⚠️ Не похоже на номер телефона. Ответьте на вопрос ещё раз одним номером, например +7 921 000-00-02.",
        )
        return

    async with async_session_maker() as session:
        lead = await set_lead_phone(session, prompt.lead_id, phone)
        batch = await load_batch_position(session, prompt.batch_id, prompt.lead_id) if lead is not None else None
    _phone_prompts.pop(key, None)  # two quick replies both get here
    if lead is None:
        await _answer_safely(message, STALE_BUTTON_TEXT)
        return

    await _answer_safely(message, f"✅ Телефон сохранён: <code>{html.escape(phone)}</code>")
    if message.bot is None:
        return
    text, markup = render_issued_card(lead, batch)
    try:
        await message.bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=prompt.card_message_id,
            reply_markup=markup,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramAPIError as exc:
        # The phone is saved; only the card on the screen is out of date.
        logger.warning("Could not update the card of lead %s: %s", prompt.lead_id, exc)


@router.message(Command("rescore"))
async def handle_rescore(message: Message) -> None:
    """Score the pool by the current weights: leads saved earlier keep their old score until then."""
    async with async_session_maker() as session:
        total, changed = await rescore_new_leads(session, settings)
    await _answer_safely(
        message, f"🔢 Лидов в запасе пересчитано: <b>{total}</b>, оценка изменилась у <b>{changed}</b>."
    )


@router.callback_query(F.data == "panel:stats")
async def handle_stats_button(callback: CallbackQuery, collector_runner: CollectorRunner) -> None:
    """The same as /leads_stats from an inline button."""
    if not isinstance(callback.message, Message):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    await callback.answer()
    # The message of the button is the bot's own: who may export is decided by the person who pressed.
    await handle_leads_stats(callback.message, collector_runner, viewer_id=callback.from_user.id)


@router.message(Command("leads_stats"))
async def handle_leads_stats(
    message: Message, collector_runner: CollectorRunner | None = None, viewer_id: int | None = None
) -> None:
    """
    Show how many leads are in the pool, what happened to the handed-out ones and the API requests spent.

    The CSV export under it is for owners of the bot only (see src/bot/roles.py).
    """
    if viewer_id is None and message.from_user is not None:
        viewer_id = message.from_user.id
    async with async_session_maker() as session:
        counts = await get_status_counts(session)
        contact_counts = await get_contact_status_counts(session)
        issued_total = await count_issued_leads(session)
        pool_stats = await get_pool_stats(session)
    text = format_funnel_stats(counts, contact_counts, issued_total, pool_stats)
    if collector_runner is not None:
        text += "\n" + format_request_usage(await collector_runner.get_request_usage())
    await message.answer(
        text,
        reply_markup=get_full_export_keyboard() if settings.is_owner(viewer_id) else None,
        parse_mode="HTML",
    )
