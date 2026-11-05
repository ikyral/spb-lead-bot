"""Tests for working in group chats: who may do what, where messages go, and what Telegram can answer."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from src.bot import notifier as notifier_module
from src.bot.chats import is_group_chat
from src.bot.formatters import format_issued_lead, format_user_label
from src.bot.handlers import collector as collector_handlers
from src.bot.handlers import issue, leads, search
from src.bot.keyboards import get_group_panel_keyboard
from src.bot.middlewares import NO_ACCESS_TEXT, AdminOnlyMiddleware
from src.bot.notifier import AdminNotifier
from src.config import Settings
from src.database.models import CONTACT_CONTACTED, STATUS_ISSUED, STATUS_NEW, Lead
from src.database.repo import count_issued_leads, count_new_leads, issue_next_leads
from src.services.collector import CollectResult
from src.services.scheduler import TopupDecision, TopupOutcome

ADMIN, SECOND_ADMIN, STRANGER = 100, 200, 999
GROUP, OTHER_GROUP = -1001, -1002
METHOD = MagicMock(spec=SendMessage)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=ADMIN, ADMIN_TG_IDS=str(SECOND_ADMIN), **overrides)


# ---------- settings ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("-1001234567890", (-1001234567890,)),
        (" -100, -200 ,, 300", (-100, -200, 300)),
        ("-100,-100", (-100,)),
    ],
)
def test_notify_chat_ids_are_parsed(raw: str, expected: tuple[int, ...]) -> None:
    assert _settings(NOTIFY_CHAT_IDS=raw).notify_chat_ids == expected


@pytest.mark.parametrize("raw", ["abc", "-", "--100", "-100;-200", "0", "@mygroup", "-100.5"])
def test_invalid_notify_chat_ids_name_the_variable(raw: str) -> None:
    with pytest.raises(ValueError, match="NOTIFY_CHAT_IDS"):
        _settings(NOTIFY_CHAT_IDS=raw)


def test_private_notifications_are_on_by_default() -> None:
    assert _settings().NOTIFY_ADMINS_PRIVATE is True


# ---------- notifications ----------


def _bot(side_effect=None) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=side_effect)
    return bot


def _sent_to(bot: MagicMock) -> list[int]:
    return [call.args[0] for call in bot.send_message.await_args_list]


async def test_without_chats_only_admins_get_notifications_as_before() -> None:
    bot = _bot()
    await AdminNotifier(bot, [ADMIN, SECOND_ADMIN]).send("итог")
    assert _sent_to(bot) == [ADMIN, SECOND_ADMIN]


async def test_notifications_go_to_all_chats_and_admins() -> None:
    bot = _bot()
    await AdminNotifier(bot, [ADMIN], [GROUP, OTHER_GROUP], notify_admins=True).send("итог", silent=True)
    assert sorted(_sent_to(bot)) == sorted([GROUP, OTHER_GROUP, ADMIN])
    assert all(call.kwargs["disable_notification"] is True for call in bot.send_message.await_args_list)


async def test_private_copies_can_be_switched_off() -> None:
    bot = _bot()
    await AdminNotifier(bot, [ADMIN], [GROUP], notify_admins=False).send("итог")
    assert _sent_to(bot) == [GROUP]


async def test_without_chats_the_private_switch_cannot_silence_everything() -> None:
    bot = _bot()
    await AdminNotifier(bot, [ADMIN], [], notify_admins=False).send("блокировка провайдера")
    assert _sent_to(bot) == [ADMIN]


async def test_the_same_id_is_not_notified_twice() -> None:
    bot = _bot()
    await AdminNotifier(bot, [ADMIN], [ADMIN, GROUP]).send("итог")
    assert sorted(_sent_to(bot)) == sorted([ADMIN, GROUP])


@pytest.mark.parametrize(
    "error",
    [
        TelegramForbiddenError(method=METHOD, message="bot was kicked from the group chat"),
        TelegramBadRequest(method=METHOD, message="chat not found"),
        TelegramNetworkError(method=METHOD, message="no network"),
        RuntimeError("something unexpected"),
    ],
)
async def test_failure_in_one_chat_does_not_affect_others_and_does_not_raise(error: Exception) -> None:
    async def fail_for_first_group(chat_id, *args, **kwargs):
        if chat_id == GROUP:
            raise error

    bot = _bot(fail_for_first_group)
    await AdminNotifier(bot, [ADMIN], [GROUP, OTHER_GROUP]).send("итог")
    assert sorted(_sent_to(bot)) == sorted([GROUP, OTHER_GROUP, ADMIN])


async def test_report_with_alerts_reaches_chats_and_survives_a_broken_one() -> None:
    async def fail_for_first_group(chat_id, *args, **kwargs):
        if chat_id == GROUP:
            raise TelegramForbiddenError(method=METHOD, message="kicked")

    bot = _bot(fail_for_first_group)
    report = MagicMock(
        trigger="schedule",
        status="partial",
        result=CollectResult(saved_new=1),
        duration_seconds=1.0,
        pool_size=1,
        new_alerts=["2gis_scraper: сайт показал капчу"],
        announce=True,
    )
    await AdminNotifier(bot, [], [GROUP, OTHER_GROUP]).report(report)

    to_other_group = [call.args[1] for call in bot.send_message.await_args_list if call.args[0] == OTHER_GROUP]
    assert len(to_other_group) == 2
    assert "капчу" in to_other_group[0]
    assert "Сбор лидов завершён" in to_other_group[1]


async def test_migrated_group_gets_the_message_under_its_new_id_from_then_on(caplog) -> None:
    new_id = -100555

    async def old_id_is_gone(chat_id, *args, **kwargs):
        if chat_id == GROUP:
            raise TelegramMigrateToChat(method=METHOD, message="migrated", migrate_to_chat_id=new_id)

    bot = _bot(old_id_is_gone)
    notifier = AdminNotifier(bot, [], [GROUP, OTHER_GROUP])

    await notifier.send("итог")

    assert _sent_to(bot).count(GROUP) == 1
    to_new = [call.args[1] for call in bot.send_message.await_args_list if call.args[0] == new_id]
    assert to_new[0] == "итог"
    assert str(new_id) in to_new[1]  # the chat is told its new ID
    assert str(new_id) in caplog.text and "NOTIFY_CHAT_IDS" in caplog.text

    bot.send_message.reset_mock()
    await notifier.send("следующий итог")
    assert sorted(_sent_to(bot)) == sorted([OTHER_GROUP, new_id])


async def test_migration_to_a_chat_already_in_the_list_does_not_duplicate() -> None:
    async def old_id_is_gone(chat_id, *args, **kwargs):
        if chat_id == GROUP:
            raise TelegramMigrateToChat(method=METHOD, message="migrated", migrate_to_chat_id=OTHER_GROUP)

    bot = _bot(old_id_is_gone)
    await AdminNotifier(bot, [], [GROUP, OTHER_GROUP]).send("итог")
    assert _sent_to(bot).count(OTHER_GROUP) == 1


async def test_flood_wait_is_respected_without_holding_up_other_chats(monkeypatch) -> None:
    waits: list[float] = []
    release = asyncio.Event()
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)
        await release.wait()

    monkeypatch.setattr(notifier_module.asyncio, "sleep", fake_sleep)
    attempts = {GROUP: 0}

    async def flood_once(chat_id, *args, **kwargs):
        if chat_id == GROUP:
            attempts[GROUP] += 1
            if attempts[GROUP] == 1:
                raise TelegramRetryAfter(method=METHOD, message="flood", retry_after=7)

    bot = _bot(flood_once)
    task = asyncio.ensure_future(AdminNotifier(bot, [], [GROUP, OTHER_GROUP]).send("итог"))
    for _ in range(20):
        await real_sleep(0)
    assert OTHER_GROUP in _sent_to(bot)  # the other chat did not wait
    assert attempts[GROUP] == 1
    release.set()
    await task
    assert waits == [8]
    assert attempts[GROUP] == 2


async def test_very_long_flood_wait_drops_the_message_instead_of_holding_up_the_run(monkeypatch) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr(notifier_module.asyncio, "sleep", sleep)
    bot = _bot(TelegramRetryAfter(method=METHOD, message="flood", retry_after=3600))
    await AdminNotifier(bot, [], [GROUP]).send("итог")
    sleep.assert_not_awaited()
    assert bot.send_message.await_count == 1


# ---------- who may do what, through the real dispatcher ----------


def _dp_bot() -> MagicMock:
    bot = MagicMock()
    bot.id = 1
    # /cmd@name in a group is compared with the bot's own username.
    bot.me = AsyncMock(return_value=User(id=1, is_bot=True, first_name="Bot", username="spb_lead_bot"))
    return bot


def _chat(chat_id: int) -> Chat:
    return Chat(id=chat_id, type="supergroup" if chat_id < 0 else "private")


def _message_update(update_id: int, chat_id: int, user_id: int, text: str | None) -> Update:
    entities = None
    if text and text.startswith("/"):
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    message = Message(
        message_id=update_id,
        date=datetime(2026, 11, 5, tzinfo=UTC),
        chat=_chat(chat_id),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        text=text,
        entities=entities,
    )
    return Update(update_id=update_id, message=message)


def _callback_update(update_id: int, chat_id: int, user_id: int, data: str) -> Update:
    message = Message(message_id=1, date=datetime(2026, 11, 5, tzinfo=UTC), chat=_chat(chat_id), text="карточка")
    callback = CallbackQuery(
        id=str(update_id),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        chat_instance="c",
        data=data,
        message=message,
    )
    return Update(update_id=update_id, callback_query=callback)


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """The dispatcher as main.py builds it, with Telegram calls recorded."""
    monkeypatch.setattr("src.bot.middlewares.settings", _settings())
    calls = MagicMock()
    calls.answer = AsyncMock()
    calls.reply = AsyncMock()
    calls.callback_answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", calls.answer)
    monkeypatch.setattr(Message, "reply", calls.reply)
    monkeypatch.setattr(CallbackQuery, "answer", calls.callback_answer)

    dp = Dispatcher()
    guard = AdminOnlyMiddleware()
    dp.message.middleware(guard)
    dp.callback_query.middleware(guard)
    routers = (search.router, leads.router, issue.router, collector_handlers.router)
    for router in routers:
        dp.include_router(router)
    calls.dp = dp
    calls.runner = MagicMock()
    calls.runner.external_mode = False
    calls.runner.start_in_background = MagicMock(return_value=True)
    calls.runner.request_topup = AsyncMock(return_value=TopupDecision(TopupOutcome.COOLDOWN))
    calls.runner.check_pool = AsyncMock()
    issue._issuing_to.clear()
    yield calls
    for router in routers:
        router._parent_router = None
    dp.sub_routers.clear()


async def _feed(telegram: MagicMock, update: Update, bot: MagicMock | None = None) -> None:
    await telegram.dp.feed_update(bot or _dp_bot(), update, collector_runner=telegram.runner)


@pytest.mark.parametrize(
    ("chat_id", "user_id", "allowed"),
    [
        (ADMIN, ADMIN, True),  # admin in private
        (GROUP, ADMIN, True),  # admin in a group
        (GROUP, SECOND_ADMIN, True),  # a second admin in a group
        (STRANGER, STRANGER, False),  # stranger in private
        (GROUP, STRANGER, False),  # ordinary member of a group
    ],
)
async def test_commands_obey_only_admins_anywhere(telegram, chat_id: int, user_id: int, allowed: bool) -> None:
    await _feed(telegram, _message_update(1, chat_id, user_id, "/collect_now"))
    assert telegram.runner.start_in_background.called is allowed


async def test_chat_id_of_the_group_is_not_an_admin_id(telegram) -> None:
    """A chat listed nowhere, and a group whose ID looks like an admin's: only the user counts."""
    await _feed(telegram, _message_update(1, -ADMIN, STRANGER, "/collect_now"))
    assert not telegram.runner.start_in_background.called


async def test_stranger_in_a_group_gets_a_short_refusal_without_flooding(telegram) -> None:
    for update_id in range(1, 6):
        await _feed(telegram, _message_update(update_id, GROUP, STRANGER, "/collect_now"))
    telegram.reply.assert_awaited_once_with(NO_ACCESS_TEXT)
    telegram.answer.assert_not_awaited()


async def test_refusal_is_repeated_only_after_the_cooldown() -> None:
    now = [0.0]
    guard = AdminOnlyMiddleware(clock=lambda: now[0])
    message = MagicMock(spec=Message)
    message.from_user = User(id=STRANGER, is_bot=False, first_name="U")
    message.chat = _chat(GROUP)
    message.text = "/collect_now"
    message.reply = AsyncMock()
    handler = AsyncMock()

    await guard(handler, message, {})
    await guard(handler, message, {})
    assert message.reply.await_count == 1
    now[0] = 301.0
    await guard(handler, message, {})
    assert message.reply.await_count == 2
    handler.assert_not_awaited()


async def test_each_member_is_answered_separately(telegram) -> None:
    await _feed(telegram, _message_update(1, GROUP, STRANGER, "/collect_now"))
    await _feed(telegram, _message_update(2, GROUP, STRANGER + 1, "/collect_now"))
    assert telegram.reply.await_count == 2


async def test_stranger_in_private_gets_no_answer_as_before(telegram) -> None:
    await _feed(telegram, _message_update(1, STRANGER, STRANGER, "/collect_now"))
    telegram.reply.assert_not_awaited()
    telegram.answer.assert_not_awaited()


async def test_ordinary_group_messages_are_ignored_completely(telegram) -> None:
    """With privacy mode off the bot receives everything, and must not react."""
    for update_id, text in enumerate(("привет всем", "сколько лидов?", "/unknown", "/collect_now@other_bot")):
        await _feed(telegram, _message_update(update_id, GROUP, STRANGER, text))
    telegram.reply.assert_not_awaited()
    telegram.answer.assert_not_awaited()
    assert not telegram.runner.start_in_background.called


async def test_stranger_uploading_a_file_to_a_group_is_not_answered(telegram) -> None:
    """A CSV matches the import handler, but a refusal to every file would be spam."""
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime(2026, 11, 5, tzinfo=UTC),
            chat=_chat(GROUP),
            from_user=User(id=STRANGER, is_bot=False, first_name="U"),
            document={"file_id": "f", "file_unique_id": "u", "file_name": "list.csv"},
        ),
    )
    await _feed(telegram, update)
    telegram.reply.assert_not_awaited()
    telegram.answer.assert_not_awaited()


async def test_commands_addressed_to_this_bot_work_in_a_group(telegram) -> None:
    await _feed(telegram, _message_update(1, GROUP, ADMIN, "/collect_now@spb_lead_bot"))
    assert telegram.runner.start_in_background.called


async def test_commands_addressed_to_another_bot_are_not_ours(telegram) -> None:
    await _feed(telegram, _message_update(1, GROUP, ADMIN, "/collect_now@other_bot"))
    assert not telegram.runner.start_in_background.called
    telegram.reply.assert_not_awaited()


async def test_stranger_addressing_this_bot_is_refused(telegram) -> None:
    await _feed(telegram, _message_update(1, GROUP, STRANGER, "/collect_now@spb_lead_bot"))
    assert not telegram.runner.start_in_background.called
    telegram.reply.assert_awaited_once_with(NO_ACCESS_TEXT)


@pytest.mark.parametrize(
    "data",
    [
        "panel:next",
        "panel:collect",
        "panel:status",
        "panel:stats",
        f"ls:1:{CONTACT_CONTACTED}",
        "lead:1:work:0",
        "export:all",
    ],
)
async def test_stranger_pressing_any_button_gets_an_alert_and_nothing_happens(telegram, monkeypatch, data: str) -> None:
    def must_not_run(*args, **kwargs):
        raise AssertionError("a stranger's button reached the database")

    monkeypatch.setattr(issue, "async_session_maker", must_not_run)
    monkeypatch.setattr("src.bot.handlers.leads.async_session_maker", must_not_run)

    await _feed(telegram, _callback_update(1, GROUP, STRANGER, data))

    telegram.callback_answer.assert_awaited_once_with(NO_ACCESS_TEXT, show_alert=True)
    telegram.answer.assert_not_awaited()
    assert not telegram.runner.start_in_background.called


async def test_refusal_that_telegram_rejects_does_not_raise(telegram) -> None:
    telegram.callback_answer.side_effect = TelegramBadRequest(method=METHOD, message="query is too old")
    await _feed(telegram, _callback_update(1, GROUP, STRANGER, "panel:next"))


@pytest.mark.parametrize(
    ("data", "called"),
    [("panel:collect", "start_in_background"), ("panel:status", "get_status")],
)
async def test_admin_can_use_the_panel_in_a_group(telegram, monkeypatch, data: str, called: str) -> None:
    telegram.runner.get_status = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(collector_handlers, "format_collector_status", lambda status: "статус")
    await _feed(telegram, _callback_update(1, GROUP, ADMIN, data))
    assert getattr(telegram.runner, called).called
    telegram.answer.assert_awaited_once()


async def test_start_in_a_group_shows_inline_buttons_not_the_bottom_keyboard(telegram) -> None:
    await _feed(telegram, _message_update(1, GROUP, ADMIN, "/start"))
    markup = telegram.answer.await_args.kwargs["reply_markup"]
    data = {button.callback_data for row in markup.inline_keyboard for button in row}
    assert data == {"panel:next", "panel:stats", "panel:status", "panel:collect"}


async def test_start_in_private_keeps_the_bottom_keyboard(telegram) -> None:
    await _feed(telegram, _message_update(1, ADMIN, ADMIN, "/start"))
    markup = telegram.answer.await_args.kwargs["reply_markup"]
    assert hasattr(markup, "keyboard")


async def test_chat_id_command_shows_the_id_of_this_group(telegram) -> None:
    await _feed(telegram, _message_update(1, GROUP, ADMIN, "/chat_id"))
    assert str(GROUP) in telegram.answer.await_args.args[0]


def test_panel_callback_data_fits_telegram_limits() -> None:
    for row in get_group_panel_keyboard().inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


def test_chat_kinds() -> None:
    message = MagicMock(spec=Message)
    for chat_type, expected in (("group", True), ("supergroup", True), ("private", False), ("channel", False)):
        message.chat = Chat(id=1, type=chat_type)
        assert is_group_chat(message) is expected


# ---------- handing out leads ----------


def _lead(number: int) -> Lead:
    return Lead(
        external_id=f"2gis_{number}",
        source="2gis",
        name=f"Организация {number}",
        category="dentist_clinic",
        site_status="no_site",
        score=50,
        crm_status=STATUS_NEW,
        created_at=datetime(2026, 11, 5, 10, 0, tzinfo=UTC) + timedelta(minutes=number),
    )


def _group_message(chat_id: int = GROUP, user_id: int = ADMIN) -> MagicMock:
    message = MagicMock(spec=Message)
    message.from_user = User(id=user_id, is_bot=False, first_name="Admin")
    message.chat = _chat(chat_id)
    message.answer = AsyncMock()
    message.bot = MagicMock()
    message.bot.send_message = AsyncMock()
    return message


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.request_topup = AsyncMock(return_value=TopupDecision(TopupOutcome.STARTED))
    runner.check_pool = AsyncMock()
    return runner


def _cards(message: MagicMock) -> list[str]:
    cards = []
    for call in message.answer.await_args_list:
        markup = call.kwargs.get("reply_markup")
        if markup is not None and markup.inline_keyboard[0][0].callback_data.startswith("ls:"):
            cards.append(call.args[0])
    return cards


@pytest.fixture
def batch_env(session_maker, monkeypatch: pytest.MonkeyPatch):
    pauses: list[float] = []
    real_sleep = asyncio.sleep

    async def record_pause(delay: float) -> None:
        pauses.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(issue, "async_session_maker", session_maker)
    monkeypatch.setattr(issue.asyncio, "sleep", record_pause)
    monkeypatch.setattr(issue, "settings", _settings(LEADS_BATCH_SIZE=3))
    issue._issuing_to.clear()
    session_maker.pauses = pauses
    return session_maker


async def _add_leads(session_maker, count: int) -> None:
    async with session_maker() as session:
        session.add_all(_lead(number) for number in range(1, count + 1))
        await session.commit()


async def test_batch_goes_to_the_chat_where_the_button_was_pressed(batch_env) -> None:
    await _add_leads(batch_env, 3)
    message = _group_message()

    await issue.handle_next_batch(message, _runner())

    assert len(_cards(message)) == 3
    message.bot.send_message.assert_not_awaited()  # nothing goes anywhere else
    async with batch_env() as session:
        assert await count_issued_leads(session) == 3


async def test_group_gets_group_pauses_and_a_warning_about_the_time(batch_env) -> None:
    await _add_leads(batch_env, 3)
    message = _group_message()
    await issue.handle_next_batch(message, _runner())
    assert batch_env.pauses == [issue.GROUP_MESSAGE_PAUSE_SECONDS] * 2
    assert issue.GROUP_MESSAGE_PAUSE_SECONDS * 20 >= 60  # at most 20 messages a minute
    assert "⏳" in message.answer.await_args_list[0].args[0]


async def test_private_chat_keeps_its_faster_pace(batch_env) -> None:
    await _add_leads(batch_env, 3)
    message = _group_message(chat_id=ADMIN)
    await issue.handle_next_batch(message, _runner())
    assert batch_env.pauses == [issue.MESSAGE_PAUSE_SECONDS] * 2
    assert len(message.answer.await_args_list) == 4  # no warning about the time


async def test_closing_message_in_a_group_offers_the_next_batch(batch_env) -> None:
    await _add_leads(batch_env, 3)
    message = _group_message()
    await issue.handle_next_batch(message, _runner())
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "panel:next"


async def test_presses_from_different_chats_at_once_do_not_overlap(batch_env) -> None:
    await _add_leads(batch_env, 9)
    messages = [_group_message(GROUP), _group_message(OTHER_GROUP, SECOND_ADMIN), _group_message(ADMIN)]

    await asyncio.gather(*(issue.handle_next_batch(message, _runner()) for message in messages))

    batches = [set(_cards(message)) for message in messages]
    assert [len(batch) for batch in batches] == [3, 3, 3]
    assert len(batches[0] | batches[1] | batches[2]) == 9


async def test_two_admins_in_one_group_get_one_batch_at_a_time(batch_env) -> None:
    await _add_leads(batch_env, 6)
    first = _group_message(GROUP, ADMIN)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_answer(*args, **kwargs):
        started.set()
        await release.wait()

    first.answer = AsyncMock(side_effect=slow_answer)
    task = asyncio.ensure_future(issue.handle_next_batch(first, _runner()))
    await started.wait()

    second = _group_message(GROUP, SECOND_ADMIN)
    await issue.handle_next_batch(second, _runner())
    assert "ещё отправляется" in second.answer.await_args.args[0]
    assert _cards(second) == []

    # Meanwhile another chat is not held up, and gets different leads.
    other = _group_message(OTHER_GROUP, SECOND_ADMIN)
    await issue.handle_next_batch(other, _runner())
    assert len(_cards(other)) == 3

    release.set()
    await task
    async with batch_env() as session:
        assert await count_new_leads(session) == 0
        assert await count_issued_leads(session) == 6


async def test_inline_button_hands_out_a_batch(batch_env) -> None:
    await _add_leads(batch_env, 3)
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = _group_message()

    await issue.handle_next_batch_button(callback, _runner())

    callback.answer.assert_awaited_once_with()
    assert len(_cards(callback.message)) == 3


async def test_inline_button_during_a_batch_only_says_so(batch_env) -> None:
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = _group_message()
    issue._issuing_to.add(GROUP)

    await issue.handle_next_batch_button(callback, _runner())

    callback.answer.assert_awaited_once_with("Предыдущая пачка ещё отправляется")
    callback.message.answer.assert_not_awaited()


async def test_inline_button_on_an_inaccessible_message_is_stale(batch_env) -> None:
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = None
    await issue.handle_next_batch_button(callback, _runner())
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_group_migrating_during_a_batch_returns_leads_and_tells_the_new_chat(batch_env) -> None:
    await _add_leads(batch_env, 3)
    new_id = -100555
    message = _group_message()
    message.answer = AsyncMock(
        side_effect=TelegramMigrateToChat(method=METHOD, message="migrated", migrate_to_chat_id=new_id)
    )

    await issue.handle_next_batch(message, _runner())  # must not raise

    async with batch_env() as session:
        assert await count_new_leads(session) == 3
        assert await count_issued_leads(session) == 0
    call = message.bot.send_message.await_args
    assert call.args[0] == new_id
    assert "ещё раз" in call.args[1]
    assert issue._issuing_to == set()


async def test_chat_where_the_bot_was_removed_returns_leads_without_raising(batch_env) -> None:
    await _add_leads(batch_env, 2)
    message = _group_message()
    message.answer = AsyncMock(side_effect=TelegramForbiddenError(method=METHOD, message="kicked"))

    await issue.handle_next_batch(message, _runner())

    async with batch_env() as session:
        assert await count_new_leads(session) == 2


async def test_only_one_of_concurrent_chats_gets_a_scarce_lead(session_maker) -> None:
    await _add_leads(session_maker, 1)

    async def take() -> list[Lead]:
        async with session_maker() as session:
            return await issue_next_leads(session, 30)

    results = await asyncio.gather(take(), take(), take())
    assert sorted(len(result) for result in results) == [0, 0, 1]


# ---------- who set the status ----------


def _status_callback(lead_id: int, status: str, user: User) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ls:{lead_id}:{status}"
    callback.answer = AsyncMock()
    callback.from_user = user
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    return callback


async def _issued_lead_id(session_maker) -> int:
    await _add_leads(session_maker, 1)
    async with session_maker() as session:
        return (await issue_next_leads(session, 1))[0].id


async def test_card_shows_who_set_the_status_and_the_database_keeps_when(batch_env) -> None:
    lead_id = await _issued_lead_id(batch_env)
    user = User(id=ADMIN, is_bot=False, first_name="Анна", username="anna_sales")
    before = datetime.now(UTC)
    callback = _status_callback(lead_id, CONTACT_CONTACTED, user)

    await issue.handle_lead_status(callback)

    assert "✍️ Написал — @anna_sales" in callback.message.edit_text.await_args.args[0]
    async with batch_env() as session:
        lead = await session.get(Lead, lead_id)
    assert (lead.contact_status_by_id, lead.contact_status_by) == (ADMIN, "@anna_sales")
    assert lead.contact_status_at >= before
    assert lead.crm_status == STATUS_ISSUED


async def test_user_without_username_is_shown_by_name_and_the_name_is_escaped(batch_env) -> None:
    lead_id = await _issued_lead_id(batch_env)
    user = User(id=ADMIN, is_bot=False, first_name="<b>Анна</b>", last_name="& Ко")
    callback = _status_callback(lead_id, CONTACT_CONTACTED, user)

    await issue.handle_lead_status(callback)

    text = callback.message.edit_text.await_args.args[0]
    assert "&lt;b&gt;Анна&lt;/b&gt; &amp; Ко" in text
    assert "<b>Анна" not in text


async def test_changing_the_status_updates_who_and_same_status_does_not(batch_env) -> None:
    lead_id = await _issued_lead_id(batch_env)
    anna = User(id=ADMIN, is_bot=False, first_name="Анна", username="anna")
    boris = User(id=SECOND_ADMIN, is_bot=False, first_name="Борис", username="boris")

    await issue.handle_lead_status(_status_callback(lead_id, CONTACT_CONTACTED, anna))
    await issue.handle_lead_status(_status_callback(lead_id, CONTACT_CONTACTED, boris))
    async with batch_env() as session:
        assert (await session.get(Lead, lead_id)).contact_status_by == "@anna"

    await issue.handle_lead_status(_status_callback(lead_id, "replied", boris))
    async with batch_env() as session:
        lead = await session.get(Lead, lead_id)
    assert (lead.contact_status, lead.contact_status_by) == ("replied", "@boris")
    assert lead.contact_status_by_id == SECOND_ADMIN


def test_card_before_any_status_names_nobody() -> None:
    assert " — " not in format_issued_lead(_lead(1))


def test_user_label() -> None:
    assert format_user_label("anna", "Анна") == "@anna"
    assert format_user_label(None, "Анна Иванова") == "Анна Иванова"


async def test_flood_wait_on_the_batch_notice_does_not_stop_the_batch(batch_env) -> None:
    await _add_leads(batch_env, 2)
    message = _group_message()
    calls = 0

    async def flood_on_the_notice(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TelegramRetryAfter(method=METHOD, message="flood", retry_after=5)

    message.answer = AsyncMock(side_effect=flood_on_the_notice)
    await issue.handle_next_batch(message, _runner())

    assert len(_cards(message)) == 2
    async with batch_env() as session:
        assert await count_issued_leads(session) == 2

