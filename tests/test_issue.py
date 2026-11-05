"""Tests for handing out leads in batches, status buttons, admin access and the schema upgrade."""

import asyncio
import csv
import io
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.bot.formatters import (
    MAX_MESSAGE_LENGTH,
    format_collection_report,
    format_collector_status,
    format_funnel_stats,
    format_issued_lead,
)
from src.bot.handlers import collector as collector_handlers
from src.bot.handlers import issue, leads
from src.bot.keyboards import LEAD_OUTCOMES, STALE_BUTTON_TEXT, get_lead_status_keyboard, get_main_menu_keyboard
from src.bot.middlewares import AdminOnlyMiddleware
from src.bot.notifier import AdminNotifier
from src.config import Settings, settings
from src.database.models import (
    STATUS_CONTACTED,
    STATUS_ISSUED,
    STATUS_NEW,
    STATUS_NO_ANSWER,
    STATUS_REJECTED,
    STATUS_REPLIED,
    CollectorState,
    Lead,
)
from src.database.repo import (
    count_issued_leads,
    count_new_leads,
    get_status_counts,
    init_db,
    issue_next_leads,
    return_undelivered_leads,
)
from src.services.collector import CollectResult
from src.services.scheduler import TopupDecision, TopupOutcome


def _lead(number: int, **fields) -> Lead:
    values = {
        "external_id": f"2gis_{number}",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": "dentist_clinic",
        "site_status": "no_site",
        "score": 50,
        "crm_status": STATUS_NEW,
        "created_at": datetime(2026, 11, 5, 10, 0, tzinfo=UTC) + timedelta(minutes=number),
    }
    values.update(fields)
    return Lead(**values)


async def _add_leads(session_maker, count: int, start: int = 1) -> None:
    async with session_maker() as session:
        session.add_all(_lead(number) for number in range(start, start + count))
        await session.commit()


# ---------- repository ----------


async def test_batch_is_the_oldest_leads_in_stable_order_and_marked_issued(session_maker) -> None:
    async with session_maker() as session:
        # Inserted out of date order; two leads with the same time are ordered by id.
        session.add_all([
            _lead(3),
            _lead(1),
            _lead(2),
            _lead(4, created_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC)),
            _lead(5, created_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC)),
        ])
        await session.commit()

    before = datetime.now(UTC)
    async with session_maker() as session:
        batch = await issue_next_leads(session, 3)

    assert [lead.external_id for lead in batch] == ["2gis_4", "2gis_5", "2gis_1"]
    assert all(lead.crm_status == STATUS_ISSUED and lead.issued_at >= before for lead in batch)
    async with session_maker() as session:
        assert await count_new_leads(session) == 2
        assert await count_issued_leads(session) == 3


async def test_repeated_batches_never_overlap(session_maker) -> None:
    await _add_leads(session_maker, 7)
    seen: list[str] = []
    for expected in (3, 3, 1, 0):
        async with session_maker() as session:
            batch = await issue_next_leads(session, 3)
        assert len(batch) == expected
        seen += [lead.external_id for lead in batch]
    assert seen == [f"2gis_{number}" for number in range(1, 8)]


async def test_concurrent_batches_do_not_hand_out_the_same_lead(session_maker) -> None:
    await _add_leads(session_maker, 40)

    async def take() -> list[str]:
        async with session_maker() as session:
            return [lead.external_id for lead in await issue_next_leads(session, 10)]

    batches = await asyncio.gather(*(take() for _ in range(6)))
    issued = [external_id for batch in batches for external_id in batch]
    assert len(issued) == 40
    assert len(set(issued)) == 40


async def test_other_statuses_are_never_handed_out(session_maker) -> None:
    async with session_maker() as session:
        session.add_all([
            _lead(1, crm_status="in_work"),
            _lead(2, crm_status=STATUS_REJECTED),
            _lead(3, crm_status=STATUS_CONTACTED),
            _lead(4),
        ])
        await session.commit()
    async with session_maker() as session:
        assert [lead.external_id for lead in await issue_next_leads(session, 10)] == ["2gis_4"]


async def test_undelivered_leads_return_to_their_place_in_the_queue(session_maker) -> None:
    await _add_leads(session_maker, 3)
    async with session_maker() as session:
        batch = await issue_next_leads(session, 2)
        await return_undelivered_leads(session, [batch[1].id])
    async with session_maker() as session:
        second = await issue_next_leads(session, 1)
    assert second[0].external_id == "2gis_2"


async def test_status_set_meanwhile_is_not_lost_by_returning_a_lead(session_maker) -> None:
    await _add_leads(session_maker, 1)
    async with session_maker() as session:
        lead_id = (await issue_next_leads(session, 1))[0].id
    async with session_maker() as session:
        lead = await session.get(Lead, lead_id)
        lead.crm_status = STATUS_CONTACTED
        await session.commit()
    async with session_maker() as session:
        await return_undelivered_leads(session, [lead_id])
        assert (await session.get(Lead, lead_id)).crm_status == STATUS_CONTACTED


async def test_status_counts_cover_every_status(session_maker) -> None:
    async with session_maker() as session:
        session.add_all([_lead(1), _lead(2, crm_status=STATUS_ISSUED), _lead(3, crm_status=STATUS_REPLIED)])
        await session.commit()
        assert await get_status_counts(session) == {STATUS_NEW: 1, STATUS_ISSUED: 1, STATUS_REPLIED: 1}


# ---------- 'next batch' button ----------


def _message(user_id: int = 1) -> MagicMock:
    message = MagicMock(spec=Message)
    message.from_user = User(id=user_id, is_bot=False, first_name="Admin")
    message.answer = AsyncMock()
    return message


def _runner(topup: TopupOutcome = TopupOutcome.STARTED) -> MagicMock:
    runner = MagicMock()
    runner.request_topup = AsyncMock(return_value=TopupDecision(topup))
    runner.check_pool = AsyncMock()
    return runner


def _final_text(message: MagicMock) -> str:
    return message.answer.await_args.args[0]


def _cards(message: MagicMock) -> list:
    return [call for call in message.answer.await_args_list if call.kwargs.get("reply_markup") is not None]


@pytest.fixture
def batch_env(session_maker, monkeypatch: pytest.MonkeyPatch):
    """Handlers use the module-level session maker; pauses are removed."""
    monkeypatch.setattr(issue, "async_session_maker", session_maker)
    monkeypatch.setattr(issue, "MESSAGE_PAUSE_SECONDS", 0)
    monkeypatch.setattr(issue, "settings", Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1, LEADS_BATCH_SIZE=3))
    issue._issuing_to.clear()
    return session_maker


async def test_button_sends_a_full_batch_with_status_buttons(batch_env) -> None:
    await _add_leads(batch_env, 5)
    message = _message()
    runner = _runner()

    await issue.handle_next_batch(message, runner)

    cards = _cards(message)
    assert len(cards) == 3
    assert "Организация 1" in cards[0].args[0]
    assert "Организация 3" in cards[2].args[0]
    assert cards[0].kwargs["parse_mode"] == "HTML"
    assert "Выдано лидов: <b>3</b>" in _final_text(message)
    assert "В запасе: <b>2</b>" in _final_text(message)
    runner.request_topup.assert_not_awaited()
    runner.check_pool.assert_awaited_once()


async def test_next_press_gives_new_leads(batch_env) -> None:
    await _add_leads(batch_env, 6)
    first, second = _message(), _message()
    await issue.handle_next_batch(first, _runner())
    await issue.handle_next_batch(second, _runner())
    assert "Организация 1" in _cards(first)[0].args[0]
    assert "Организация 4" in _cards(second)[0].args[0]


async def test_double_tap_does_not_hand_out_a_second_batch(batch_env) -> None:
    await _add_leads(batch_env, 6)
    message = _message()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_answer(*args, **kwargs):
        started.set()
        await release.wait()

    message.answer = AsyncMock(side_effect=slow_answer)
    first = asyncio.create_task(issue.handle_next_batch(message, _runner()))
    await started.wait()

    double_tap = _message()
    await issue.handle_next_batch(double_tap, _runner())
    assert "ещё отправляется" in _final_text(double_tap)
    assert _cards(double_tap) == []

    release.set()
    await first
    async with batch_env() as session:
        assert await count_new_leads(session) == 3  # only one batch left the pool


async def test_guard_is_released_after_an_error(batch_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(issue, "issue_next_leads", AsyncMock(side_effect=RuntimeError("db")))
    with pytest.raises(RuntimeError):
        await issue.handle_next_batch(_message(), _runner())
    assert issue._issuing_to == set()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (TopupOutcome.STARTED, "Запустил сбор"),
        (TopupOutcome.RUNNING, "Сбор уже идёт"),
        (TopupOutcome.AUTO_OFF, "/collect_now"),
        (TopupOutcome.DAILY_LIMIT, "Суточный лимит"),
    ],
)
async def test_short_batch_gives_what_is_left_and_tells_about_collection(batch_env, outcome, expected) -> None:
    await _add_leads(batch_env, 2)
    message = _message()
    runner = _runner(outcome)

    await issue.handle_next_batch(message, runner)

    assert len(_cards(message)) == 2
    assert "Выдано лидов: <b>2</b>" in _final_text(message)
    assert expected in _final_text(message)
    runner.request_topup.assert_awaited_once_with("batch")


async def test_short_batch_during_cooldown_says_when_the_next_collection_is_possible(batch_env) -> None:
    await _add_leads(batch_env, 1)
    message = _message()
    runner = _runner()
    runner.request_topup.return_value = TopupDecision(
        TopupOutcome.COOLDOWN, retry_at=datetime(2026, 11, 5, 12, 30, tzinfo=UTC)
    )
    await issue.handle_next_batch(message, runner)
    assert "15:30" in _final_text(message)


async def test_empty_pool_says_so_and_starts_collection(batch_env) -> None:
    message = _message()
    runner = _runner()
    await issue.handle_next_batch(message, runner)
    assert _cards(message) == []
    assert "Новых лидов в запасе нет" in _final_text(message)
    assert "Запустил сбор" in _final_text(message)
    runner.request_topup.assert_awaited_once()


async def test_network_failure_returns_undelivered_leads_to_the_pool(batch_env) -> None:
    await _add_leads(batch_env, 3)
    message = _message()
    calls = 0

    async def fail_on_second_card(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TelegramNetworkError(method=MagicMock(spec=SendMessage), message="no network")

    message.answer = AsyncMock(side_effect=fail_on_second_card)
    await issue.handle_next_batch(message, _runner())

    async with batch_env() as session:
        assert await count_issued_leads(session) == 1
        assert await count_new_leads(session) == 2
    assert "Не удалось отправить: <b>2</b>" in _final_text(message)


async def test_cancelled_delivery_returns_undelivered_leads(batch_env) -> None:
    await _add_leads(batch_env, 3)
    message = _message()
    started = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    message.answer = AsyncMock(side_effect=hang)
    task = asyncio.create_task(issue.handle_next_batch(message, _runner()))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    async with batch_env() as session:
        assert await count_new_leads(session) == 3


async def test_telegram_flood_wait_is_respected(batch_env, monkeypatch: pytest.MonkeyPatch) -> None:
    await _add_leads(batch_env, 1)
    waits: list[float] = []

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(issue.asyncio, "sleep", fake_sleep)
    message = _message()
    calls = 0

    async def flood_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TelegramRetryAfter(method=MagicMock(spec=SendMessage), message="flood", retry_after=7)

    message.answer = AsyncMock(side_effect=flood_once)
    await issue.handle_next_batch(message, _runner())

    assert waits == [8]
    async with batch_env() as session:
        assert await count_issued_leads(session) == 1


async def test_card_refused_by_telegram_does_not_block_the_rest(batch_env) -> None:
    await _add_leads(batch_env, 3)
    message = _message()

    async def refuse_first(text, **kwargs):
        if "Организация 1" in text:
            raise TelegramBadRequest(method=MagicMock(spec=SendMessage), message="can't parse entities")

    message.answer = AsyncMock(side_effect=refuse_first)
    await issue.handle_next_batch(message, _runner())

    async with batch_env() as session:
        assert await count_issued_leads(session) == 2
        assert await count_new_leads(session) == 1


# ---------- card and status buttons ----------


def test_card_has_every_requested_field_and_escapes_html() -> None:
    lead = _lead(
        1,
        name="<b>Улыбка</b> & Ко",
        address="Невский, 10",
        phone="+78125550101",
        email="info@smile.ru",
        raw_website="https://vk.com/smile",
        site_status="social_only",
        source_url="https://2gis.ru/firm/1?a=1&b=2",
        crm_status=STATUS_ISSUED,
    )
    card = format_issued_lead(lead)
    assert "&lt;b&gt;Улыбка&lt;/b&gt; &amp; Ко" in card
    assert "Стоматологии и клиники" in card
    for value in ("Невский, 10", "+78125550101", "info@smile.ru", "https://vk.com/smile"):
        assert value in card
    assert '<a href="https://2gis.ru/firm/1?a=1&amp;b=2">Карточка 2ГИС</a>' in card


def test_card_without_contacts_or_safe_link() -> None:
    card = format_issued_lead(_lead(1, source_url="javascript:alert(1)", map_url=None))
    assert "href" not in card
    assert "❌ нет" in card


def test_card_never_exceeds_the_telegram_limit() -> None:
    huge = "&" * 3000
    lead = _lead(1, name=huge, address=huge, phone=huge, email=huge, socials=huge, raw_website=huge)
    assert len(format_issued_lead(lead)) <= MAX_MESSAGE_LENGTH


def test_status_keyboard_marks_the_current_status_and_fits_telegram_limits() -> None:
    keyboard = get_lead_status_keyboard(12345678, STATUS_REPLIED)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert [button.text for button in buttons] == ["Написал", "✅ Ответили", "Отказ", "Нет ответа"]
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
    assert set(LEAD_OUTCOMES) == {STATUS_CONTACTED, STATUS_REPLIED, STATUS_REJECTED, STATUS_NO_ANSWER}


def _status_callback(data: str) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    return callback


async def _issued_lead_id(session_maker) -> int:
    await _add_leads(session_maker, 1)
    async with session_maker() as session:
        return (await issue_next_leads(session, 1))[0].id


@pytest.mark.parametrize("status", list(LEAD_OUTCOMES))
async def test_status_button_saves_the_status_and_updates_the_card(batch_env, status: str) -> None:
    lead_id = await _issued_lead_id(batch_env)
    callback = _status_callback(f"ls:{lead_id}:{status}")

    await issue.handle_lead_status(callback)

    async with batch_env() as session:
        lead = await session.get(Lead, lead_id)
    assert lead.crm_status == status
    assert lead.issued_at is not None
    callback.answer.assert_awaited_once_with(f"Статус: {LEAD_OUTCOMES[status]}")
    edit = callback.message.edit_text.await_args
    assert "Организация 1" in edit.args[0]
    marked = [b.text for row in edit.kwargs["reply_markup"].inline_keyboard for b in row if b.text.startswith("✅")]
    assert marked == [f"✅ {LEAD_OUTCOMES[status]}"]


async def test_status_can_be_changed_and_same_status_is_no_error(batch_env) -> None:
    lead_id = await _issued_lead_id(batch_env)
    await issue.handle_lead_status(_status_callback(f"ls:{lead_id}:{STATUS_CONTACTED}"))
    await issue.handle_lead_status(_status_callback(f"ls:{lead_id}:{STATUS_REPLIED}"))

    again = _status_callback(f"ls:{lead_id}:{STATUS_REPLIED}")
    await issue.handle_lead_status(again)
    again.message.edit_text.assert_not_awaited()
    async with batch_env() as session:
        assert (await session.get(Lead, lead_id)).crm_status == STATUS_REPLIED


@pytest.mark.parametrize(
    "data",
    ["ls:1", "ls:abc:contacted", "ls:1:new", "ls:1:issued", "ls:1:drop table", "ls:999:contacted"],
)
async def test_forged_or_stale_status_buttons_are_rejected(batch_env, data: str) -> None:
    await _issued_lead_id(batch_env)
    callback = _status_callback(data)
    await issue.handle_lead_status(callback)
    callback.answer.assert_awaited_once_with(STALE_BUTTON_TEXT, show_alert=True)
    async with batch_env() as session:
        assert (await session.get(Lead, 1)).crm_status == STATUS_ISSUED


async def test_status_of_a_lead_not_handed_out_cannot_be_set(batch_env) -> None:
    await _add_leads(batch_env, 1)
    callback = _status_callback(f"ls:1:{STATUS_CONTACTED}")
    await issue.handle_lead_status(callback)
    callback.answer.assert_awaited_once_with(STALE_BUTTON_TEXT, show_alert=True)
    async with batch_env() as session:
        assert await count_new_leads(session) == 1


async def test_status_is_saved_even_if_the_old_card_cannot_be_edited(batch_env) -> None:
    lead_id = await _issued_lead_id(batch_env)
    callback = _status_callback(f"ls:{lead_id}:{STATUS_REPLIED}")
    callback.message.edit_text.side_effect = TelegramBadRequest(
        method=MagicMock(spec=SendMessage), message="message can't be edited"
    )
    await issue.handle_lead_status(callback)
    async with batch_env() as session:
        assert (await session.get(Lead, lead_id)).crm_status == STATUS_REPLIED


# ---------- statistics and other messages ----------


def test_funnel_stats_text() -> None:
    counts = {
        STATUS_NEW: 5,
        STATUS_ISSUED: 2,
        STATUS_CONTACTED: 3,
        STATUS_REPLIED: 1,
        STATUS_REJECTED: 4,
        STATUS_NO_ANSWER: 6,
    }
    text_ = format_funnel_stats(counts, issued_total=16)
    for line in (
        "Всего: <b>21</b>", "в запасе: <b>5</b>", "Выдано всего: <b>16</b>", "ждут действия: <b>2</b>",
        "Написал: <b>3</b>", "Ответили: <b>1</b>", "Отказ: <b>4</b>", "Нет ответа: <b>6</b>",
    ):
        assert line in text_
    assert "В работе" not in text_


async def test_stats_command_counts_from_the_database(batch_env) -> None:
    lead_id = await _issued_lead_id(batch_env)
    await _add_leads(batch_env, 2, start=10)
    await issue.handle_lead_status(_status_callback(f"ls:{lead_id}:{STATUS_CONTACTED}"))

    message = _message()
    await issue.handle_leads_stats(message)

    sent = message.answer.await_args
    assert "Всего: <b>3</b>" in sent.args[0]
    assert "в запасе: <b>2</b>" in sent.args[0]
    assert "Выдано всего: <b>1</b>" in sent.args[0]
    assert "Написал: <b>1</b>" in sent.args[0]
    assert sent.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "export:all"


async def test_existing_stats_button_total_includes_handed_out_leads(session_maker, monkeypatch) -> None:
    monkeypatch.setattr(leads, "async_session_maker", session_maker)
    async with session_maker() as session:
        session.add_all([_lead(1), _lead(2, crm_status=STATUS_CONTACTED), _lead(3, crm_status="in_work")])
        await session.commit()
    message = _message()
    await leads.handle_stats(message)
    text_ = message.answer.await_args.args[0]
    assert "Новые: <b>1</b>" in text_
    assert "В работе: <b>1</b>" in text_
    assert "Всего в базе: <b>3</b>" in text_


async def test_csv_export_gets_new_columns_at_the_end(session_maker, monkeypatch) -> None:
    monkeypatch.setattr(leads, "async_session_maker", session_maker)
    await _add_leads(session_maker, 1)
    async with session_maker() as session:
        await issue_next_leads(session, 1)
    callback = _status_callback("export:all")
    callback.message.answer_document = AsyncMock()

    await leads.handle_export_csv(callback)

    document = callback.message.answer_document.await_args.kwargs["document"]
    header, row = list(csv.reader(io.StringIO(document.data.decode("utf-8-sig")), delimiter=";"))[:2]
    assert header[:2] == ["ID", "Источник"]
    assert header[-3:] == ["Email", "Карточка источника", "Дата выдачи (МСК)"]
    assert row[-1] != ""


def test_main_menu_has_the_batch_button() -> None:
    texts = [button.text for row in get_main_menu_keyboard().keyboard for button in row]
    assert f"📦 Следующие {settings.LEADS_BATCH_SIZE}" in texts


def test_collection_report_escapes_error_text() -> None:
    report = MagicMock(
        status="partial",
        result=CollectResult(found=3, saved_new=1, errors=1, last_error="<ошибка> & код"),
        duration_seconds=12.4,
        pool_size=7,
    )
    text_ = format_collection_report(report)
    assert "&lt;ошибка&gt; &amp; код" in text_
    assert "Новых сохранено: <b>1</b>" in text_
    assert "В запасе: <b>7</b>" in text_


def test_collector_status_before_the_first_run_and_after_it() -> None:
    state = CollectorState(id=1, found=0, qualified=0, saved_new=0, duplicates=0, errors=0)
    status = MagicMock(
        state=state,
        auto_enabled=True,
        is_running=False,
        next_run_at=datetime(2026, 11, 6, 1, 0, tzinfo=UTC),
        schedule_text="cron 0 4 * * * (Europe/Moscow)",
        pool_size=10,
        active_providers=["osm"],
        disabled_providers={"2gis": "не задан TWOGIS_API_KEY"},
        requests_today=3,
        max_requests_per_day=200,
    )
    text_ = format_collector_status(status)
    assert "06.11.2026 04:00 МСК" in text_
    assert "не задан TWOGIS_API_KEY" in text_
    assert "Сбор ещё не запускался" in text_

    state.last_started_at = datetime(2026, 11, 5, 1, 0, tzinfo=UTC)
    state.last_status, state.last_trigger, state.saved_new = "ok", "schedule", 4
    state.found, state.duplicates, state.errors = 9, 2, 1
    state.last_error = "<сеть>"
    text_ = format_collector_status(status)
    assert "по расписанию" in text_ and "успешно" in text_
    assert "новых: <b>4</b>" in text_ and "&lt;сеть&gt;" in text_


async def test_notifier_sends_alerts_and_summary_to_all_admins_silently_at_night() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    report = MagicMock(
        trigger="schedule",
        status="ok",
        result=CollectResult(saved_new=2),
        duration_seconds=1.0,
        pool_size=2,
        new_alerts=["ключ <заблокирован>"],
        announce=True,
    )
    await AdminNotifier(bot, [2, 1]).report(report)

    calls = bot.send_message.await_args_list
    assert [call.args[0] for call in calls] == [1, 2, 1, 2]
    assert "&lt;заблокирован&gt;" in calls[0].args[1]
    assert calls[0].kwargs["disable_notification"] is False
    assert calls[2].kwargs["disable_notification"] is True


async def test_notifier_survives_one_unreachable_admin() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[
        TelegramNetworkError(method=MagicMock(spec=SendMessage), message="down"), None,
    ])
    await AdminNotifier(bot, [1, 2]).send("текст")
    assert bot.send_message.await_count == 2


async def test_collect_commands_answer_and_switch() -> None:
    runner = MagicMock()
    runner.start_in_background = MagicMock(side_effect=[True, False])
    runner.set_auto_enabled = AsyncMock()

    message = _message()
    await collector_handlers.handle_collect_now(message, runner)
    assert "запущен" in _final_text(message)
    await collector_handlers.handle_collect_now(message, runner)
    assert "уже выполняется" in _final_text(message)

    await collector_handlers.handle_collect_on(message, runner)
    runner.set_auto_enabled.assert_awaited_with(True)
    await collector_handlers.handle_collect_off(message, runner)
    runner.set_auto_enabled.assert_awaited_with(False)


# ---------- access control ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", {100}), ("200, 300", {100, 200, 300}), ("100", {100}), (" 200 ,, ", {100, 200})],
)
def test_admin_ids_from_settings(raw: str, expected: set[int]) -> None:
    assert Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=100, ADMIN_TG_IDS=raw).admin_ids == expected


def test_invalid_admin_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="ADMIN_TG_IDS"):
        Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=100, ADMIN_TG_IDS="100,@admin")


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.id = 1
    return bot


def _update(update_id: int, user_id: int, message_text: str) -> Update:
    entities = None
    if message_text.startswith("/"):
        entities = [{"type": "bot_command", "offset": 0, "length": len(message_text)}]
    message = Message(
        message_id=update_id,
        date=datetime(2026, 11, 5, tzinfo=UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        text=message_text,
        entities=entities,
    )
    return Update(update_id=update_id, message=message)


def _callback_update(update_id: int, user_id: int, data: str) -> Update:
    callback = CallbackQuery(
        id=str(update_id),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        chat_instance="c",
        data=data,
    )
    return Update(update_id=update_id, callback_query=callback)


@pytest.fixture
def guarded_dispatcher(monkeypatch: pytest.MonkeyPatch) -> Dispatcher:
    """The dispatcher as main.py builds it, where only ADMIN_TG_ID=100 and ADMIN_TG_IDS=200 are admins."""
    monkeypatch.setattr(
        "src.bot.middlewares.settings",
        Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=100, ADMIN_TG_IDS="200"),
    )
    monkeypatch.setattr(Message, "answer", AsyncMock())
    dp = Dispatcher()
    guard = AdminOnlyMiddleware()
    dp.message.middleware(guard)
    dp.callback_query.middleware(guard)
    # Routers are singletons: detach them again so that another test can include them.
    routers = (issue.router, collector_handlers.router)
    for router in routers:
        dp.include_router(router)
    yield dp
    for router in routers:
        router._parent_router = None
    dp.sub_routers.clear()


@pytest.mark.parametrize(("user_id", "allowed"), [(100, True), (200, True), (999, False)])
async def test_commands_reach_only_admins_through_the_real_dispatcher(guarded_dispatcher, user_id, allowed) -> None:
    runner = MagicMock()
    runner.start_in_background = MagicMock(return_value=True)
    await guarded_dispatcher.feed_update(_bot(), _update(1, user_id, "/collect_now"), collector_runner=runner)
    assert runner.start_in_background.called is allowed


async def test_stranger_cannot_take_leads_or_set_statuses(guarded_dispatcher, monkeypatch) -> None:
    def must_not_run(*args, **kwargs):
        raise AssertionError("a stranger's update reached the database")

    monkeypatch.setattr(issue, "issue_next_leads", must_not_run)
    monkeypatch.setattr(issue, "async_session_maker", must_not_run)
    bot = _bot()
    runner = MagicMock()
    updates = (
        _update(1, 999, "📦 Следующие 30"),
        _update(2, 999, "/next_leads"),
        _update(3, 999, "/leads_stats"),
        _callback_update(4, 999, f"ls:1:{STATUS_CONTACTED}"),
    )
    for update in updates:
        await guarded_dispatcher.feed_update(bot, update, collector_runner=runner)


async def test_batch_button_with_any_size_reaches_the_handler(guarded_dispatcher, monkeypatch) -> None:
    """A keyboard sent before LEADS_BATCH_SIZE was changed must keep working."""
    calls = []

    async def fake_issue(message, runner):
        calls.append(message.text)

    monkeypatch.setattr(issue, "_issue_batch", fake_issue)
    issue._issuing_to.clear()
    for number, text_ in enumerate(("📦 Следующие 30", "📦 Следующие 50", "/next_leads", "📦 Следующие")):
        await guarded_dispatcher.feed_update(_bot(), _update(number, 100, text_), collector_runner=MagicMock())
    assert calls == ["📦 Следующие 30", "📦 Следующие 50", "/next_leads"]


# ---------- schema upgrade ----------

OLD_SCHEMA = [
    """
    CREATE TABLE leads (
        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        external_id VARCHAR(128) NOT NULL,
        source VARCHAR(32) NOT NULL,
        name VARCHAR(255) NOT NULL,
        category VARCHAR(128) NOT NULL,
        district VARCHAR(128),
        address VARCHAR(512),
        phone VARCHAR(128),
        socials VARCHAR(512),
        raw_website VARCHAR(512),
        site_status VARCHAR(32) NOT NULL,
        score INTEGER NOT NULL,
        crm_status VARCHAR(32) NOT NULL,
        map_url VARCHAR(512),
        created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX ix_leads_external_id ON leads (external_id)",
    """
    INSERT INTO leads (external_id, source, name, category, site_status, score, crm_status)
    VALUES ('osm_1', 'osm', 'Старый лид', 'dentist_clinic', 'no_site', 80, 'new'),
           ('osm_2', 'osm', 'В работе', 'dentist_clinic', 'no_site', 80, 'in_work')
    """,
]

# The rollback documented in README.md.
ROLLBACK_SCHEMA = [
    "UPDATE leads SET crm_status = 'in_work' WHERE crm_status IN ('issued', 'contacted', 'replied', 'no_answer')",
    "DROP INDEX ix_leads_crm_status_created_at",
    "ALTER TABLE leads DROP COLUMN email",
    "ALTER TABLE leads DROP COLUMN source_url",
    "ALTER TABLE leads DROP COLUMN issued_at",
    "DROP TABLE collector_state",
]


async def _columns(engine) -> set[str]:
    async with engine.connect() as conn:
        return {row[1] for row in await conn.execute(text("PRAGMA table_info(leads)"))}


async def _tables(engine) -> set[str]:
    async with engine.connect() as conn:
        return {row[0] for row in await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}


@pytest.fixture
async def old_database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    async with engine.begin() as conn:
        for statement in OLD_SCHEMA:
            await conn.execute(text(statement))
    yield engine
    await engine.dispose()


async def test_upgrade_keeps_data_is_repeatable_and_old_leads_join_the_queue(old_database) -> None:
    await init_db(old_database)
    await init_db(old_database)  # a restart must not fail

    assert {"email", "source_url", "issued_at"} <= await _columns(old_database)
    assert "collector_state" in await _tables(old_database)

    session_maker = async_sessionmaker(old_database, expire_on_commit=False, class_=AsyncSession)
    async with session_maker() as session:
        batch = await issue_next_leads(session, 10)
    assert [lead.name for lead in batch] == ["Старый лид"]  # 'in_work' stays where it is
    assert batch[0].email is None


async def test_documented_rollback_restores_the_old_schema(old_database) -> None:
    before = await _columns(old_database)
    await init_db(old_database)
    session_maker = async_sessionmaker(old_database, expire_on_commit=False, class_=AsyncSession)
    async with session_maker() as session:
        await issue_next_leads(session, 1)

    async with old_database.begin() as conn:
        for statement in ROLLBACK_SCHEMA:
            await conn.execute(text(statement))

    assert await _columns(old_database) == before
    assert "collector_state" not in await _tables(old_database)
    async with old_database.connect() as conn:
        rows = (await conn.execute(text("SELECT crm_status FROM leads ORDER BY id"))).scalars().all()
    assert rows == ["in_work", "in_work"]

    await init_db(old_database)  # and it can be upgraded again
    assert {"email", "source_url", "issued_at"} <= await _columns(old_database)
