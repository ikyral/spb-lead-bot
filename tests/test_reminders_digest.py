"""Tests for reminders about leads the owner wrote to, and for the morning plan sent only to admins."""

from datetime import UTC, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User

from src.bot.digest import DailyDigest
from src.bot.formatters import MAX_MESSAGE_LENGTH, format_digest_summary, format_followup, format_issued_lead
from src.bot.handlers import reminders
from src.bot.keyboards import get_issued_lead_keyboard
from src.bot.notifier import AdminNotifier
from src.config import Settings
from src.database.models import (
    CONTACT_CONTACTED,
    CONTACT_NO_ANSWER,
    CONTACT_REJECTED,
    CONTACT_REPLIED,
    STATUS_ISSUED,
    STATUS_NEW,
    CollectorState,
    Lead,
)
from src.database.repo import DigestCounts, get_digest_counts, get_followups, set_contact_status
from src.services.digest import FOLLOWUPS_SHOWN, DigestData, digest_bounds, digest_window_open
from src.services.scheduler import DgisKeyStatus, RequestUsage
from src.services.site_status import SITE_DEAD, SITE_FOUND, SITE_NO_SITE, SITE_UNKNOWN

MSK = ZoneInfo("Europe/Moscow")
# 10:00 in Moscow.
NOW = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
METHOD = MagicMock(spec=SendMessage)
ADMIN, GROUP = 100, -1001


def _lead(number: int, **overrides: Any) -> Lead:
    values: dict[str, Any] = {
        "id": number,
        "external_id": f"2gis_{number}",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": "dentist_clinic",
        "site_status": SITE_NO_SITE,
        "score": 50,
        "crm_status": STATUS_NEW,
        "source_url": f"https://2gis.ru/spb/firm/{70000000 + number}",
        "created_at": NOW - timedelta(days=30),
    }
    values.update(overrides)
    return Lead(**values)


def _issued(number: int, status: str | None = CONTACT_CONTACTED, written_days_ago: float = 1, **overrides: Any) -> Lead:
    return _lead(
        number,
        crm_status=STATUS_ISSUED,
        issued_at=NOW - timedelta(days=10),
        contact_status=status,
        contact_status_at=NOW - timedelta(days=written_days_ago) if status else None,
        **overrides,
    )


async def _add(session_maker, *leads: Lead) -> None:
    async with session_maker() as session:
        session.add_all(leads)
        await session.commit()


async def _get(session_maker, lead_id: int) -> Lead:
    async with session_maker() as session:
        lead = await session.get(Lead, lead_id)
    assert lead is not None
    return lead


# ---------- reminders on a card ----------


def _callback(data: str, message: Any = None) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = User(id=ADMIN, is_bot=False, first_name="Admin", username="owner")
    callback.answer = AsyncMock()
    callback.message = message or MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    return callback


@pytest.fixture
def db(session_maker, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reminders, "async_session_maker", session_maker)
    return session_maker


async def test_a_reminder_is_set_answered_with_its_date_and_shown_on_the_card(db) -> None:
    await _add(db, _issued(1))
    callback = _callback("rm:1:3")

    await reminders.handle_remind(callback)

    lead = await _get(db, 1)
    assert lead.remind_at is not None
    assert timedelta(days=3) - timedelta(minutes=1) < lead.remind_at - datetime.now(UTC) <= timedelta(days=3)
    expected = lead.remind_at.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m")
    callback.answer.assert_awaited_once_with(f"Напомню {expected}")
    assert f"⏰ <b>Напомню:</b> {expected}" in callback.message.edit_text.await_args.args[0]


async def test_pressing_again_replaces_the_reminder(db) -> None:
    await _add(db, _issued(1))
    await reminders.handle_remind(_callback("rm:1:7"))
    first = (await _get(db, 1)).remind_at

    await reminders.handle_remind(_callback("rm:1:1"))

    second = (await _get(db, 1)).remind_at
    assert first is not None and second is not None and second < first - timedelta(days=5)


@pytest.mark.parametrize("data", ["rm:1:2", "rm:1:x", "rm:x:3", "rm:1", "rm:1:3:4"])
async def test_a_wrong_reminder_button_is_stale(db, data: str) -> None:
    await _add(db, _issued(1))
    callback = _callback(data)

    await reminders.handle_remind(callback)

    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert (await _get(db, 1)).remind_at is None


@pytest.mark.parametrize("status", [CONTACT_REPLIED, CONTACT_REJECTED])
async def test_no_reminder_after_an_answer_or_a_refusal(db, status: str) -> None:
    await _add(db, _issued(1, status=status))
    callback = _callback("rm:1:3")

    await reminders.handle_remind(callback)

    assert "не нужно" in callback.answer.await_args.args[0]
    assert (await _get(db, 1)).remind_at is None


@pytest.mark.parametrize(
    ("status", "kept"),
    [(CONTACT_REPLIED, False), (CONTACT_REJECTED, False), (CONTACT_NO_ANSWER, True), (CONTACT_CONTACTED, True)],
)
async def test_a_reply_or_a_refusal_clears_the_reminder(session_maker, status: str, kept: bool) -> None:
    await _add(session_maker, _issued(1, remind_at=NOW + timedelta(days=2)))

    async with session_maker() as session:
        await set_contact_status(session, 1, status, ADMIN, "@owner")

    assert ((await _get(session_maker, 1)).remind_at is not None) is kept


def test_reminder_buttons_appear_only_after_wrote_to_them() -> None:
    def data(lead: Lead) -> list[str]:
        markup = get_issued_lead_keyboard(lead)
        return [button.callback_data or "" for row in markup.inline_keyboard for button in row]

    assert [item for item in data(_issued(1)) if item.startswith("rm:")] == ["rm:1:1", "rm:1:3", "rm:1:7"]
    for status in (None, CONTACT_REPLIED, CONTACT_REJECTED, CONTACT_NO_ANSWER):
        assert not any(item.startswith("rm:") for item in data(_issued(1, status=status)))


def test_the_card_does_not_show_a_reminder_after_a_refusal() -> None:
    lead = _issued(1, status=CONTACT_REJECTED, remind_at=NOW)
    assert "Напомню" not in format_issued_lead(lead)


# ---------- who is due ----------


async def test_followups_take_due_reminders_and_long_silence_only(session_maker) -> None:
    due_before, stale_before = digest_bounds(NOW, MSK, 3)
    await _add(
        session_maker,
        _issued(1, remind_at=NOW - timedelta(days=1)),  # due yesterday
        _issued(2, remind_at=NOW + timedelta(hours=5)),  # due today, in the evening
        _issued(3, remind_at=NOW + timedelta(days=2)),  # later
        _issued(4, written_days_ago=5),  # wrote 5 days ago, no reminder
        _issued(5, written_days_ago=1),  # wrote yesterday
        _issued(6, status=CONTACT_REPLIED, written_days_ago=9),
        _issued(7, status=CONTACT_REJECTED, remind_at=NOW - timedelta(days=1)),
        _issued(8, status=CONTACT_NO_ANSWER, written_days_ago=9),  # silence without a reminder: not 'wrote to them'
        _issued(9, status=None, remind_at=NOW - timedelta(hours=1)),  # a reminder without a status still counts
        _lead(10, contact_status=CONTACT_CONTACTED, contact_status_at=NOW - timedelta(days=9)),  # never handed out
        _issued(11, written_days_ago=8, remind_at=NOW + timedelta(days=1)),  # postponed: waits for its date
    )

    async with session_maker() as session:
        followups = await get_followups(session, due_before, stale_before, FOLLOWUPS_SHOWN)

    # Due reminders by date first, then the longest silence.
    assert [lead.id for lead in followups] == [1, 9, 2, 4]


async def test_no_more_than_ten_followups(session_maker) -> None:
    due_before, stale_before = digest_bounds(NOW, MSK, 3)
    await _add(session_maker, *(_issued(n, remind_at=NOW - timedelta(hours=n)) for n in range(1, 15)))

    async with session_maker() as session:
        followups = await get_followups(session, due_before, stale_before, FOLLOWUPS_SHOWN)

    assert len(followups) == 10


# ---------- the plan ----------


async def test_digest_counts(session_maker) -> None:
    due_before, stale_before = digest_bounds(NOW, MSK, 3)
    await _add(
        session_maker,
        _lead(1, site_status=SITE_NO_SITE),
        _lead(2, site_status=SITE_DEAD),
        _lead(3, site_status="social_only"),
        _lead(4, site_status=SITE_NO_SITE, is_chain=True),  # a chain is not handed out
        _lead(5, site_status=SITE_FOUND),
        _lead(6, site_status=SITE_UNKNOWN),
        _lead(7, site_status=SITE_UNKNOWN),
        _issued(8, remind_at=NOW),
        _issued(9, written_days_ago=4),
        _issued(10, written_days_ago=4, remind_at=NOW + timedelta(days=3)),
    )

    async with session_maker() as session:
        counts = await get_digest_counts(session, due_before, stale_before)

    assert counts == DigestCounts(useful_in_pool=3, waiting_for_enrichment=2, reminders_due=1, written_no_answer=2)


def _data(counts: DigestCounts, followups: list[Lead], usage: RequestUsage | None = None) -> DigestData:
    return DigestData(NOW.date(), counts, followups, 3, usage)


def _usage(used: int, limit: int) -> RequestUsage:
    key = DgisKeyStatus(used=used, limit=limit, reserve=50, expires_on=None, days_left=None, request_contacts=False)
    return RequestUsage(collector_today=0, collector_day_limit=200, dgis_key=key)


def test_an_empty_plan_says_there_is_nobody_to_remind() -> None:
    text = format_digest_summary(_data(DigestCounts(0, 0, 0, 0), []))

    assert "План на 09.11" in text
    assert "Полезных лидов в запасе (без сайта и сайт не открывается): <b>0</b>" in text
    assert "Напоминать сегодня некому." in text
    assert "API 2ГИС" not in text  # no key, no line


def test_a_plan_with_data() -> None:
    text = format_digest_summary(_data(DigestCounts(12, 340, 2, 5), [_issued(1), _issued(2)], _usage(640, 1000)))

    assert "<b>12</b>" in text and "<b>340</b>" in text
    assert "Напоминаний на сегодня: <b>2</b>" in text
    assert "«Написал» без ответа дольше 3 дн.: <b>5</b>" in text
    assert "Запросов к API 2ГИС осталось: <b>360</b> из 1000" in text
    assert "Пора напомнить (2)" in text


def test_a_followup_shows_contacts_and_the_card_and_stays_within_the_limit() -> None:
    lead = _issued(1, phone="+79210000001", rubric="Стоматология <b>", remind_at=NOW)
    text = format_followup(lead)

    assert "+79210000001" in text
    assert "Стоматология &lt;b&gt;" in text
    assert 'href="https://2gis.ru/spb/firm/70000001"' in text
    assert "Написали" in text and "Напомнить было" in text

    socials = ", ".join(f"https://x{n}.ru/{'a' * 490}" for n in range(6))
    huge = _issued(2, name="Н" * 255, phone="7" * 128, rubric="Р" * 255, socials=socials)
    assert len(format_followup(huge)) > 0 and "x4.ru" in format_followup(huge)  # five distinct links fit
    assert len(format_followup(huge, "✅ Напомнили 09.11")) <= MAX_MESSAGE_LENGTH


def test_the_window_of_the_morning_plan() -> None:
    def at(hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 11, 9, hour, minute, tzinfo=MSK)

    start = time(9, 30)
    assert not digest_window_open(at(9, 29), MSK, start)
    assert digest_window_open(at(9, 30), MSK, start)
    assert digest_window_open(at(11, 59), MSK, start)
    assert not digest_window_open(at(12, 0), MSK, start)  # missed plans are not sent after noon
    # A late DIGEST_TIME still has two hours.
    assert digest_window_open(at(19, 30), MSK, time(18, 0))
    assert not digest_window_open(at(20, 0), MSK, time(18, 0))
    assert digest_window_open(at(23, 59), MSK, time(23, 0))


@pytest.mark.parametrize(("raw", "expected"), [("9:30", "09:30"), ("07:05", "07:05"), ("0:00", "00:00")])
def test_digest_time_setting(raw: str, expected: str) -> None:
    assert expected == Settings(_env_file=None, DIGEST_TIME=raw).DIGEST_TIME


@pytest.mark.parametrize("raw", ["24:00", "9.30", "nine", "09:60"])
def test_a_wrong_digest_time_names_the_variable(raw: str) -> None:
    with pytest.raises(ValueError, match="DIGEST_TIME"):
        Settings(_env_file=None, DIGEST_TIME=raw)


# ---------- sending the plan ----------


def _digest(session_maker, notifier: Any, now: datetime, **kwargs: Any) -> DailyDigest:
    return DailyDigest(session_maker, notifier, MSK, time(9, 30), 3, clock=lambda: now, **kwargs)


async def test_the_plan_goes_out_once_a_day(session_maker) -> None:
    await _add(session_maker, _issued(1, remind_at=NOW - timedelta(hours=1)))
    notifier = MagicMock()
    notifier.send_sequence = AsyncMock()

    await _digest(session_maker, notifier, NOW).send_scheduled()
    # A restart after a deployment at 10:30: the plan of today has gone out already.
    await _digest(session_maker, notifier, NOW + timedelta(minutes=30)).send_scheduled()

    notifier.send_sequence.assert_awaited_once()
    messages = notifier.send_sequence.await_args.args[0]
    assert len(messages) == 2  # the summary, then the lead to remind about
    summary, summary_markup = messages[0]
    assert "План на 09.11" in summary
    assert summary_markup.inline_keyboard[0][0].callback_data == "panel:next"
    followup_data = [button.callback_data for row in messages[1][1].inline_keyboard for button in row]
    assert followup_data == ["dg:1:done", "dg:1:reject", "dg:1:later"]
    async with session_maker() as session:
        state = await session.get(CollectorState, 1)
    assert state is not None and state.digest_sent_on == "2026-11-09"


async def test_the_next_day_the_plan_goes_out_again(session_maker) -> None:
    notifier = MagicMock()
    notifier.send_sequence = AsyncMock()

    await _digest(session_maker, notifier, NOW).send_scheduled()
    await _digest(session_maker, notifier, NOW + timedelta(days=1)).send_scheduled()

    assert notifier.send_sequence.await_count == 2


@pytest.mark.parametrize("hour_utc", [6, 9])  # 09:00 and 12:00 in Moscow
async def test_no_plan_before_its_time_or_after_noon(session_maker, hour_utc: int) -> None:
    notifier = MagicMock()
    notifier.send_sequence = AsyncMock()

    await _digest(session_maker, notifier, datetime(2026, 11, 9, hour_utc, 0, tzinfo=UTC)).send_scheduled()

    notifier.send_sequence.assert_not_awaited()
    async with session_maker() as session:
        state = await session.get(CollectorState, 1)
    assert state is None or state.digest_sent_on is None  # the day is not used up


async def test_a_disabled_plan_is_not_sent(session_maker) -> None:
    notifier = MagicMock()
    notifier.send_sequence = AsyncMock()

    await _digest(session_maker, notifier, NOW, enabled=False).send_scheduled()

    notifier.send_sequence.assert_not_awaited()


async def test_a_failing_plan_does_not_raise(session_maker, caplog) -> None:
    async def broken_usage() -> RequestUsage:
        raise RuntimeError("database is gone")

    notifier = MagicMock()
    notifier.send_sequence = AsyncMock()

    await _digest(session_maker, notifier, NOW, usage=broken_usage).send_scheduled()

    assert "The morning plan failed" in caplog.text
    notifier.send_sequence.assert_not_awaited()
    # The day is not used up: the next start may still send it.
    async with session_maker() as session:
        state = await session.get(CollectorState, 1)
    assert state is None or state.digest_sent_on is None


async def test_one_chat_refusing_the_plan_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.bot.notifier.asyncio.sleep", AsyncMock())
    sent: list[tuple[int, str]] = []

    async def send_message(chat_id: int, text: str, **kwargs: Any) -> None:
        if chat_id == GROUP:
            raise TelegramBadRequest(method=METHOD, message="chat not found")
        if chat_id == ADMIN + 1:
            raise TelegramForbiddenError(method=METHOD, message="bot was blocked by the user")
        sent.append((chat_id, text))

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=send_message)
    notifier = AdminNotifier(bot, [ADMIN, ADMIN + 1], [GROUP], notify_admins=True)
    markup = InlineKeyboardMarkup(inline_keyboard=[])

    await notifier.send_sequence([("план", markup), ("лид 1", None), ("лид 2", None)])

    assert sent == [(ADMIN, "план"), (ADMIN, "лид 1"), (ADMIN, "лид 2")]
    # A refusing chat is tried once, not for every message.
    tried = [call.args[0] for call in bot.send_message.await_args_list]
    assert tried.count(GROUP) == 1 and tried.count(ADMIN + 1) == 1


# ---------- the buttons of the plan and /today ----------


async def test_reminded_starts_the_silence_anew_and_clears_the_reminder(db) -> None:
    await _add(db, _issued(1, written_days_ago=5, remind_at=NOW))
    callback = _callback("dg:1:done")

    await reminders.handle_followup(callback)

    lead = await _get(db, 1)
    assert lead.remind_at is None
    assert lead.contact_status == CONTACT_CONTACTED and lead.contact_status_by == "@owner"
    assert lead.contact_status_at is not None and datetime.now(UTC) - lead.contact_status_at < timedelta(minutes=1)
    assert callback.answer.await_args.args[0].startswith("✅ Напомнили")
    assert callback.message.edit_text.await_args.kwargs["reply_markup"] is None


async def test_refused_from_the_plan(db) -> None:
    await _add(db, _issued(1, remind_at=NOW))

    await reminders.handle_followup(_callback("dg:1:reject"))

    lead = await _get(db, 1)
    assert lead.contact_status == CONTACT_REJECTED and lead.remind_at is None


async def test_later_puts_the_reminder_three_days_ahead(db) -> None:
    await _add(db, _issued(1, remind_at=NOW))
    callback = _callback("dg:1:later")

    await reminders.handle_followup(callback)

    lead = await _get(db, 1)
    assert lead.remind_at is not None and lead.remind_at - datetime.now(UTC) > timedelta(days=2, hours=23)
    assert callback.answer.await_args.args[0].startswith("⏰ Отложено до")


async def test_a_plan_button_of_a_lead_that_answered_meanwhile_changes_nothing(db) -> None:
    await _add(db, _issued(1, status=CONTACT_REPLIED))
    callback = _callback("dg:1:done")

    await reminders.handle_followup(callback)

    assert (await _get(db, 1)).contact_status == CONTACT_REPLIED
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_today_sends_the_plan_to_the_chat_it_was_asked_in(session_maker, monkeypatch) -> None:
    monkeypatch.setattr(reminders.asyncio, "sleep", AsyncMock())
    await _add(session_maker, _issued(1, remind_at=NOW - timedelta(hours=1)), _issued(2, written_days_ago=6))
    message = MagicMock(spec=Message)
    message.chat = Chat(id=ADMIN, type="private")
    message.answer = AsyncMock()
    digest = _digest(session_maker, MagicMock(), NOW)

    await reminders.handle_today(message, digest)

    texts = [call.args[0] for call in message.answer.await_args_list]
    assert len(texts) == 3 and "План на 09.11" in texts[0]
    assert "Организация 1" in texts[1] and "Организация 2" in texts[2]
    async with session_maker() as session:
        assert await session.get(CollectorState, 1) is None  # /today does not use up the morning plan
