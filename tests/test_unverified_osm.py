"""Leads with an unchecked "no site" from OSM (or from an unknown source) are kept but never handed out."""

import pytest
from sqlalchemy import select

from src.bot.formatters import format_funnel_stats, format_niche_table
from src.database.models import CONTACT_CONTACTED, STATUS_ISSUED, STATUS_NEW, Lead
from src.database.repo import (
    _in_pool_clause,
    count_new_leads,
    count_unverified_osm_leads,
    get_pool_stats,
    issue_next_leads,
    set_contact_status,
)
from src.services.niche_priority import NicheStats
from src.services.site_status import is_unverified_no_site

MARK = "не проверены (OSM)"


def _lead(number: int, site_status: str = "no_site", site_source: str | None = None, **fields) -> Lead:
    values = {
        "external_id": f"x_{number}",
        "source": "2gis",
        "name": f"Организация {number}",
        "category": "dentist_clinic",
        "site_status": site_status,
        "site_source": site_source,
        "score": 50,
        "crm_status": STATUS_NEW,
    }
    values.update(fields)
    return Lead(**values)


async def _save(session_maker, *leads: Lead) -> None:
    async with session_maker() as session:
        session.add_all(leads)
        await session.commit()


async def _issued_names(session_maker) -> set[str]:
    async with session_maker() as session:
        return {lead.name for lead in await issue_next_leads(session, 50)}


async def test_no_site_from_osm_is_not_issued(session_maker) -> None:
    await _save(session_maker, _lead(1, site_source="osm"))
    assert await _issued_names(session_maker) == set()


async def test_no_site_without_a_source_is_not_issued(session_maker) -> None:
    await _save(session_maker, _lead(1, site_source=None))
    assert await _issued_names(session_maker) == set()


async def test_no_site_from_a_real_check_is_issued(session_maker) -> None:
    sources = ("2gis", "2gis_card", "google", "manual")
    await _save(session_maker, *(_lead(number, site_source=source) for number, source in enumerate(sources, 1)))
    assert len(await _issued_names(session_maker)) == 4


async def test_other_statuses_from_osm_or_without_source_are_issued(session_maker) -> None:
    await _save(
        session_maker,
        _lead(1, "dead_site", "osm"),
        _lead(2, "social_only", None),
        _lead(3, "unknown", None),
        _lead(4, "site_not_found", None),
        _lead(5, "site_uncertain", None),
    )
    assert len(await _issued_names(session_maker)) == 5


async def test_leads_already_in_work_are_untouched_and_not_counted(session_maker) -> None:
    await _save(
        session_maker,
        _lead(1, site_source="osm"),
        _lead(2, site_source=None),
        _lead(3, site_source="2gis"),
    )
    async with session_maker() as session:
        # Taken while they were still visible, as before this change.
        for lead in (
            await session.execute(select(Lead).where(Lead.name.in_(["Организация 1", "Организация 2"])))
        ).scalars():
            lead.crm_status = STATUS_ISSUED
        await session.commit()
        ids = (await session.execute(select(Lead.id).where(Lead.crm_status == STATUS_ISSUED))).scalars().all()
        await set_contact_status(session, ids[0], CONTACT_CONTACTED, 1, "@anna")

    assert await _issued_names(session_maker) == {"Организация 3"}
    async with session_maker() as session:
        rows = (await session.execute(select(Lead.crm_status, Lead.site_source).where(Lead.id.in_(ids)))).all()
        assert {tuple(row) for row in rows} == {("issued", None), ("issued", "osm")}
        assert await count_unverified_osm_leads(session) == 0


async def test_helper_table() -> None:
    cases = [
        ("no_site", "osm", True),
        ("no_site", None, True),
        ("no_site", "2gis", False),
        ("no_site", "2gis_card", False),
        ("no_site", "google", False),
        ("no_site", "manual", False),
        ("dead_site", "osm", False),
        ("social_only", None, False),
        ("unknown", None, False),
    ]
    for site_status, site_source, expected in cases:
        assert is_unverified_no_site(site_status, site_source) is expected


async def test_sql_clause_agrees_with_the_helper(session_maker) -> None:
    cases = [
        (status, source)
        for status in ("no_site", "dead_site", "social_only", "unknown")
        for source in (None, "osm", "2gis", "2gis_card", "google", "manual", "other")
    ]
    await _save(session_maker, *(_lead(number, status, source) for number, (status, source) in enumerate(cases)))
    async with session_maker() as session:
        in_pool = set((await session.execute(select(Lead.id).where(_in_pool_clause()))).scalars())
        leads = (await session.execute(select(Lead))).scalars().all()
    for lead in leads:
        assert (lead.id in in_pool) is (not is_unverified_no_site(lead.site_status, lead.site_source))


async def test_counters_count_only_new_non_chain_leads(session_maker) -> None:
    await _save(
        session_maker,
        _lead(1, site_source="osm"),
        _lead(2, site_source=None),
        _lead(3, site_source="osm", is_chain=True),
        _lead(4, site_source="osm", crm_status="in_work"),
        _lead(5, site_source="2gis"),
    )
    async with session_maker() as session:
        assert await count_unverified_osm_leads(session) == 2
        assert (await get_pool_stats(session)).unverified_osm == 2
        assert await count_new_leads(session) == 1


async def test_pool_stats_show_the_hidden_count_only_when_there_is_one(session_maker) -> None:
    await _save(session_maker, _lead(1, site_source="osm"), _lead(2, site_source="osm"))
    async with session_maker() as session:
        stats = await get_pool_stats(session)
    assert f"{MARK}: <b>2</b>" in format_funnel_stats({STATUS_NEW: 2}, {}, 0, stats)

    async with session_maker() as session:
        await session.execute(Lead.__table__.delete())
        await session.commit()
        empty = await get_pool_stats(session)
    assert MARK not in format_funnel_stats({}, {}, 0, empty)


def test_niche_table_shows_the_hidden_count_only_when_there_is_one() -> None:
    stats = [NicheStats(category="dentist_clinic", no_site=3)]
    shown = "\n".join(format_niche_table(stats, unverified_osm=7))
    assert f"{MARK}: 7 (в выдачу не попадают)" in shown
    assert MARK not in "\n".join(format_niche_table(stats))
    assert MARK not in "\n".join(format_niche_table(stats, unverified_osm=0))


@pytest.mark.parametrize("source", ["osm", None])
async def test_manual_verdict_brings_the_lead_back(session_maker, source) -> None:
    await _save(session_maker, _lead(1, site_source=source))
    assert await _issued_names(session_maker) == set()
    async with session_maker() as session:
        lead = (await session.execute(select(Lead))).scalar_one()
        lead.site_source = "manual"
        await session.commit()
    assert await _issued_names(session_maker) == {"Организация 1"}
