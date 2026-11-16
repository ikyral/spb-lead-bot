"""Reminders about leads the owner wrote to, and the plan of the day. Access is restricted by AdminOnlyMiddleware."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from src.bot.chats import is_group_chat
from src.bot.digest import DailyDigest
from src.bot.formatters import (
    format_followup,
    format_issued_lead,
    format_msk_date,
    format_plan,
    format_team,
    format_user_label,
    owned_by_text,
)
from src.bot.keyboards import STALE_BUTTON_TEXT, get_issued_lead_keyboard
from src.bot.texts import (
    DIGEST_DONE_MARK,
    DIGEST_LATER_DAYS,
    DIGEST_LATER_MARK,
    DIGEST_REJECT_MARK,
    NOT_YOUR_LEAD_TEXT,
    REMIND_DAYS,
    REMIND_IMPOSSIBLE_TOAST,
    REMIND_SET_TOAST,
)
from src.config import settings
from src.database.models import CONTACT_CONTACTED, CONTACT_REJECTED, Lead
from src.database.repo import (
    OUTCOME_OK,
    OUTCOME_OWNED,
    OUTCOME_SAME,
    REMINDER_ENDING_STATUSES,
    async_session_maker,
    change_contact_status,
    get_owner_summary,
    get_team_stats,
    set_remind_at,
)

logger = logging.getLogger(__name__)
router = Router(name="reminders")

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
# Telegram asks for about one message a second to a chat, and no more than 20 a minute to a group.
_MESSAGE_PAUSE_SECONDS: float = 1.0
_GROUP_MESSAGE_PAUSE_SECONDS: float = 3.1
_FOLLOWUP_ACTIONS: frozenset[str] = frozenset({"done", "reject", "later"})


def _parse(data: str | None, prefix: str) -> tuple[int, str] | None:
    """'prefix:<lead id>:<value>' of a button, or None for a stale or forged one."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != prefix or not parts[1].isdigit():
        return None
    return int(parts[1]), parts[2]


@router.callback_query(F.data.startswith("rm:"))
async def handle_remind(callback: CallbackQuery) -> None:
    """'⏰ 1 д / 3 д / 7 д' under a card: set the reminder, a second press replaces it."""
    parsed = _parse(callback.data, "rm")
    if parsed is None or not parsed[1].isdigit() or int(parsed[1]) not in REMIND_DAYS:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    lead_id, days = parsed[0], int(parsed[1])
    remind_at = datetime.now(UTC) + timedelta(days=days)
    # One quick write before the answer: the answer must not promise a reminder that was not saved.
    async with async_session_maker() as session:
        lead = await set_remind_at(session, lead_id, remind_at)
    if lead is None:
        await callback.answer(REMIND_IMPOSSIBLE_TOAST, show_alert=True)
        return
    await callback.answer(REMIND_SET_TOAST.format(date=format_msk_date(remind_at)))
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                format_issued_lead(lead),
                reply_markup=get_issued_lead_keyboard(lead),
                parse_mode="HTML",
                link_preview_options=_NO_PREVIEW,
            )
        except TelegramAPIError as exc:
            # The reminder is saved; only the card on the screen is out of date.
            logger.warning("Could not update the card of lead %s: %s", lead_id, exc)


@router.callback_query(F.data.startswith("dg:"))
async def handle_followup(callback: CallbackQuery) -> None:
    """The buttons of a lead in the plan: reminded, refused, or remind again in three days."""
    parsed = _parse(callback.data, "dg")
    if parsed is None or parsed[1] not in _FOLLOWUP_ACTIONS:
        await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
        return
    lead_id, action = parsed
    user = callback.from_user
    changed_by = format_user_label(user.username, user.full_name)
    now = datetime.now(UTC)

    async with async_session_maker() as session:
        lead = await session.get(Lead, lead_id)
        if lead is None or lead.issued_at is None:
            await callback.answer(STALE_BUTTON_TEXT, show_alert=True)
            return
        if lead.contact_status in REMINDER_ENDING_STATUSES and action != "reject":
            await callback.answer(REMIND_IMPOSSIBLE_TOAST, show_alert=True)
            return
        if action in ("reject", "done"):
            # Written to once more: the days without an answer and the ownership count from now.
            status = CONTACT_REJECTED if action == "reject" else CONTACT_CONTACTED
            change = await change_contact_status(
                session,
                lead_id,
                status,
                user.id,
                changed_by,
                owner_role=settings.is_owner(user.id),
                now=now,
                repeat=action == "done",
            )
            if change.outcome == OUTCOME_OWNED and change.lead is not None:
                await callback.answer(owned_by_text(change.lead), show_alert=True)
                return
            if change.outcome not in (OUTCOME_OK, OUTCOME_SAME):
                await callback.answer(NOT_YOUR_LEAD_TEXT, show_alert=True)
                return
            if action == "reject":
                mark = DIGEST_REJECT_MARK
            else:
                await set_remind_at(session, lead_id, None)
                mark = DIGEST_DONE_MARK.format(date=format_msk_date(now))
        else:
            later = now + timedelta(days=DIGEST_LATER_DAYS)
            await set_remind_at(session, lead_id, later)
            mark = DIGEST_LATER_MARK.format(date=format_msk_date(later))
        await session.refresh(lead)

    await callback.answer(mark)
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                format_followup(lead, mark), reply_markup=None, parse_mode="HTML", link_preview_options=_NO_PREVIEW
            )
        except TelegramAPIError as exc:
            logger.warning("Could not update the plan message of lead %s: %s", lead_id, exc)


@router.message(Command("today"))
async def handle_today(message: Message, daily_digest: DailyDigest) -> None:
    """
    The plan of the day on request, like the morning one but with the leads of the person who asked only.

    It does not count as the morning plan.
    """
    owner_id = message.from_user.id if message.from_user is not None else None
    messages = await daily_digest.build(owner_id=owner_id)
    pause = _GROUP_MESSAGE_PAUSE_SECONDS if is_group_chat(message) else _MESSAGE_PAUSE_SECONDS
    for index, (text, markup) in enumerate(messages):
        if index:
            await asyncio.sleep(pause)
        try:
            await message.answer(text, reply_markup=markup, parse_mode="HTML", link_preview_options=_NO_PREVIEW)
        except TelegramAPIError as exc:
            logger.warning("Could not send the plan of the day to chat %s: %s", message.chat.id, exc)
            return


@router.message(Command("plan"))
async def handle_plan(message: Message) -> None:
    """The leads of the person who asked: by status, and those that go back to the pool within a day."""
    if message.from_user is None:
        return
    async with async_session_maker() as session:
        summary = await get_owner_summary(session, message.from_user.id)
    await message.answer(format_plan(summary), parse_mode="HTML", link_preview_options=_NO_PREVIEW)


@router.message(Command("team"))
async def handle_team(message: Message) -> None:
    """Who works how many leads. Only owners of the bot get here (see src/bot/roles.py)."""
    async with async_session_maker() as session:
        rows = await get_team_stats(session)
    # Roles are marked only with several owners: with one the list looks as it always did.
    if settings.has_extra_owners:
        text = format_team(rows, settings.owner_ids, settings.operator_ids)
    else:
        text = format_team(rows)
    await message.answer(text, parse_mode="HTML")
