"""Several owners of the bot: ADMIN_TG_ID plus ADMIN_TG_OWNER_IDS."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import BotCommandScopeChat, CallbackQuery, Chat, Document, Message, User
from pydantic import ValidationError

from src.bot import commands as bot_commands
from src.bot import middlewares
from src.bot.formatters import format_team
from src.bot.handlers import reminders
from src.bot.middlewares import AdminOnlyMiddleware
from src.bot.notifier import AdminNotifier
from src.bot.texts import TEAM_OPERATOR_MARK, TEAM_OWNER_MARK
from src.config import Settings
from src.database.repo import TeamRow

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
OWNER, SECOND_OWNER, THIRD_OWNER, ANNA, STRANGER = 100, 110, 120, 200, 999
METHOD = MagicMock()


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=OWNER, **values)  # type: ignore[arg-type]


MULTI = {"ADMIN_TG_IDS": f"{ANNA}", "ADMIN_TG_OWNER_IDS": f"{SECOND_OWNER}, {THIRD_OWNER}"}


# ---------- settings ----------


def test_owners_and_operators_with_extra_owners() -> None:
    settings = _settings(**MULTI)
    assert settings.owner_ids == frozenset({OWNER, SECOND_OWNER, THIRD_OWNER})
    assert settings.operator_ids == frozenset({ANNA})
    assert settings.admin_ids == frozenset({OWNER, SECOND_OWNER, THIRD_OWNER, ANNA})
    assert settings.owner_id == OWNER  # the main owner stays ADMIN_TG_ID
    assert settings.has_extra_owners
    assert settings.is_owner(OWNER) and settings.is_owner(SECOND_OWNER) and settings.is_owner(THIRD_OWNER)
    assert not settings.is_owner(ANNA) and not settings.is_owner(STRANGER) and not settings.is_owner(None)


def test_an_id_in_both_lists_is_an_owner() -> None:
    settings = _settings(ADMIN_TG_IDS=f"{ANNA},{SECOND_OWNER}", ADMIN_TG_OWNER_IDS=f"{SECOND_OWNER}")
    assert settings.is_owner(SECOND_OWNER)
    assert SECOND_OWNER not in settings.operator_ids
    assert SECOND_OWNER in settings.admin_ids
    assert settings.operator_ids == frozenset({ANNA})


def test_without_extra_owners_nothing_changes() -> None:
    settings = _settings(ADMIN_TG_IDS=f"{ANNA}")
    assert settings.owner_ids == frozenset({OWNER})
    assert settings.admin_ids == frozenset({OWNER, ANNA})
    assert settings.operator_ids == frozenset({ANNA})
    assert not settings.has_extra_owners


def test_admin_tg_id_zero_gives_no_owner() -> None:
    settings = Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_IDS=f"{ANNA}")
    assert settings.owner_ids == frozenset()
    assert not settings.is_owner(0)
    # As before: ADMIN_TG_ID=0 is still in admin_ids, but not an operator.
    assert settings.admin_ids == frozenset({0, ANNA})
    assert settings.operator_ids == frozenset({ANNA})

    extra = Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_OWNER_IDS=f"{SECOND_OWNER}")
    assert extra.owner_ids == frozenset({SECOND_OWNER})
    assert not extra.is_owner(0)


def test_model_construct_fallback_has_no_owners() -> None:
    from pydantic import SecretStr

    settings = Settings.model_construct(BOT_TOKEN=SecretStr(""), ADMIN_TG_ID=0)
    assert settings.owner_ids == frozenset()
    assert not settings.is_owner(0)
    assert not settings.has_extra_owners


@pytest.mark.parametrize("name", ["ADMIN_TG_OWNER_IDS", "ADMIN_TG_IDS"])
@pytest.mark.parametrize("value", ["abc", "@user", "1,@user", "1,x", "-5"])
def test_wrong_ids_are_refused(name: str, value: str) -> None:
    with pytest.raises(ValidationError, match=name):
        _settings(**{name: value})


def test_zero_is_refused_among_owners() -> None:
    for value in ("0", f"{SECOND_OWNER},0", "00"):
        with pytest.raises(ValidationError, match="ADMIN_TG_OWNER_IDS"):
            _settings(ADMIN_TG_OWNER_IDS=value)


@pytest.mark.parametrize("name", ["ADMIN_TG_OWNER_IDS", "ADMIN_TG_IDS"])
def test_spaces_and_empty_items_are_allowed(name: str) -> None:
    settings = _settings(**{name: " 1, ,2,"})
    assert {1, 2} <= settings.admin_ids


def test_admin_ids_error_text_is_unchanged() -> None:
    with pytest.raises(ValidationError, match=r"ADMIN_TG_IDS must be comma-separated Telegram IDs, got '@user'"):
        _settings(ADMIN_TG_IDS="@user")


# ---------- access ----------

_DOCUMENT = Document(file_id="f", file_unique_id="u", file_name="leads.json")
OWNER_ONLY_EVENTS = [
    ("/rescore", None),
    ("/collect_now", None),
    ("/collect_on", None),
    ("/provider_on 2gis", None),
    ("/team", None),
    ("💼 В работе", None),
    ("📊 Статистика и CSV", None),
    (None, "export:all"),
    (None, "panel:collect"),
    ("document", None),
]
OPERATOR_EVENTS = [("/next_leads", None), ("/plan", None), (None, "ls:1:contacted")]


def _event(user_id: int, text_: str | None, data: str | None) -> Message | CallbackQuery:
    user = User(id=user_id, is_bot=False, first_name="U")
    if data is not None:
        return CallbackQuery(id="1", from_user=user, chat_instance="c", data=data)
    chat = Chat(id=user_id, type="private")
    if text_ == "document":
        return Message(message_id=1, date=NOW, chat=chat, from_user=user, document=_DOCUMENT)
    return Message(message_id=1, date=NOW, chat=chat, from_user=user, text=text_)


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> AdminOnlyMiddleware:
    monkeypatch.setattr(middlewares, "settings", _settings(**MULTI))
    monkeypatch.setattr(Message, "answer", AsyncMock())
    monkeypatch.setattr(Message, "reply", AsyncMock())
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    return AdminOnlyMiddleware()


@pytest.mark.parametrize(("text_", "data"), OWNER_ONLY_EVENTS + OPERATOR_EVENTS)
@pytest.mark.parametrize("user_id", [OWNER, SECOND_OWNER, THIRD_OWNER, ANNA, STRANGER])
async def test_access_matrix_with_several_owners(guard, user_id: int, text_: str | None, data: str | None) -> None:
    handler = AsyncMock(return_value="handled")
    event = _event(user_id, text_, data)
    result = await guard(handler, event, {"event_from_user": event.from_user})

    owner_only = (text_, data) in OWNER_ONLY_EVENTS
    allowed = user_id in (OWNER, SECOND_OWNER, THIRD_OWNER) or (user_id == ANNA and not owner_only)
    assert (result == "handled") is allowed
    assert handler.await_count == int(allowed)


# ---------- notifications and the command menu ----------


async def test_every_owner_gets_notifications_even_if_one_fails() -> None:
    settings = _settings(**MULTI)
    errors = {
        SECOND_OWNER: TelegramForbiddenError(method=METHOD, message="bot was blocked by the user"),
        ANNA: TelegramAPIError(method=METHOD, message="something"),
    }

    async def send_message(target: int, *args: object, **kwargs: object) -> None:
        if target in errors:
            raise errors[target]

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=send_message)

    await AdminNotifier(bot, settings.admin_ids).send("итог")

    sent_to = sorted(call.args[0] for call in bot.send_message.await_args_list)
    assert sent_to == sorted(settings.admin_ids) == sorted([OWNER, SECOND_OWNER, THIRD_OWNER, ANNA])


async def test_the_command_menu_is_set_for_every_owner() -> None:
    settings = _settings(**MULTI)
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()

    await bot_commands.register_bot_commands(bot, settings.admin_ids)

    chats = [
        call.kwargs["scope"].chat_id
        for call in bot.set_my_commands.await_args_list
        if isinstance(call.kwargs["scope"], BotCommandScopeChat)
    ]
    assert chats == sorted([OWNER, SECOND_OWNER, THIRD_OWNER, ANNA])


# ---------- /team ----------

ROWS = [
    TeamRow("@anna", 3, 1, 0, ANNA),
    TeamRow("<b>Second</b>", 2, 0, 1, SECOND_OWNER),
    TeamRow("@gone", 1, 0, 0, STRANGER),
]
OLD_TEXT = (
    "👥 <b>Команда</b>\n\n"
    "• <b>@anna</b>: в работе 3, ответили 1, истекают за сутки 0\n"
    "• <b>&lt;b&gt;Second&lt;/b&gt;</b>: в работе 2, ответили 0, истекают за сутки 1\n"
    "• <b>@gone</b>: в работе 1, ответили 0, истекают за сутки 0"
)


def test_team_without_owner_ids_is_as_before() -> None:
    assert format_team(ROWS) == OLD_TEXT


def test_team_marks_roles_with_owner_ids() -> None:
    settings = _settings(**MULTI)
    lines = format_team(ROWS, settings.owner_ids, settings.operator_ids).splitlines()
    assert lines[2] == f"• <b>@anna</b>{TEAM_OPERATOR_MARK}: в работе 3, ответили 1, истекают за сутки 0"
    assert lines[3] == (
        f"• <b>&lt;b&gt;Second&lt;/b&gt;</b>{TEAM_OWNER_MARK}: в работе 2, ответили 0, истекают за сутки 1"
    )
    # Someone no longer in the lists still has leads and is shown, without a mark.
    assert lines[4] == "• <b>@gone</b>: в работе 1, ответили 0, истекают за сутки 0"


@pytest.mark.parametrize(("values", "marked"), [({"ADMIN_TG_IDS": f"{ANNA}"}, False), (MULTI, True)])
async def test_handle_team_marks_roles_only_with_extra_owners(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, str], marked: bool
) -> None:
    @asynccontextmanager
    async def session_maker():  # type: ignore[no-untyped-def]
        yield MagicMock()

    monkeypatch.setattr(reminders, "settings", _settings(**values))
    monkeypatch.setattr(reminders, "async_session_maker", session_maker)
    monkeypatch.setattr(reminders, "get_team_stats", AsyncMock(return_value=ROWS))
    message = MagicMock()
    message.answer = AsyncMock()

    await reminders.handle_team(message)

    text = message.answer.await_args.args[0]
    if marked:
        assert TEAM_OPERATOR_MARK in text and TEAM_OWNER_MARK in text
    else:
        assert text == OLD_TEXT
