"""Tests for several people in one bot: whose a lead is, for how long, and who may do what."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from src.bot.formatters import format_help, format_issued_lead
from src.bot.handlers import collector as collector_handlers
from src.bot.handlers import imports, issue, reminders
from src.bot.middlewares import AdminOnlyMiddleware
from src.bot.texts import OWNER_ONLY_TEXT
from src.config import Settings
from src.database.models import (
    CONTACT_CONTACTED,
    CONTACT_NO_ANSWER,
    CONTACT_REJECTED,
    CONTACT_REPLIED,
    STATUS_ISSUED,
    STATUS_NEW,
    Lead,
)
from src.database.ownership import (
    assign_existing_leads,
    can_act,
    get_people_stats,
    release_expired_leads,
    reserve_for_export,
)
from src.database.repo import (
    IssueRules,
    get_digest_counts,
    get_followups,
    get_unknown_site_candidates,
    issue_next_leads,
    set_contact_status,
)
from src.services.site_status import SITE_NO_SITE, SITE_UNKNOWN

OWNER, ANNA, BORIS, STRANGER = 100, 200, 300, 999
NOW = datetime.now(UTC)
DAYS = 30


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=OWNER, ADMIN_TG_IDS=f"{ANNA},{BORIS}", **overrides)


def _lead(number: int, **overrides: Any) -> Lead:
    values: dict[str, Any] = {
        "id": number,
        "external_id": f"2gis_{number}_abc",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": "dentist_clinic",
        "site_status": SITE_NO_SITE,
        "score": 50,
        "crm_status": STATUS_NEW,
        "source_url": f"https://2gis.ru/spb/firm/{70000000 + number}",
        "created_at": NOW - timedelta(days=30, minutes=number),
    }
    values.update(overrides)
    return Lead(**values)


def _issued(number: int, to: int | None = ANNA, **overrides: Any) -> Lead:
    return _lead(number, crm_status=STATUS_ISSUED, issued_at=NOW - timedelta(days=1), issued_to_user_id=to, **overrides)


async def _add(session_maker, *leads: Lead) -> None:
    async with session_maker() as session:
        session.add_all(leads)
        await session.commit()


async def _get(session_maker, lead_id: int) -> Lead:
    async with session_maker() as session:
        lead = await session.get(Lead, lead_id)
    assert lead is not None
    return lead


async def _status(session_maker, lead_id: int, status: str, user_id: int, name: str = "@anna") -> None:
    async with session_maker() as session:
        await set_contact_status(session, lead_id, status, user_id, name, ownership_days=DAYS)


# ---------- ownership by status ----------


async def test_wrote_to_them_makes_the_lead_theirs_for_thirty_days(session_maker) -> None:
    await _add(session_maker, _issued(1))

    await _status(session_maker, 1, CONTACT_CONTACTED, ANNA)

    lead = await _get(session_maker, 1)
    assert (lead.owner_user_id, lead.owner_name) == (ANNA, "@anna")
    assert lead.owned_until is not None
    assert timedelta(days=DAYS) - timedelta(minutes=1) < lead.owned_until - NOW < timedelta(days=DAYS, minutes=1)
    assert "👤 <b>В работе у</b> @anna до " in format_issued_lead(lead)


async def test_replied_keeps_the_lead_with_no_end_even_if_the_owner_of_the_bot_presses_it(session_maker) -> None:
    await _add(session_maker, _issued(1))
    await _status(session_maker, 1, CONTACT_CONTACTED, ANNA)

    await _status(session_maker, 1, CONTACT_REPLIED, OWNER, "@owner")

    lead = await _get(session_maker, 1)
    assert (lead.owner_user_id, lead.owner_name, lead.owned_until) == (ANNA, "@anna", None)
    assert "👤 <b>В работе у</b> @anna" in format_issued_lead(lead)


async def test_no_answer_frees_the_lead_and_gives_it_back_a_day_later(session_maker) -> None:
    await _add(session_maker, _issued(1))
    await _status(session_maker, 1, CONTACT_CONTACTED, ANNA)

    await _status(session_maker, 1, CONTACT_NO_ANSWER, ANNA)

    lead = await _get(session_maker, 1)
    assert lead.owner_user_id is None and lead.owned_until is not None
    assert "Вернётся в пул" in format_issued_lead(lead)
    async with session_maker() as session:
        assert await release_expired_leads(session, NOW + timedelta(hours=23)) == 0
        assert await release_expired_leads(session, NOW + timedelta(hours=25)) == 1
    lead = await _get(session_maker, 1)
    assert (lead.crm_status, lead.issued_at, lead.issued_to_user_id, lead.contact_status) == (
        STATUS_NEW,
        None,
        None,
        None,
    )


async def test_refused_stays_out_of_every_batch(session_maker) -> None:
    await _add(session_maker, _issued(1))
    await _status(session_maker, 1, CONTACT_CONTACTED, ANNA)
    await _status(session_maker, 1, CONTACT_REJECTED, ANNA)

    async with session_maker() as session:
        assert await release_expired_leads(session, NOW + timedelta(days=365)) == 0
        assert await issue_next_leads(session, 10, user_id=BORIS) == []
    assert (await _get(session_maker, 1)).contact_status == CONTACT_REJECTED


async def test_a_lead_without_progress_goes_back_when_its_time_ends_and_only_once(session_maker) -> None:
    await _add(
        session_maker,
        _issued(1, owner_user_id=ANNA, owned_until=NOW - timedelta(minutes=1), contact_status=CONTACT_CONTACTED),
        _issued(2, owner_user_id=ANNA, owned_until=NOW + timedelta(days=1), contact_status=CONTACT_CONTACTED),
        _issued(3, owner_user_id=ANNA, owned_until=None, contact_status=CONTACT_REPLIED),
        _issued(4),  # handed out, nothing pressed: stays with whom it went to
    )

    async with session_maker() as session:
        assert await release_expired_leads(session, NOW) == 1
        assert await release_expired_leads(session, NOW) == 0  # idempotent

    assert (await _get(session_maker, 1)).crm_status == STATUS_NEW
    assert [(await _get(session_maker, n)).crm_status for n in (2, 3, 4)] == [STATUS_ISSUED] * 3
    async with session_maker() as session:
        again = await issue_next_leads(session, 10, user_id=BORIS)
    assert [lead.id for lead in again] == [1]  # back in the pool, for anybody now


# ---------- batches ----------


async def test_two_people_pressing_at_once_get_different_leads(session_maker) -> None:
    await _add(session_maker, *(_lead(n) for n in range(1, 11)))
    rules = IssueRules(issue_unknown=True, max_niche_share=1)

    async def take(user_id: int) -> list[Lead]:
        async with session_maker() as session:
            return await issue_next_leads(session, 6, rules, user_id=user_id)

    anna, boris = await asyncio.gather(take(ANNA), take(BORIS))

    assert not {lead.id for lead in anna} & {lead.id for lead in boris}
    assert len(anna) + len(boris) == 10
    assert {(await _get(session_maker, lead.id)).issued_to_user_id for lead in anna} == {ANNA}
    assert {(await _get(session_maker, lead.id)).issued_to_user_id for lead in boris} == {BORIS}


async def test_one_person_pressing_in_two_chats_gets_one_batch(monkeypatch) -> None:
    started: list[int | None] = []
    monkeypatch.setattr(issue, "_start_reserved_batch", lambda message, runner, user_id=None: started.append(user_id))
    issue._issuing_to.clear()
    issue._issuing_people.clear()

    def message(chat_id: int) -> MagicMock:
        item = MagicMock(spec=Message)
        item.chat = Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup")
        item.from_user = User(id=ANNA, is_bot=False, first_name="Анна")
        item.answer = AsyncMock()
        return item

    first, second = message(ANNA), message(-1001)
    await issue.handle_next_batch_request(first, MagicMock())
    await issue.handle_next_batch_request(second, MagicMock())

    assert started == [ANNA]
    assert "ещё отправляется" in second.answer.await_args.args[0]
    issue._issuing_to.clear()
    issue._issuing_people.clear()


# ---------- request files ----------


async def test_a_request_file_keeps_its_leads_for_its_author(session_maker) -> None:
    await _add(session_maker, *(_lead(n, site_status=SITE_UNKNOWN) for n in range(1, 5)))
    async with session_maker() as session:
        kept = await reserve_for_export(session, [1, 2], ANNA, NOW + timedelta(hours=6), NOW)
        for_boris = [lead.id for lead in await get_unknown_site_candidates(session, user_id=BORIS, now=NOW)]
        for_anna = [lead.id for lead in await get_unknown_site_candidates(session, user_id=ANNA, now=NOW)]
        later = NOW + timedelta(hours=7)
        for_boris_later = [lead.id for lead in await get_unknown_site_candidates(session, user_id=BORIS, now=later)]

    assert kept == {1, 2}
    assert sorted(for_boris) == [3, 4]
    assert sorted(for_anna) == [1, 2, 3, 4]
    assert sorted(for_boris_later) == [1, 2, 3, 4]  # the reserve has ended


async def test_two_request_files_at_once_never_share_a_lead(session_maker) -> None:
    await _add(session_maker, *(_lead(n, site_status=SITE_UNKNOWN) for n in range(1, 7)))

    async def reserve(user_id: int) -> set[int]:
        async with session_maker() as session:
            return await reserve_for_export(session, list(range(1, 7)), user_id, NOW + timedelta(hours=6), NOW)

    anna, boris = await asyncio.gather(reserve(ANNA), reserve(BORIS))

    assert not anna & boris
    assert anna | boris == set(range(1, 7))


async def test_export_command_reserves_for_who_asked(session_maker, monkeypatch) -> None:
    await _add(session_maker, *(_lead(n, site_status=SITE_UNKNOWN) for n in range(1, 4)))
    monkeypatch.setattr(imports, "async_session_maker", session_maker)

    def message(user_id: int) -> MagicMock:
        item = MagicMock()
        item.from_user = User(id=user_id, is_bot=False, first_name="U")
        item.answer = AsyncMock()
        item.answer_document = AsyncMock()
        return item

    anna, boris = message(ANNA), message(BORIS)
    await imports.handle_export_unknown(anna, MagicMock(args="2"))
    await imports.handle_export_unknown(boris, MagicMock(args="30"))

    anna_ids = {r["source_id"] for r in json.loads(anna.answer_document.await_args.args[0].data)["records"]}
    boris_ids = {r["source_id"] for r in json.loads(boris.answer_document.await_args.args[0].data)["records"]}
    assert len(anna_ids) == 2 and len(boris_ids) == 1 and not anna_ids & boris_ids


# ---------- the plan of the day ----------


async def test_reminders_and_silence_are_counted_of_each_person(session_maker) -> None:
    due_before, stale_before = NOW + timedelta(days=1), NOW - timedelta(days=3)
    await _add(
        session_maker,
        _issued(1, to=ANNA, owner_user_id=ANNA, contact_status=CONTACT_CONTACTED, remind_at=NOW),
        _issued(2, to=BORIS, owner_user_id=BORIS, contact_status=CONTACT_CONTACTED, remind_at=NOW),
        _issued(
            3,
            to=BORIS,
            contact_status=CONTACT_CONTACTED,
            contact_status_at=NOW - timedelta(days=5),
            owner_user_id=BORIS,
        ),
        _lead(4),  # the pool is everybody's
    )

    async with session_maker() as session:
        anna = await get_followups(session, due_before, stale_before, 10, user_id=ANNA)
        boris = await get_followups(session, due_before, stale_before, 10, user_id=BORIS)
        anna_counts = await get_digest_counts(session, due_before, stale_before, user_id=ANNA)
        boris_counts = await get_digest_counts(session, due_before, stale_before, user_id=BORIS)

    assert [lead.id for lead in anna] == [1]
    assert sorted(lead.id for lead in boris) == [2, 3]
    assert (anna_counts.reminders_due, anna_counts.written_no_answer) == (1, 0)
    assert (boris_counts.reminders_due, boris_counts.written_no_answer) == (1, 1)
    assert anna_counts.useful_in_pool == boris_counts.useful_in_pool == 1


async def test_people_stats_for_the_owner(session_maker) -> None:
    await _add(
        session_maker,
        _issued(1, to=ANNA, owner_user_id=ANNA, owner_name="@anna", contact_status=CONTACT_CONTACTED),
        _issued(2, to=ANNA, owner_user_id=ANNA, owner_name="@anna", contact_status=CONTACT_REPLIED),
        _issued(3, to=BORIS, contact_status=CONTACT_REJECTED, contact_status_by_id=BORIS, contact_status_by="@boris"),
        _issued(4, to=BORIS),
    )

    async with session_maker() as session:
        stats = await get_people_stats(session)

    assert [(p.user_id, p.name, p.in_work, p.replied, p.rejected) for p in stats] == [
        (ANNA, "@anna", 1, 1, 0),
        (BORIS, "@boris", 1, 0, 1),
    ]


# ---------- the migration ----------


async def test_leads_written_to_before_this_version_become_the_owners_once(session_maker) -> None:
    tonight = NOW + timedelta(hours=12)
    await _add(
        session_maker,
        _issued(1, to=None, contact_status=CONTACT_CONTACTED, contact_status_at=NOW - timedelta(days=2)),
        _issued(2, to=None, contact_status=CONTACT_CONTACTED, contact_status_at=NOW - timedelta(days=40)),
        _issued(3, to=None, contact_status=CONTACT_REPLIED, contact_status_at=NOW - timedelta(days=50)),
        _issued(4, to=None),
        _issued(5, to=ANNA, owner_user_id=ANNA, contact_status=CONTACT_CONTACTED, owned_until=NOW + timedelta(days=9)),
        _lead(6),
    )

    async with session_maker() as session:
        first = await assign_existing_leads(session, OWNER, DAYS, tonight)
    async with session_maker() as session:
        second = await assign_existing_leads(session, OWNER, DAYS, tonight)

    assert first > 0 and second == 0
    one, two, three, four, five, six = [await _get(session_maker, n) for n in range(1, 7)]
    assert one.owner_user_id == OWNER and one.owned_until is not None
    assert abs((one.owned_until - (NOW + timedelta(days=DAYS - 2))).total_seconds()) < 5
    assert two.owner_user_id == OWNER and two.owned_until == tonight  # not before tonight: nothing goes back at once
    assert (three.owner_user_id, three.owned_until) == (OWNER, None)
    assert (four.issued_to_user_id, four.owner_user_id) == (OWNER, None)
    assert (five.owner_user_id, five.issued_to_user_id) == (ANNA, ANNA)  # already someone's: untouched
    assert six.issued_to_user_id is None


# ---------- who may do what ----------


def test_who_may_press_the_buttons_of_a_lead() -> None:
    anna_lead = _issued(1, to=ANNA)
    nobodys = _issued(2, to=None)

    assert can_act(anna_lead, ANNA, OWNER)
    assert not can_act(anna_lead, BORIS, OWNER)
    assert can_act(anna_lead, OWNER, OWNER)  # the owner of the bot sees and frees anybody's leads
    assert can_act(nobodys, BORIS, OWNER)


@pytest.fixture
def env(session_maker, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(issue, "async_session_maker", session_maker)
    monkeypatch.setattr(reminders, "async_session_maker", session_maker)
    monkeypatch.setattr(issue, "settings", _settings())
    monkeypatch.setattr(reminders, "settings", _settings())
    return session_maker


def _callback(data: str, user_id: int) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = User(id=user_id, is_bot=False, first_name="U", username=f"u{user_id}")
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    return callback


@pytest.mark.parametrize("data", ["ls:1:replied", "rm:1:3", "dg:1:done", "rp:1", "sv:1:yes", "lp:1"])
async def test_someone_elses_lead_cannot_be_changed(env, data: str) -> None:
    await _add(env, _issued(1, to=ANNA, owner_user_id=ANNA, owner_name="@anna", contact_status=CONTACT_CONTACTED))
    handlers = {
        "ls": issue.handle_lead_status,
        "rm": reminders.handle_remind,
        "dg": reminders.handle_followup,
        "rp": issue.handle_release,
        "sv": issue.handle_site_verdict,
        "lp": issue.handle_add_phone,
    }
    callback = _callback(data, BORIS)

    await handlers[data.split(":")[0]](callback)

    assert callback.answer.await_args.args[0] == "Этот лид в работе у @anna"
    lead = await _get(env, 1)
    assert (lead.contact_status, lead.crm_status, lead.remind_at, lead.site_status) == (
        CONTACT_CONTACTED,
        STATUS_ISSUED,
        None,
        SITE_NO_SITE,
    )


async def test_the_person_gives_their_lead_back_to_the_pool(env) -> None:
    await _add(env, _issued(1, to=ANNA, owner_user_id=ANNA, contact_status=CONTACT_CONTACTED))
    callback = _callback("rp:1", ANNA)

    await issue.handle_release(callback)

    assert callback.answer.await_args.args[0] == "Лид вернулся в пул"
    assert (await _get(env, 1)).crm_status == STATUS_NEW


async def test_a_replied_lead_cannot_be_given_back(env) -> None:
    await _add(env, _issued(1, to=ANNA, owner_user_id=ANNA, contact_status=CONTACT_REPLIED))
    callback = _callback("rp:1", OWNER)

    await issue.handle_release(callback)

    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert (await _get(env, 1)).crm_status == STATUS_ISSUED


def test_the_release_button_is_on_a_lead_in_work_only() -> None:
    from src.bot.keyboards import get_issued_lead_keyboard

    def data(lead: Lead) -> list[str]:
        return [b.callback_data or "" for row in get_issued_lead_keyboard(lead).inline_keyboard for b in row]

    assert "rp:1" in data(_issued(1, contact_status=CONTACT_CONTACTED))
    assert "rp:1" not in data(_issued(1, contact_status=CONTACT_REPLIED))


# ---------- roles through the dispatcher ----------


def _update(user_id: int, text: str) -> Update:
    entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else None
    message = Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        text=text,
        entities=entities,
    )
    return Update(update_id=1, message=message)


@pytest.fixture
def dispatcher(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.bot.middlewares.settings", _settings())
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    dp = Dispatcher()
    guard = AdminOnlyMiddleware()
    dp.message.middleware(guard)
    dp.callback_query.middleware(guard)
    routers = (issue.router, collector_handlers.router, reminders.router)
    for router in routers:
        dp.include_router(router)
    dp.answer = answer
    yield dp
    for router in routers:
        router._parent_router = None
    dp.sub_routers.clear()


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.id = 1
    bot.me = AsyncMock(return_value=User(id=1, is_bot=True, first_name="Bot", username="spb_lead_bot"))
    return bot


@pytest.mark.parametrize(
    "command", ["/rescore", "/collect_now", "/collect_on", "/collect_off", "/provider_on", "/team"]
)
async def test_an_operator_cannot_change_shared_data(dispatcher, monkeypatch, command: str) -> None:
    def must_not_run(*args, **kwargs):
        raise AssertionError("an operator reached a command of the owner")

    monkeypatch.setattr(issue, "async_session_maker", must_not_run)
    monkeypatch.setattr(reminders, "async_session_maker", must_not_run)
    runner = MagicMock()

    await dispatcher.feed_update(_bot(), _update(ANNA, command), collector_runner=runner)

    dispatcher.answer.assert_awaited_once_with(OWNER_ONLY_TEXT)
    assert not runner.start_in_background.called and not runner.set_auto_enabled.called


async def test_the_owner_may_start_a_collection(dispatcher) -> None:
    runner = MagicMock()
    runner.external_mode = False
    runner.start_in_background = MagicMock(return_value=True)

    await dispatcher.feed_update(_bot(), _update(OWNER, "/collect_now"), collector_runner=runner)

    assert runner.start_in_background.called


async def test_an_operator_may_ask_for_the_plan_of_the_day(dispatcher) -> None:
    digest = MagicMock()
    digest.build = AsyncMock(return_value=[("план", None)])

    await dispatcher.feed_update(_bot(), _update(BORIS, "/today"), collector_runner=MagicMock(), daily_digest=digest)

    digest.build.assert_awaited_once_with(user_id=BORIS)
    dispatcher.answer.assert_awaited_once()


async def test_an_operator_brings_enrichment_but_not_new_leads(monkeypatch) -> None:
    monkeypatch.setattr("src.bot.middlewares.settings", _settings())

    def message(user_id: int) -> MagicMock:
        item = MagicMock()
        item.from_user = User(id=user_id, is_bot=False, first_name="U")
        status = MagicMock()
        status.edit_text = AsyncMock()
        item.answer = AsyncMock(return_value=status)
        item.document = MagicMock(file_size=10, file_name="leads.json")
        item.status = status
        return item

    async def download(document, destination) -> None:
        destination.write(b'{"leads": []}')

    bot = MagicMock()
    bot.download = AsyncMock(side_effect=download)
    service = MagicMock(max_bytes=10_000)
    service.import_bytes = AsyncMock()

    operator = message(ANNA)
    await imports.handle_lead_file(operator, bot, service)
    service.import_bytes.assert_not_awaited()
    assert "владелец" in operator.status.edit_text.await_args.args[0]


def test_an_operator_does_not_see_commands_of_the_owner_in_help() -> None:
    operator = "\n".join(format_help(is_admin=True, is_owner=False))
    owner = "\n".join(format_help(is_admin=True, is_owner=True))

    for command in ("/rescore", "/collect_now", "/provider_on", "/team"):
        assert command not in operator
        assert f"{command} —" in owner
    assert "(только владелец)" in owner and "/today" in operator
