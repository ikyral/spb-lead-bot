"""Ownership of leads: issuing, statuses, return to the pool, the export reserve, roles and the migration."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import CallbackQuery, Chat, Document, Message, User
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.bot.handlers import issue
from src.bot.middlewares import NO_ACCESS_TEXT, AdminOnlyMiddleware
from src.bot.roles import command_name, is_owner_only
from src.config import Settings
from src.database.models import (
    CONTACT_CONTACTED,
    CONTACT_NO_ANSWER,
    CONTACT_REJECTED,
    CONTACT_REPLIED,
    STATUS_ISSUED,
    STATUS_NEW,
    Base,
    Lead,
)
from src.database.repo import (
    NEW_LEAD_COLUMNS,
    OUTCOME_MISSING,
    OUTCOME_NOT_YOURS,
    OUTCOME_OK,
    OUTCOME_OWNED,
    OUTCOME_SAME,
    change_contact_status,
    count_reserved_for_others,
    get_owned_in_area,
    get_owner_summary,
    get_team_stats,
    init_db,
    issue_next_leads,
    migrate_legacy_ownership,
    release_expired_leads,
    release_lead,
    reserve_leads_for_export,
    return_undelivered_leads,
)
from src.services.collector import load_collector_config
from src.services.niche_priority import MAX_LEADS_PER_NICHE, reserve_export
from src.services.ownership import RELEASE_INTERVAL_SECONDS, RELEASE_JOB_ID, release_expired_job
from src.services.providers.registry import ProviderSet
from src.services.scheduler import CollectorRunner

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
OWNER, ANNA, BORIS, STRANGER = 100, 200, 300, 999
DAYS = 30


def _lead(number: int, **fields) -> Lead:
    values = {
        "external_id": f"2gis_{number}",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": "dentist_clinic",
        "district": "Центральный",
        "site_status": "no_site",
        "score": 50,
        "crm_status": STATUS_NEW,
        "created_at": datetime(2026, 11, 5, 10, 0, tzinfo=UTC) + timedelta(minutes=number),
    }
    values.update(fields)
    return Lead(**values)


async def _add(session_maker, *leads: Lead) -> None:
    async with session_maker() as session:
        session.add_all(leads)
        await session.commit()


async def _add_pool(session_maker, count: int) -> None:
    await _add(session_maker, *(_lead(number) for number in range(1, count + 1)))


async def _get(session_maker, lead_id: int) -> Lead:
    async with session_maker() as session:
        lead = await session.get(Lead, lead_id)
    assert lead is not None
    return lead


async def _issue(session_maker, limit: int, user_id: int | None, now: datetime = NOW) -> list[Lead]:
    async with session_maker() as session:
        return await issue_next_leads(session, limit, user_id=user_id, now=now)


async def _status(session_maker, lead_id: int, status: str, user_id: int, now: datetime = NOW, **kwargs):
    async with session_maker() as session:
        return await change_contact_status(
            session, lead_id, status, user_id, f"@user{user_id}", now=now, days=DAYS, **kwargs
        )


async def _issued_to(session_maker, user_id: int) -> int:
    await _add_pool(session_maker, 1)
    return (await _issue(session_maker, 1, user_id))[0].id


# ---------- issuing ----------


async def test_batches_of_different_people_never_overlap_and_remember_whom(session_maker) -> None:
    await _add_pool(session_maker, 7)
    anna = await _issue(session_maker, 3, ANNA)
    boris = await _issue(session_maker, 3, BORIS)
    assert not {lead.id for lead in anna} & {lead.id for lead in boris}
    assert {lead.issued_to_user_id for lead in anna} == {ANNA}
    assert {lead.issued_to_user_id for lead in boris} == {BORIS}
    assert all(lead.issued_at == NOW and lead.crm_status == STATUS_ISSUED for lead in anna + boris)


async def test_concurrent_batches_of_different_people_never_overlap(session_maker) -> None:
    await _add_pool(session_maker, 40)
    batches = await asyncio.gather(*(_issue(session_maker, 10, user_id) for user_id in range(1, 7)))
    issued = [lead.id for batch in batches for lead in batch]
    assert len(issued) == 40 and len(set(issued)) == 40
    for user_id, batch in zip(range(1, 7), batches, strict=True):
        assert all(lead.issued_to_user_id == user_id for lead in batch)
    async with session_maker() as session:
        rows = (await session.execute(text("SELECT issued_to_user_id, COUNT(*) FROM leads GROUP BY 1"))).all()
    assert sum(count for _, count in rows) == 40 and None not in {user_id for user_id, _ in rows}


async def test_a_failed_batch_is_rolled_back_whole(session_maker, monkeypatch) -> None:
    await _add_pool(session_maker, 3)
    from src.database import repo

    original = repo._pick_leads
    calls = 0

    async def fail_on_second_pick(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("boom")
        return (await original(*args, **kwargs))[:1]

    monkeypatch.setattr(repo, "_pick_leads", fail_on_second_pick)
    with pytest.raises(RuntimeError):
        await _issue(session_maker, 3, ANNA)
    monkeypatch.setattr(repo, "_pick_leads", original)
    assert len(await _issue(session_maker, 3, BORIS)) == 3  # nothing was taken by the failed batch


async def test_undelivered_leads_forget_whom_they_were_issued_to(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    async with session_maker() as session:
        await return_undelivered_leads(session, [lead_id])
    lead = await _get(session_maker, lead_id)
    assert (lead.crm_status, lead.issued_at, lead.issued_to_user_id) == (STATUS_NEW, None, None)


async def test_leads_reserved_by_another_person_are_skipped_until_the_reserve_ends(session_maker) -> None:
    until = NOW + timedelta(hours=6)
    await _add(
        session_maker,
        _lead(1, reserved_by=ANNA, reserved_until=until),
        _lead(2, reserved_by=BORIS, reserved_until=until),
        _lead(3),
    )
    assert [lead.id for lead in await _issue(session_maker, 10, BORIS)] == [2, 3]
    # Exactly at reserved_until the reserve is over.
    assert [lead.id for lead in await _issue(session_maker, 10, STRANGER, now=until)] == [1]


async def test_batch_without_a_person_skips_every_active_reserve(session_maker) -> None:
    await _add(session_maker, _lead(1, reserved_by=ANNA, reserved_until=NOW + timedelta(minutes=1)), _lead(2))
    assert [lead.id for lead in await _issue(session_maker, 10, None)] == [2]


# ---------- statuses and ownership ----------


async def test_wrote_gives_thirty_days_replied_no_expiry_no_answer_one_day(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)

    assert (await _status(session_maker, lead_id, CONTACT_CONTACTED, ANNA)).outcome == OUTCOME_OK
    lead = await _get(session_maker, lead_id)
    assert (lead.owner_user_id, lead.owner_name, lead.owned_until) == (ANNA, f"@user{ANNA}", NOW + timedelta(days=30))

    assert (await _status(session_maker, lead_id, CONTACT_NO_ANSWER, ANNA)).outcome == OUTCOME_OK
    assert (await _get(session_maker, lead_id)).owned_until == NOW + timedelta(days=1)

    assert (await _status(session_maker, lead_id, CONTACT_REPLIED, ANNA)).outcome == OUTCOME_OK
    lead = await _get(session_maker, lead_id)
    assert (lead.owner_user_id, lead.owned_until) == (ANNA, None)


async def test_rejected_keeps_the_owner_without_expiry(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    await _status(session_maker, lead_id, CONTACT_REJECTED, ANNA)
    lead = await _get(session_maker, lead_id)
    assert (lead.owner_user_id, lead.owned_until, lead.contact_status) == (ANNA, None, CONTACT_REJECTED)


async def test_another_person_is_refused_while_the_owner_is_active_and_nothing_changes(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    await _status(session_maker, lead_id, CONTACT_CONTACTED, ANNA)
    before = await _get(session_maker, lead_id)

    for status in (CONTACT_CONTACTED, CONTACT_REPLIED, CONTACT_REJECTED, CONTACT_NO_ANSWER):
        change = await _status(session_maker, lead_id, status, BORIS, now=NOW + timedelta(days=29))
        assert change.outcome == OUTCOME_OWNED
        assert change.lead is not None and change.lead.owner_name == f"@user{ANNA}"
    after = await _get(session_maker, lead_id)
    assert (after.contact_status, after.owner_user_id, after.owned_until) == (
        before.contact_status,
        before.owner_user_id,
        before.owned_until,
    )


async def test_the_owner_of_the_bot_takes_over_a_lead_someone_else_works(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    await _status(session_maker, lead_id, CONTACT_CONTACTED, ANNA)

    owner = await _status(session_maker, lead_id, CONTACT_REPLIED, OWNER, owner_role=True)
    assert owner.outcome == OUTCOME_OK
    lead = await _get(session_maker, lead_id)
    assert (lead.owner_user_id, lead.contact_status, lead.owned_until) == (OWNER, CONTACT_REPLIED, None)
    # Now the operator is the one refused.
    assert (await _status(session_maker, lead_id, CONTACT_CONTACTED, ANNA)).outcome == OUTCOME_OWNED


async def test_after_expiry_another_person_takes_the_lead_over_before_the_scheduler_runs(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    await _status(session_maker, lead_id, CONTACT_CONTACTED, ANNA)
    expiry = NOW + timedelta(days=30)

    early = await _status(session_maker, lead_id, CONTACT_CONTACTED, BORIS, now=expiry - timedelta(microseconds=1))
    assert early.outcome == OUTCOME_OWNED
    # 'Expired' means owned_until <= now: exactly at the boundary it is free.
    taken = await _status(session_maker, lead_id, CONTACT_CONTACTED, BORIS, now=expiry)
    assert taken.outcome == OUTCOME_OK
    lead = await _get(session_maker, lead_id)
    assert (lead.owner_user_id, lead.owned_until) == (BORIS, expiry + timedelta(days=30))


async def test_lead_issued_to_someone_else_is_refused_to_an_operator_but_not_to_the_owner(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    assert (await _status(session_maker, lead_id, CONTACT_CONTACTED, BORIS)).outcome == OUTCOME_NOT_YOURS
    assert (await _get(session_maker, lead_id)).owner_user_id is None
    owner = await _status(session_maker, lead_id, CONTACT_CONTACTED, OWNER, owner_role=True)
    assert owner.outcome == OUTCOME_OK
    assert (await _get(session_maker, lead_id)).owner_user_id == OWNER


async def test_the_same_press_twice_changes_nothing(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    await _status(session_maker, lead_id, CONTACT_CONTACTED, ANNA)
    again = await _status(session_maker, lead_id, CONTACT_CONTACTED, ANNA, now=NOW + timedelta(days=5))
    assert again.outcome == OUTCOME_SAME
    assert (await _get(session_maker, lead_id)).owned_until == NOW + timedelta(days=30)


async def test_no_answer_then_wrote_again_renews_the_ownership(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    await _status(session_maker, lead_id, CONTACT_NO_ANSWER, ANNA)
    later = NOW + timedelta(hours=20)
    assert (await _status(session_maker, lead_id, CONTACT_CONTACTED, ANNA, now=later)).outcome == OUTCOME_OK
    assert (await _get(session_maker, lead_id)).owned_until == later + timedelta(days=30)


async def test_status_of_a_lead_that_is_not_issued_is_stale(session_maker) -> None:
    await _add_pool(session_maker, 1)
    assert (await _status(session_maker, 1, CONTACT_CONTACTED, ANNA)).outcome == OUTCOME_MISSING
    assert (await _status(session_maker, 999, CONTACT_CONTACTED, ANNA)).outcome == OUTCOME_MISSING


# ---------- return to the pool ----------


async def test_scheduler_returns_only_expired_leads_that_did_not_reply(session_maker) -> None:
    for number in range(1, 6):
        await _add(session_maker, _lead(number))
    await _issue(session_maker, 5, ANNA)
    await _status(session_maker, 1, CONTACT_CONTACTED, ANNA)  # 30 days
    await _status(session_maker, 2, CONTACT_NO_ANSWER, ANNA)  # 1 day
    await _status(session_maker, 3, CONTACT_REPLIED, ANNA)  # never
    await _status(session_maker, 4, CONTACT_REJECTED, ANNA)  # never
    # 5: handed out and never pressed: stays with Anna as before.

    async with session_maker() as session:
        assert await release_expired_leads(session, NOW + timedelta(hours=23)) == 0
        assert await release_expired_leads(session, NOW + timedelta(days=1)) == 1  # exactly at owned_until
        assert await release_expired_leads(session, NOW + timedelta(days=365)) == 1

    statuses = {number: (await _get(session_maker, number)).crm_status for number in range(1, 6)}
    assert statuses == {1: STATUS_NEW, 2: STATUS_NEW, 3: STATUS_ISSUED, 4: STATUS_ISSUED, 5: STATUS_ISSUED}
    released = await _get(session_maker, 2)
    assert (
        released.issued_at,
        released.issued_to_user_id,
        released.owner_user_id,
        released.owner_name,
        released.owned_until,
        released.contact_status,
        released.contact_status_by_id,
        released.contact_status_by,
        released.contact_status_at,
    ) == (None,) * 9

    again = await _issue(session_maker, 10, BORIS, now=NOW + timedelta(days=366))
    assert {lead.id for lead in again} == {1, 2}


async def test_replied_is_never_released_even_with_an_expiry(session_maker) -> None:
    await _add(
        session_maker,
        _lead(
            1,
            crm_status=STATUS_ISSUED,
            issued_at=NOW,
            contact_status=CONTACT_REPLIED,
            owner_user_id=ANNA,
            owned_until=NOW - timedelta(days=1),
        ),
    )
    async with session_maker() as session:
        assert await release_expired_leads(session, NOW) == 0


async def test_release_job_logs_and_never_raises(session_maker, caplog) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    await _status(session_maker, lead_id, CONTACT_NO_ANSWER, ANNA)
    with caplog.at_level(logging.INFO, logger="src.services.ownership"):
        assert await release_expired_job(session_maker, NOW + timedelta(days=2)) == 1
    assert "Returned to the pool" in caplog.text

    def broken():
        raise RuntimeError("database is gone")

    assert await release_expired_job(broken) == 0  # type: ignore[arg-type]


async def test_return_button_works_for_the_owner_and_is_refused_to_another_operator(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    await _status(session_maker, lead_id, CONTACT_CONTACTED, ANNA)
    async with session_maker() as session:
        refused = await release_lead(session, lead_id, BORIS)
    assert refused.outcome == OUTCOME_OWNED
    assert (await _get(session_maker, lead_id)).crm_status == STATUS_ISSUED

    async with session_maker() as session:
        assert (await release_lead(session, lead_id, ANNA)).outcome == OUTCOME_OK
    lead = await _get(session_maker, lead_id)
    assert (lead.crm_status, lead.owner_user_id, lead.contact_status) == (STATUS_NEW, None, None)
    async with session_maker() as session:
        assert (await release_lead(session, lead_id, ANNA)).outcome == OUTCOME_MISSING  # an old card


async def test_return_button_of_the_owner_of_the_bot_and_of_the_person_it_was_issued_to(session_maker) -> None:
    await _add_pool(session_maker, 2)
    first, second = (lead.id for lead in await _issue(session_maker, 2, ANNA))
    async with session_maker() as session:
        assert (await release_lead(session, first, BORIS)).outcome == OUTCOME_NOT_YOURS
        assert (await release_lead(session, first, ANNA)).outcome == OUTCOME_OK  # issued to her, not pressed
    await _status(session_maker, second, CONTACT_REPLIED, ANNA)
    async with session_maker() as session:
        assert (await release_lead(session, second, OWNER, owner_role=True)).outcome == OUTCOME_OK


# ---------- time ----------


async def test_timestamps_come_back_as_aware_utc(session_maker) -> None:
    lead_id = await _issued_to(session_maker, ANNA)
    moscow = datetime(2026, 11, 15, 15, 0, tzinfo=UTC).astimezone(ZoneInfo("Europe/Moscow"))
    await _status(session_maker, lead_id, CONTACT_CONTACTED, ANNA, now=moscow)
    lead = await _get(session_maker, lead_id)
    assert lead.owned_until is not None and lead.owned_until.tzinfo == UTC
    assert lead.owned_until == datetime(2026, 12, 15, 15, 0, tzinfo=UTC)
    async with session_maker() as session:
        stored = (await session.execute(text("SELECT owned_until FROM leads"))).scalar_one()
    assert str(stored).startswith("2026-12-15 15:00:00")  # naive UTC on disk


# ---------- export reserve ----------


def _unknown(number: int) -> Lead:
    """A lead for an enrichment request file: nothing known about the site, a card on 2GIS to open."""
    return _lead(number, site_status="unknown", source_url=f"https://2gis.ru/spb/firm/7{number}")


async def _export(session_maker, limit: int, user_id: int, now: datetime = NOW) -> list[int]:
    async with session_maker() as session:
        plan = await reserve_export(session, limit, None, user_id, 6, now)
    return [lead.id for lead in plan.leads]


async def test_two_people_exporting_get_disjoint_files_and_the_same_person_gets_the_same(session_maker) -> None:
    await _add(session_maker, *(_unknown(number) for number in range(1, 7)))
    anna = await _export(session_maker, 4, ANNA)
    boris = await _export(session_maker, 4, BORIS, NOW + timedelta(hours=1))
    anna_again = await _export(session_maker, 4, ANNA, NOW + timedelta(hours=2))
    assert len(anna) == 4 and set(boris) == set(range(1, 7)) - set(anna)
    assert set(anna_again) == set(anna)
    for lead_id in anna:
        lead = await _get(session_maker, lead_id)
        assert (lead.reserved_by, lead.reserved_until) == (ANNA, NOW + timedelta(hours=8))  # extended


async def test_concurrent_exports_never_overlap(session_maker) -> None:
    await _add(session_maker, *(_unknown(number) for number in range(1, 31)))
    first, second = await asyncio.gather(_export(session_maker, 20, ANNA), _export(session_maker, 20, BORIS))
    assert not set(first) & set(second)
    # One niche gives at most MAX_LEADS_PER_NICHE to a file; whoever lost a race got other leads instead.
    assert len(first) == len(second) == MAX_LEADS_PER_NICHE


async def test_after_the_reserve_another_person_can_export_and_batches_skip_it_meanwhile(session_maker) -> None:
    await _add(session_maker, _unknown(1), _unknown(2))
    assert await _export(session_maker, 30, ANNA) == [1, 2]
    async with session_maker() as session:
        assert await count_reserved_for_others(session, BORIS, NOW) == 2
        assert await count_reserved_for_others(session, ANNA, NOW) == 0
        # A plan chosen before Anna's reserve still cannot take her leads.
        assert await reserve_leads_for_export(session, [2, 1], BORIS, 6, NOW) == []
    assert await _export(session_maker, 30, BORIS, NOW + timedelta(hours=5)) == []
    assert await _issue(session_maker, 10, BORIS, now=NOW + timedelta(hours=5)) == []
    assert len(await _issue(session_maker, 1, ANNA, now=NOW + timedelta(hours=5))) == 1  # her own reserve
    # Exactly at reserved_until the reserve is over.
    assert await _export(session_maker, 30, BORIS, NOW + timedelta(hours=6)) == [2]


async def test_the_csv_export_of_new_leads_reserves_nothing(session_maker, monkeypatch) -> None:
    from src.bot.handlers import leads as leads_handlers

    await _add_pool(session_maker, 2)
    monkeypatch.setattr(leads_handlers, "async_session_maker", session_maker)
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "export:new"
    callback.from_user = User(id=OWNER, is_bot=False, first_name="U")
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.answer_document = AsyncMock()
    await leads_handlers.handle_export_csv(callback)
    callback.message.answer_document.assert_awaited_once()
    assert (await _get(session_maker, 1)).reserved_by is None
    assert len(await _issue(session_maker, 10, BORIS)) == 2


async def test_enrich_request_file_holds_only_the_reserved_leads(session_maker, monkeypatch) -> None:
    from src.bot.handlers import imports

    await _add(session_maker, _unknown(1), _unknown(2), _unknown(3))
    monkeypatch.setattr(imports, "async_session_maker", session_maker)

    def message(user_id: int) -> MagicMock:
        sent = MagicMock(spec=Message)
        sent.from_user = User(id=user_id, is_bot=False, first_name="U")
        sent.answer = AsyncMock()
        sent.answer_document = AsyncMock()
        return sent

    def ids_in_file(sent: MagicMock) -> set[str]:
        document = sent.answer_document.await_args.args[0]
        payload = json.loads(document.data.decode("utf-8"))
        return {str(record["source_id"]) for record in payload["records"]}

    anna, boris = message(ANNA), message(BORIS)
    await imports.send_enrich_request(anna, 2)
    await imports.send_enrich_request(boris, 2)
    assert len(ids_in_file(anna)) == 2 and len(ids_in_file(boris)) == 1
    assert not ids_in_file(anna) & ids_in_file(boris)


# ---------- summaries ----------


async def test_owner_summary_team_and_the_marker_of_the_search(session_maker) -> None:
    await _add_pool(session_maker, 3)
    await _issue(session_maker, 3, ANNA)
    await _status(session_maker, 1, CONTACT_CONTACTED, ANNA)
    await _status(session_maker, 2, CONTACT_NO_ANSWER, ANNA)
    await _status(session_maker, 3, CONTACT_REPLIED, ANNA)

    async with session_maker() as session:
        summary = await get_owner_summary(session, ANNA, NOW)
        team = await get_team_stats(session, NOW)
        area = await get_owned_in_area(session, "Центральный", "dentist_clinic", NOW)
        nobody = await get_owner_summary(session, BORIS, NOW)
    assert summary.by_status == {CONTACT_CONTACTED: 1, CONTACT_NO_ANSWER: 1, CONTACT_REPLIED: 1}
    assert (summary.active, summary.replied, summary.expiring_total) == (2, 1, 1)
    assert [lead.id for lead in summary.expiring] == [2]
    assert [(row.owner_name, row.active, row.replied, row.expiring) for row in team] == [(f"@user{ANNA}", 2, 1, 1)]
    assert area == [(f"@user{ANNA}", 3, None)]  # 'replied' has no expiry
    assert nobody.by_status == {} and nobody.expiring == []


async def test_team_without_any_leads(session_maker) -> None:
    async with session_maker() as session:
        assert await get_team_stats(session, NOW) == []


# ---------- migration of older leads ----------


async def test_legacy_leads_go_to_the_owner_once(session_maker) -> None:
    issued = {"crm_status": STATUS_ISSUED, "issued_at": NOW - timedelta(days=3)}
    await _add(
        session_maker,
        _lead(1, contact_status=CONTACT_CONTACTED, contact_status_by="@anna", **issued),
        _lead(2, contact_status=CONTACT_REPLIED, **issued),
        _lead(3, contact_status=CONTACT_REJECTED, **issued),
        _lead(4, contact_status=CONTACT_NO_ANSWER, **issued),
        _lead(5, **issued),
        _lead(6),
        _lead(
            7,
            contact_status=CONTACT_CONTACTED,
            owner_user_id=BORIS,
            owner_name="@boris",
            owned_until=NOW + timedelta(days=2),
            **issued,
        ),
    )
    async with session_maker() as session:
        assert await migrate_legacy_ownership(session, OWNER, NOW, days=DAYS) == 2
    first, second = await _get(session_maker, 1), await _get(session_maker, 2)
    assert (first.owner_user_id, first.owner_name, first.issued_to_user_id, first.owned_until) == (
        OWNER,
        "@anna",
        OWNER,
        NOW + timedelta(days=30),
    )
    assert (second.owner_user_id, second.owner_name, second.owned_until) == (OWNER, "owner", None)
    for number in (3, 4, 5, 6):
        assert (await _get(session_maker, number)).owner_user_id is None
    untouched = await _get(session_maker, 7)
    assert (untouched.owner_user_id, untouched.owned_until) == (BORIS, NOW + timedelta(days=2))

    # A second start changes nothing and extends nothing.
    async with session_maker() as session:
        assert await migrate_legacy_ownership(session, OWNER, NOW + timedelta(days=10), days=DAYS) == 0
    assert (await _get(session_maker, 1)).owned_until == NOW + timedelta(days=30)


async def test_migration_is_skipped_without_an_owner(session_maker) -> None:
    await _add(session_maker, _lead(1, crm_status=STATUS_ISSUED, issued_at=NOW, contact_status=CONTACT_CONTACTED))
    async with session_maker() as session:
        assert await migrate_legacy_ownership(session, 0, NOW) == 0
    assert (await _get(session_maker, 1)).owner_user_id is None


async def test_old_table_gets_the_six_columns_and_the_index(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    owned = ("issued_to_user_id", "owner_user_id", "owner_name", "owned_until", "reserved_by", "reserved_until")
    assert set(owned) <= set(NEW_LEAD_COLUMNS)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP INDEX ix_leads_owner_owned_until"))
        for name in owned:
            await conn.execute(text(f"ALTER TABLE leads DROP COLUMN {name}"))
        await conn.execute(
            text(
                "INSERT INTO leads (external_id, source, name, category, site_status, score, crm_status) "
                "VALUES ('osm_1', 'osm', 'Старый', 'dentist_clinic', 'no_site', 50, 'new')"
            )
        )
    try:
        await init_db(engine)
        await init_db(engine)  # a restart must not fail
        async with engine.connect() as conn:
            columns = {row[1] for row in await conn.execute(text("PRAGMA table_info(leads)"))}
            indexes = {row[1] for row in await conn.execute(text("PRAGMA index_list(leads)"))}
        assert set(owned) <= columns
        assert "ix_leads_owner_owned_until" in indexes
        session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        batch = await _issue(session_maker, 1, ANNA)
        assert batch[0].name == "Старый" and batch[0].issued_to_user_id == ANNA
    finally:
        await engine.dispose()


# ---------- settings ----------


def test_new_settings_have_defaults_and_reject_zero() -> None:
    settings = Settings(_env_file=None)
    assert (settings.LEAD_OWNERSHIP_DAYS, settings.EXPORT_RESERVE_HOURS) == (30, 6)
    assert Settings(_env_file=None, LEAD_OWNERSHIP_DAYS="").LEAD_OWNERSHIP_DAYS == 30
    for name in ("LEAD_OWNERSHIP_DAYS", "EXPORT_RESERVE_HOURS"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{name: 0})


def test_owner_and_operators() -> None:
    settings = Settings(_env_file=None, ADMIN_TG_ID=OWNER, ADMIN_TG_IDS=f"{ANNA}, {BORIS},{OWNER}")
    assert settings.owner_id == OWNER
    assert settings.operator_ids == frozenset({ANNA, BORIS})
    assert settings.admin_ids == frozenset({OWNER, ANNA, BORIS})
    assert settings.is_owner(OWNER) and not settings.is_owner(ANNA) and not settings.is_owner(None)
    assert not Settings(_env_file=None).is_owner(0)


# ---------- roles ----------


def _message(user_id: int, text_: str | None = None, document: Document | None = None) -> Message:
    return Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        text=text_,
        document=document,
    )


def _callback(user_id: int, data: str) -> CallbackQuery:
    return CallbackQuery(id="1", from_user=User(id=user_id, is_bot=False, first_name="U"), chat_instance="c", data=data)


_DOCUMENT = Document(file_id="f", file_unique_id="u", file_name="leads.json")
OWNER_ONLY_EVENTS = [
    ("/rescore", None),
    ("/rescore@spb_lead_bot", None),
    ("/RESCORE now", None),
    ("/collect_now", None),
    ("/collect_status", None),
    ("/collect_on", None),
    ("/collect_off", None),
    ("/provider_on 2gis", None),
    ("/team", None),
    ("💼 В работе", None),
    ("📊 Статистика и CSV", None),
    (None, "export:new"),
    (None, "export:all"),
    (None, "export:in_work"),
    (None, "panel:collect"),
    (None, "panel:status"),
    ("document", None),
]
OPERATOR_EVENTS = [
    ("/next_leads", None),
    ("📦 Следующие 30", None),
    ("/today", None),
    ("/plan", None),
    ("/leads_stats", None),
    ("/start", None),
    ("/chat_id", None),
    ("/version", None),
    ("/export_unknown 30", None),
    ("📋 Новые лиды", None),
    ("🔍 Поиск по СПб", None),
    (None, "panel:next"),
    (None, "panel:stats"),
    (None, "ls:1:contacted"),
    (None, "sv:1:yes"),
    (None, "lp:1"),
    (None, "lr:1"),
    (None, "lead:1:work:0"),
]


def _event(user_id: int, text_: str | None, data: str | None) -> Message | CallbackQuery:
    if data is not None:
        return _callback(user_id, data)
    if text_ == "document":
        return _message(user_id, document=_DOCUMENT)
    return _message(user_id, text_)


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> AdminOnlyMiddleware:
    monkeypatch.setattr(
        "src.bot.middlewares.settings",
        Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=OWNER, ADMIN_TG_IDS=f"{ANNA},{BORIS}"),
    )
    monkeypatch.setattr(Message, "answer", AsyncMock())
    monkeypatch.setattr(Message, "reply", AsyncMock())
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    return AdminOnlyMiddleware()


@pytest.mark.parametrize(("text_", "data"), OWNER_ONLY_EVENTS + OPERATOR_EVENTS)
@pytest.mark.parametrize("user_id", [OWNER, ANNA, STRANGER])
async def test_access_matrix(guard, user_id: int, text_: str | None, data: str | None) -> None:
    handler = AsyncMock(return_value="handled")
    event = _event(user_id, text_, data)
    result = await guard(handler, event, {"event_from_user": event.from_user})

    owner_only = (text_, data) in OWNER_ONLY_EVENTS
    allowed = user_id == OWNER or (user_id == ANNA and not owner_only)
    assert (result == "handled") is allowed
    assert handler.await_count == int(allowed)


async def test_operator_gets_no_access_as_an_alert_and_a_reply(guard) -> None:
    await guard(AsyncMock(), _callback(ANNA, "export:new"), {})
    CallbackQuery.answer.assert_awaited_once_with(NO_ACCESS_TEXT, show_alert=True)  # type: ignore[attr-defined]

    await guard(AsyncMock(), _message(ANNA, "/rescore"), {})
    await guard(AsyncMock(), _message(ANNA, "/team"), {})  # within the cooldown: no second reply
    Message.answer.assert_awaited_once_with(NO_ACCESS_TEXT)  # type: ignore[attr-defined]


@pytest.mark.parametrize("caption", ["/collect_on", "/Rescore@spb_lead_bot", "/team", "/provider_on 2gis"])
async def test_operator_photo_with_a_command_caption_is_refused(guard, caption: str) -> None:
    photo = Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=ANNA, type="private"),
        from_user=User(id=ANNA, is_bot=False, first_name="U"),
        photo=[{"file_id": "p", "file_unique_id": "q", "width": 1, "height": 1}],
        caption=caption,
    )
    handler = AsyncMock()
    assert await guard(handler, photo, {"event_from_user": photo.from_user}) is None
    handler.assert_not_awaited()
    # The owner may send the same.
    owner_photo = photo.model_copy(update={"from_user": User(id=OWNER, is_bot=False, first_name="U")})
    await guard(handler, owner_photo, {"event_from_user": owner_photo.from_user})
    handler.assert_awaited_once()


def test_command_names_are_parsed_robustly() -> None:
    assert command_name("/Rescore@spb_lead_bot 30") == "rescore"
    assert command_name("/collect_status") == "collect_status"
    assert command_name("rescore") is None
    assert command_name("/") is None
    assert command_name(None) is None
    mock = MagicMock(spec=Message)  # attributes of a mock are not text
    assert is_owner_only(mock) is False


# ---------- handlers ----------


@pytest.fixture
def handler_env(session_maker, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(issue, "async_session_maker", session_maker)
    monkeypatch.setattr(
        issue, "settings", Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=OWNER, LEADS_BATCH_SIZE=3)
    )
    return session_maker


def _button(user_id: int, data: str, username: str = "anna") -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = User(id=user_id, is_bot=False, first_name="U", username=username)
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    return callback


async def test_status_button_of_someone_elses_lead_shows_an_alert(handler_env) -> None:
    lead_id = await _issued_to(handler_env, ANNA)
    await issue.handle_lead_status(_button(ANNA, f"ls:{lead_id}:{CONTACT_CONTACTED}"))
    boris = _button(BORIS, f"ls:{lead_id}:{CONTACT_REPLIED}", username="boris")
    await issue.handle_lead_status(boris)
    alert = boris.answer.await_args
    assert alert.args[0].startswith("Лид в работе у @anna до ") and alert.kwargs["show_alert"] is True
    boris.message.edit_text.assert_not_awaited()


async def test_return_button_edits_the_card_and_an_old_card_gets_the_stale_alert(handler_env) -> None:
    lead_id = await _issued_to(handler_env, ANNA)
    await issue.handle_lead_status(_button(ANNA, f"ls:{lead_id}:{CONTACT_CONTACTED}"))

    boris = _button(BORIS, f"lr:{lead_id}", username="boris")
    await issue.handle_return_to_pool(boris)
    assert boris.answer.await_args.kwargs["show_alert"] is True

    anna = _button(ANNA, f"lr:{lead_id}")
    await issue.handle_return_to_pool(anna)
    anna.answer.assert_awaited_once_with("Лид возвращён в пул")
    assert "Возвращён в пул" in anna.message.edit_text.await_args.args[0]
    assert (await _get(handler_env, lead_id)).crm_status == STATUS_NEW

    stale = _button(ANNA, f"ls:{lead_id}:{CONTACT_CONTACTED}")
    await issue.handle_lead_status(stale)
    assert stale.answer.await_args.kwargs["show_alert"] is True
    for forged in ("lr:abc", "lr:", "lr:1:2"):
        bad = _button(ANNA, forged)
        await issue.handle_return_to_pool(bad)
        assert bad.answer.await_args.kwargs["show_alert"] is True


async def test_one_person_in_two_chats_gets_one_batch_at_a_time(handler_env) -> None:
    issue._issuing_to.clear()
    issue._issuing_users.clear()
    assert issue._reserve_chat(-1, ANNA)
    assert not issue._reserve_chat(-2, ANNA)  # the same person elsewhere
    assert issue._reserve_chat(-2, BORIS)
    issue._release_chat(-1, ANNA)
    issue._release_chat(-2, BORIS)
    assert issue._issuing_to == set() and issue._issuing_users == set()


async def test_stats_hide_the_export_from_operators(handler_env) -> None:
    for user_id, has_export in ((OWNER, True), (ANNA, False)):
        message = MagicMock(spec=Message)
        message.from_user = User(id=user_id, is_bot=False, first_name="U")
        message.answer = AsyncMock()
        await issue.handle_leads_stats(message)
        assert (message.answer.await_args.kwargs["reply_markup"] is not None) is has_export


# ---------- scheduler ----------


async def test_release_job_is_registered_and_runs_with_an_external_collector(session_maker) -> None:
    config = load_collector_config(
        Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1, COLLECT_ENABLED=False, COLLECT_PROVIDERS="")
    )
    runner = CollectorRunner(
        session_maker, ProviderSet(providers=[], disabled={}), config, AsyncMock(), external_mode=True, min_pool=0
    )
    runner.add_interval_job(AsyncMock(), RELEASE_INTERVAL_SECONDS, RELEASE_JOB_ID)
    await runner.start()
    try:
        job = runner._scheduler.get_job(RELEASE_JOB_ID)
        assert job is not None and isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval == timedelta(minutes=15)
        assert runner._scheduler.running
    finally:
        await runner.shutdown()
