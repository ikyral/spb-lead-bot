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

from src.bot.chats import is_group_chat
from src.bot.formatters import (
    format_funnel_stats,
    format_issued_lead,
    format_msk_time,
    format_request_usage,
    format_user_label,
    site_verdict_text,
)
from src.bot.keyboards import (
    LEAD_OUTCOMES,
    STALE_BUTTON_TEXT,
    get_full_export_keyboard,
    get_issued_lead_keyboard,
    get_next_batch_keyboard,
)
from src.bot.texts import BATCH_BUSY_TEXT, BATCH_BUSY_TOAST, BATCH_STARTED_TOAST
from src.config import settings
from src.database.models import Lead
from src.database.repo import (
    IssueRules,
    async_session_maker,
    count_issued_leads,
    count_new_leads,
    count_unknown_site_leads,
    get_contact_status_counts,
    get_pool_stats,
    get_status_counts,
    issue_next_leads,
    return_undelivered_leads,
    set_contact_status,
    set_lead_phone,
    set_site_verdict,
)
from src.services.chains import rescore_new_leads
from src.services.normalize import parse_manual_phone
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


# By (chat, message with the question). A restart loses them; the admin is then asked to press the button again.
_phone_prompts: dict[tuple[int, int], _PhonePrompt] = {}


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


async def _send_card(message: Message, lead: Lead) -> None:
    """Send one lead with its status buttons, waiting as long as Telegram asks to."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            await message.answer(
                format_issued_lead(lead),
                reply_markup=get_issued_lead_keyboard(lead),
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


async def _issue_batch(message: Message, collector_runner: CollectorRunner) -> None:
    size = settings.LEADS_BATCH_SIZE
    rules = IssueRules(issue_unknown=settings.LEADS_ISSUE_UNKNOWN, max_niche_share=settings.LEADS_MAX_NICHE_SHARE)
    async with async_session_maker() as session:
        leads = await issue_next_leads(session, size, rules)

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
        unknown = await count_unknown_site_leads(session)

    delivered = len(leads) - len(undelivered)
    lines: list[str] = []
    if delivered:
        lines.append(f"✅ Выдано лидов: <b>{delivered}</b>. В запасе: <b>{pool_size}</b>.")
    if undelivered:
        lines.append(f"⚠️ Не удалось отправить: <b>{len(undelivered)}</b>, они остались в запасе.")
    # What was held back by the rules: leads that failed to be delivered are reported above.
    held_back = pool_size - len(undelivered) if len(leads) < size else 0
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


async def handle_next_batch(message: Message, collector_runner: CollectorRunner) -> None:
    """Hand out the next batch of leads and wait until it is sent."""
    chat_id = message.chat.id
    if not _reserve_chat(chat_id):
        await _answer_safely(message, BATCH_BUSY_TEXT)
        return
    try:
        await _issue_batch(message, collector_runner)
    finally:
        _issuing_to.discard(chat_id)


def _reserve_chat(chat_id: int) -> bool:
    """Take the chat for one batch. No await between the check and the mark: a double tap finds it taken."""
    if chat_id in _issuing_to:
        return False
    _issuing_to.add(chat_id)
    return True


def _start_reserved_batch(message: Message, collector_runner: CollectorRunner) -> None:
    """Send a batch to a chat already reserved, without waiting: about a second per card is Telegram's limit."""

    async def run() -> None:
        try:
            await _issue_batch(message, collector_runner)
        except Exception:
            # The leads that did not go out are back in the pool already (see _issue_batch).
            logger.exception("Handing out a batch to chat %s failed", message.chat.id)
        finally:
            _issuing_to.discard(message.chat.id)

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
    if not _reserve_chat(message.chat.id):
        await _answer_safely(message, BATCH_BUSY_TEXT)
        return
    _start_reserved_batch(message, collector_runner)


@router.callback_query(F.data == "panel:next")
async def handle_next_batch_button(callback: CallbackQuery, collector_runner: CollectorRunner) -> None:
    """The same from an inline button: answered at once, the batch goes out in the background."""
    if not isinstance(callback.message, Message):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    reserved = _reserve_chat(callback.message.chat.id)
    try:
        await callback.answer(BATCH_STARTED_TOAST if reserved else BATCH_BUSY_TOAST)
    except TelegramAPIError as exc:
        # 'query is too old' and the like: the batch is still sent, only the spinner on the button stays.
        logger.warning("Could not answer the 'next batch' button: %s", exc)
    if reserved:
        _start_reserved_batch(callback.message, collector_runner)


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
                reply_markup=get_issued_lead_keyboard(lead),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            # The status is saved; only the card on the screen is out of date.
            logger.warning("Could not update the card of lead %s: %s", lead_id, exc)


@router.callback_query(F.data.startswith("sv:"))
async def handle_site_verdict(callback: CallbackQuery) -> None:
    """Save what a person found out about the site of a handed-out lead and show it on the card."""
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("yes", "no"):
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    lead_id, has_site = int(parts[1]), parts[2] == "yes"

    async with async_session_maker() as session:
        lead, changed = await set_site_verdict(session, lead_id, has_site)
    if lead is None:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    if not changed:
        await callback.answer("Эта отметка уже стоит")
        return

    await callback.answer(site_verdict_text(has_site))
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                format_issued_lead(lead),
                reply_markup=get_issued_lead_keyboard(lead),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            # The verdict is saved; only the card on the screen is out of date.
            logger.warning("Could not update the card of lead %s: %s", lead_id, exc)


def _remember_phone_prompt(chat_id: int, prompt_message_id: int, lead_id: int, card_message_id: int) -> None:
    now = time.monotonic()
    for key, prompt in list(_phone_prompts.items()):
        if now - prompt.asked_at > _PHONE_PROMPT_TTL_SECONDS:
            del _phone_prompts[key]
    if len(_phone_prompts) >= _MAX_PHONE_PROMPTS:
        del _phone_prompts[next(iter(_phone_prompts))]
    _phone_prompts[(chat_id, prompt_message_id)] = _PhonePrompt(lead_id, card_message_id, now)


@router.callback_query(F.data.startswith("lp:"))
async def handle_add_phone(callback: CallbackQuery) -> None:
    """Ask for the phone found in the card of a handed-out lead."""
    parts = (callback.data or "").split(":")
    if len(parts) != 2 or not parts[1].isdigit() or not isinstance(callback.message, Message):
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
    _remember_phone_prompt(callback.message.chat.id, prompt.message_id, lead_id, callback.message.message_id)
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
    _phone_prompts.pop(key, None)  # two quick replies both get here
    if lead is None:
        await _answer_safely(message, STALE_BUTTON_TEXT)
        return

    await _answer_safely(message, f"✅ Телефон сохранён: <code>{html.escape(phone)}</code>")
    if message.bot is None:
        return
    try:
        await message.bot.edit_message_text(
            format_issued_lead(lead),
            chat_id=message.chat.id,
            message_id=prompt.card_message_id,
            reply_markup=get_issued_lead_keyboard(lead),
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
    await handle_leads_stats(callback.message, collector_runner)


@router.message(Command("leads_stats"))
async def handle_leads_stats(message: Message, collector_runner: CollectorRunner | None = None) -> None:
    """Show how many leads are in the pool, what happened to the handed-out ones and the API requests spent."""
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
        reply_markup=get_full_export_keyboard(),
        parse_mode="HTML",
    )
