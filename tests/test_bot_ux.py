"""Tests for the menu, the help, the command menus of Telegram and the hints instead of silence."""

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.methods import SendMessage
from aiogram.types import (
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from src.bot import commands as bot_commands
from src.bot.formatters import MAX_MESSAGE_LENGTH, format_help, format_import_report, format_unknown_niche
from src.bot.handlers import collector, fallback, imports, issue, leads, menu, reminders, search
from src.bot.middlewares import AdminOnlyMiddleware
from src.bot.texts import (
    ENRICH_COMMAND,
    HELP_SECTIONS,
    NOT_UNDERSTOOD_ADMIN_TEXT,
    NOT_UNDERSTOOD_USER_TEXT,
    START_ADMIN_TEXT,
    START_USER_TEXT,
    CommandHelp,
    HelpSection,
)
from src.config import Settings
from src.services.lead_enrich import EnrichStats
from src.services.lead_file import REQUEST_FILE_ERROR
from src.services.lead_import import ImportResult

ADMIN, SECOND_ADMIN, STRANGER = 100, 200, 999
GROUP = -1001
METHOD = MagicMock(spec=SendMessage)
ROUTERS = (
    search.router,
    leads.router,
    issue.router,
    collector.router,
    imports.router,
    reminders.router,
    menu.router,
    fallback.router,
)
ADMIN_ONLY_COMMANDS = {
    "next_leads",
    "rescore",
    "today",
    "export_unknown",
    "leads_stats",
    "niches",
    "collect_status",
    "collect_now",
    "collect_on",
    "collect_off",
    "provider_on",
    "chat_id",
}


def _settings() -> Settings:
    return Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=ADMIN, ADMIN_TG_IDS=str(SECOND_ADMIN))


def _commands_in_code() -> set[str]:
    """Every command some handler answers, read from the filters of the routers."""
    names: set[str] = set()
    for router in ROUTERS:
        for handler in router.message.handlers:
            for item in handler.filters or []:
                if isinstance(item.callback, Command):
                    names |= {command for command in item.callback.commands if isinstance(command, str)}
    return names


# ---------- the command menus ----------


def test_everyone_gets_a_short_menu_and_admins_get_every_command() -> None:
    public = [command.command for command in bot_commands.public_commands()]
    full = [command.command for command in bot_commands.admin_commands()]

    assert public == ["start", "help"]
    assert set(full) == {"start", "help", *ADMIN_ONLY_COMMANDS}
    # Nothing a handler answers is missing from the admin menu, and nothing in it is made up.
    assert set(full) == _commands_in_code()
    for command in bot_commands.admin_commands():
        assert 1 <= len(command.description) <= 256
        assert command.command.islower() and len(command.command) <= 32


async def test_menus_go_to_everyone_and_to_the_chat_of_each_admin() -> None:
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()

    await bot_commands.register_bot_commands(bot, frozenset({SECOND_ADMIN, ADMIN}))

    scopes = [call.kwargs["scope"] for call in bot.set_my_commands.await_args_list]
    assert isinstance(scopes[0], BotCommandScopeDefault)
    assert [scope.chat_id for scope in scopes[1:] if isinstance(scope, BotCommandScopeChat)] == [ADMIN, SECOND_ADMIN]
    assert len(bot.set_my_commands.await_args_list[1].args[0]) == len(bot_commands.admin_commands())


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(method=METHOD, message="chat not found"),
        TelegramNetworkError(method=METHOD, message="no network"),
        RuntimeError("something unexpected"),
    ],
)
async def test_a_failed_menu_is_only_logged_and_the_others_are_still_set(error: Exception, caplog) -> None:
    bot = MagicMock()
    bot.set_my_commands = AsyncMock(side_effect=[error, error, None])

    await bot_commands.register_bot_commands(bot, [ADMIN, SECOND_ADMIN])

    assert bot.set_my_commands.await_count == 3
    assert "command menu" in caplog.text


# ---------- /help ----------


def test_help_hides_admin_commands_from_a_stranger() -> None:
    text = "\n".join(format_help(is_admin=False))

    assert "/start" in text and "/help" in text
    assert "🔒" not in text
    for command in ADMIN_ONLY_COMMANDS:
        assert f"/{command}" not in text
    assert "только администраторам" in text


def test_help_for_an_admin_has_every_command_marked_with_an_example() -> None:
    text = "\n".join(format_help(is_admin=True))

    for section in ("Выдача", "Обогащение", "Статистика", "Настройки"):
        assert f"<b>{section}</b>" in text
    for command in ADMIN_ONLY_COMMANDS:
        assert f"🔒 /{command} —" in text
    assert text.count("Пример:") == sum(len(section.commands) for section in HELP_SECTIONS)
    assert "только администраторам" not in text


def test_long_help_is_split_within_the_limit_without_losing_a_command() -> None:
    many = tuple(
        HelpSection(
            f"Раздел {number}",
            tuple(
                CommandHelp(f"cmd{number}_{item}", "м", "Описание " * 20, f"/cmd{number}_{item} 1")
                for item in range(20)
            ),
        )
        for number in range(10)
    )

    messages = format_help(is_admin=True, sections=many)

    assert len(messages) > 1
    assert all(len(message) <= MAX_MESSAGE_LENGTH for message in messages)
    joined = "\n".join(messages)
    assert all(f"/cmd{number}_{item} —" in joined for number in range(10) for item in range(20))
    # A command and its example stay together in one message.
    assert all(message.count(" — ") == message.count("Пример:") for message in messages)


def test_the_real_help_fits_in_one_message() -> None:
    assert len(format_help(is_admin=True)) == 1


# ---------- wrong arguments and wrong files ----------


async def test_a_wrong_number_gets_one_sentence_and_an_example(monkeypatch) -> None:
    message = MagicMock()
    message.answer = AsyncMock()

    await imports.handle_export_unknown(message, MagicMock(args="0"))

    text = message.answer.await_args.args[0]
    assert "от 1 до" in text
    assert "<code>/export_unknown 30</code>" in text


def test_an_unknown_niche_gets_an_example_with_a_real_niche() -> None:
    first, *_ = format_unknown_niche("abc", ["beauty", "dentist_clinic"])

    assert first.startswith("Неизвестная ниша: abc. Пример: <code>/export_unknown 30 beauty</code>")
    assert "dentist_clinic" in first


async def test_an_unknown_source_gets_an_example() -> None:
    message = MagicMock()
    message.answer = AsyncMock()
    runner = MagicMock()
    runner.known_provider = MagicMock(return_value=False)

    await collector.handle_provider_on(message, MagicMock(args="<yandex>"), runner)

    text = message.answer.await_args.args[0]
    assert "Неизвестный источник: &lt;yandex&gt;" in text
    assert "<code>/provider_on 2gis_scraper</code>" in text
    runner.enable_provider.assert_not_called()


def test_a_request_file_sent_back_says_which_file_to_send_and_where_it_is() -> None:
    result = ImportResult(source="telegram", file_name="enrich_request_1.json", ok=False, error=REQUEST_FILE_ERROR)

    text = format_import_report(result)

    assert REQUEST_FILE_ERROR in text
    assert "без слова request" in text
    assert "<code>exports/</code>" in text


def test_other_import_errors_get_no_hint_about_request_files() -> None:
    result = ImportResult(source="telegram", file_name="x.json", ok=False, error="неверный JSON")

    assert "без слова request" not in format_import_report(result)


# ---------- the file for a collector and the result of an enrichment ----------


def test_the_caption_of_a_request_has_the_steps_and_the_command_within_the_limit() -> None:
    caption = imports._export_caption(30, 30, 3, None)
    with_niche = imports._export_caption(10, 30, 0, "очень длинная ниша <b>" * 20)

    assert f"<code>{ENRICH_COMMAND}</code>" in caption
    assert "капчу" in caption and "без слова request" in caption
    assert len(with_niche) <= 1024
    assert "<b>очень" not in with_niche  # a niche from the database is escaped


async def test_the_result_of_an_enrichment_ends_with_buttons_for_what_is_next() -> None:
    message = MagicMock()
    status = MagicMock()
    status.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status)
    message.document = MagicMock(file_size=10, file_name="enrich.json")
    message.chat.id = ADMIN
    bot = MagicMock()
    bot.download = AsyncMock()
    result = ImportResult(source="telegram", file_name="enrich.json", total=1, enrichment=EnrichStats(updated=1))
    service = MagicMock(max_bytes=1000)
    service.import_bytes = AsyncMock(return_value=result)

    await imports.handle_lead_file(message, bot, service)

    message.answer.assert_awaited_once()  # only the note: it became the result
    markup = status.edit_text.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert data == ["panel:next", "enrich:request"]


async def test_a_failed_import_has_no_buttons() -> None:
    message = MagicMock()
    status = MagicMock()
    status.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status)
    message.document = MagicMock(file_size=10, file_name="enrich_request.json")
    bot = MagicMock()
    bot.download = AsyncMock()
    result = ImportResult(source="telegram", file_name="enrich_request.json", ok=False, error=REQUEST_FILE_ERROR)
    service = MagicMock(max_bytes=1000)
    service.import_bytes = AsyncMock(return_value=result)

    await imports.handle_lead_file(message, bot, service)

    assert status.edit_text.await_args.kwargs["reply_markup"] is None


async def test_the_button_for_a_request_is_answered_before_the_file_is_built(monkeypatch) -> None:
    order: list[str] = []

    async def send(message, limit, niche_text=None):
        order.append(f"file {limit}")

    monkeypatch.setattr(menu, "send_enrich_request", send)
    callback = MagicMock(spec=CallbackQuery)
    callback.message = MagicMock(spec=Message)
    callback.answer = AsyncMock(side_effect=lambda *args, **kwargs: order.append("answered"))

    await menu.handle_enrich_request_button(callback)

    assert order == ["answered", "file 30"]


# ---------- through the dispatcher ----------


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.id = 1
    bot.me = AsyncMock(return_value=User(id=1, is_bot=True, first_name="Bot", username="spb_lead_bot"))
    return bot


def _update(chat_id: int, user_id: int, text: str) -> Update:
    entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else None
    message = Message(
        message_id=1,
        date=datetime(2026, 11, 7, tzinfo=UTC),
        chat=Chat(id=chat_id, type="supergroup" if chat_id < 0 else "private"),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        text=text,
        entities=entities,
    )
    return Update(update_id=1, message=message)


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """The dispatcher as main.py builds it, with Telegram calls recorded."""
    monkeypatch.setattr("src.bot.middlewares.settings", _settings())
    calls = MagicMock()
    calls.answer = AsyncMock()
    calls.reply = AsyncMock()
    monkeypatch.setattr(Message, "answer", calls.answer)
    monkeypatch.setattr(Message, "reply", calls.reply)
    dp = Dispatcher()
    guard = AdminOnlyMiddleware()
    dp.message.middleware(guard)
    dp.callback_query.middleware(guard)
    for router in ROUTERS:
        dp.include_router(router)
    calls.feed = lambda chat_id, user_id, text: dp.feed_update(
        _bot(), _update(chat_id, user_id, text), collector_runner=MagicMock(), import_service=MagicMock()
    )
    yield calls
    for router in ROUTERS:
        router._parent_router = None
    dp.sub_routers.clear()


def _answered(telegram: MagicMock) -> tuple[str, object]:
    call = telegram.answer.await_args
    return call.args[0], call.kwargs.get("reply_markup")


@pytest.mark.parametrize("text", ["привет", "/unknown_command", "сколько лидов?"])
async def test_an_admin_with_unclear_text_gets_a_hint_and_the_menu(telegram, text: str) -> None:
    await telegram.feed(ADMIN, ADMIN, text)

    answer, markup = _answered(telegram)
    assert answer == NOT_UNDERSTOOD_ADMIN_TEXT
    assert answer.startswith("Не понял. Вот что можно:")
    assert isinstance(markup, ReplyKeyboardMarkup)


async def test_a_stranger_with_unclear_text_gets_a_hint_without_the_admin_menu(telegram) -> None:
    await telegram.feed(STRANGER, STRANGER, "здравствуйте")

    answer, markup = _answered(telegram)
    assert answer == NOT_UNDERSTOOD_USER_TEXT
    assert markup is None


async def test_unclear_text_in_a_group_gets_no_answer(telegram) -> None:
    await telegram.feed(GROUP, ADMIN, "привет всем")

    telegram.answer.assert_not_awaited()
    telegram.reply.assert_not_awaited()


async def test_start_for_an_admin_shows_the_bottom_keyboard(telegram) -> None:
    await telegram.feed(ADMIN, ADMIN, "/start")

    answer, markup = _answered(telegram)
    assert answer == START_ADMIN_TEXT
    assert isinstance(markup, ReplyKeyboardMarkup)


async def test_start_for_a_stranger_says_what_the_bot_is(telegram) -> None:
    await telegram.feed(STRANGER, STRANGER, "/start")

    answer, markup = _answered(telegram)
    assert answer == START_USER_TEXT
    assert markup is None


async def test_start_in_a_group_keeps_the_inline_panel(telegram) -> None:
    await telegram.feed(GROUP, ADMIN, "/start")

    _, markup = _answered(telegram)
    assert isinstance(markup, InlineKeyboardMarkup)


async def test_help_for_a_stranger_through_the_bot_hides_admin_commands(telegram) -> None:
    await telegram.feed(STRANGER, STRANGER, "/help")

    answer, _ = _answered(telegram)
    assert "/export_unknown" not in answer
    assert "/help" in answer


async def test_help_button_for_an_admin(telegram) -> None:
    await telegram.feed(ADMIN, ADMIN, "❓ Помощь")

    answer, markup = _answered(telegram)
    assert "🔒 /export_unknown" in answer
    assert isinstance(markup, ReplyKeyboardMarkup)


async def test_enrichment_button_shows_five_steps_with_the_command_and_a_request_button(telegram) -> None:
    await telegram.feed(ADMIN, ADMIN, "🔎 Обогащение")

    answer, markup = _answered(telegram)
    assert all(f"<b>{step}.</b>" in answer for step in range(1, 6))
    assert f"<code>{ENRICH_COMMAND}</code>" in answer
    assert len(answer) <= MAX_MESSAGE_LENGTH
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].callback_data == "enrich:request"


async def test_a_stranger_cannot_open_the_enrichment_guide(telegram) -> None:
    await telegram.feed(STRANGER, STRANGER, "🔎 Обогащение")

    answer, _ = _answered(telegram)
    assert "только администраторам" in answer
    assert "collect_cli" not in answer


async def test_stats_button_shows_the_funnel_and_the_niches(telegram, monkeypatch) -> None:
    stats, niches = AsyncMock(), AsyncMock()
    monkeypatch.setattr(menu, "handle_leads_stats", stats)
    monkeypatch.setattr(menu, "handle_niches", niches)

    await telegram.feed(ADMIN, ADMIN, "📊 Ниши и статистика")

    stats.assert_awaited_once()
    niches.assert_awaited_once()
