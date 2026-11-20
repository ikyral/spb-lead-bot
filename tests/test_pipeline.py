"""Integration tests for lead pipeline and database repository."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.database.models import Base, Lead
from src.database.repo import get_leads, get_leads_stats, update_crm_status
from src.services import pipeline
from src.services.pipeline import import_csv_harvest, run_lead_harvest
from src.services.providers.base import RawPlace
from src.services.providers.osm import OSMOverpassProvider


@pytest.fixture
async def session() -> AsyncSession:
    """Create an isolated in-memory SQLite database session."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    async with session_maker() as s:
        yield s

    await engine.dispose()


@pytest.mark.asyncio
async def test_csv_pipeline_full_integration(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Test real test_leads.csv processing:
    - 4 rows total.
    - 1 dentist with dead_site -> qualified (score 100).
    - 1 detailing with social_only -> qualified (score 90).
    - 1 furniture studio with no_site -> qualified (score 80).
    - 1 cafe with active site (ya.ru) -> dropped.
    - Repeated run detects all 3 as duplicates.
    """

    # Patch `send`: both `get` and `stream` go through it, so no real network calls are made.
    async def mock_send(self: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        if "broken-domain-dentist-spb-404.ru" in str(request.url):
            raise httpx.ConnectError("Connection failed: DNS resolution error", request=request)
        return httpx.Response(status_code=200, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

    csv_bytes = Path("data/test_leads.csv").read_bytes()

    # First run: 3 leads saved
    first_stats = await import_csv_harvest(session, csv_bytes)
    assert first_stats == {
        "total_found": 4,
        "qualified": 3,
        "saved_new": 3,
        "duplicates_skipped": 0,
        "over_limit": 0,
    }

    # Verify database contents
    # The CSV has no source of the verdict, so its "no site" lead is unverified and is not in the pool.
    pool = await get_leads(session, limit=10)
    assert len(pool) == 2
    leads = list((await session.execute(select(Lead))).scalars().all())
    assert len(leads) == 3

    leads_by_name = {lead.name: lead for lead in leads}
    assert "Кофейня с рабочим сайтом" not in leads_by_name

    dentist = leads_by_name["Стоматология Улыбка СПб"]
    assert dentist.site_status == "dead_site"
    assert dentist.score == 95  # 60 dead + 15 landline + 10 socials + 10 margin
    assert dentist.crm_status == "new"
    assert dentist.district == "Петроградский"

    detailing = leads_by_name["Детейлинг Гараж 78"]
    assert detailing.site_status == "social_only"
    assert detailing.score == 100  # 55 social only + 25 mobile + 10 socials + 10 margin
    assert detailing.crm_status == "new"

    furniture = leads_by_name["Студия мебели ЛофтНева"]
    assert furniture.site_status == "no_site"
    assert furniture.score == 85  # 50 no site + 15 landline + 10 socials + 10 margin
    assert furniture.crm_status == "new"

    # Verify CRM stats
    stats = await get_leads_stats(session)
    assert stats == {"new": 3, "in_work": 0, "rejected": 0}

    # Second run: 0 saved, 3 duplicates skipped
    second_stats = await import_csv_harvest(session, csv_bytes)
    assert second_stats == {
        "total_found": 4,
        "qualified": 3,
        "saved_new": 0,
        "duplicates_skipped": 3,
        "over_limit": 0,
    }


@pytest.mark.asyncio
async def test_get_leads_stats_and_status_transitions(session: AsyncSession) -> None:
    """Verify get_leads_stats accurately tracks status changes."""
    lead1 = Lead(
        external_id="lead_1",
        source="osm",
        name="Клиника 1",
        category="dentist",
        site_status="dead_site",
        score=85,
        crm_status="new",
    )
    lead2 = Lead(
        external_id="lead_2",
        source="osm",
        name="Клиника 2",
        category="dentist",
        site_status="social_only",
        score=75,
        crm_status="new",
    )
    session.add_all([lead1, lead2])
    await session.commit()

    initial_stats = await get_leads_stats(session)
    assert initial_stats == {"new": 2, "in_work": 0, "rejected": 0}

    await update_crm_status(session, lead1.id, "in_work")
    await update_crm_status(session, lead2.id, "rejected")

    updated_stats = await get_leads_stats(session)
    assert updated_stats == {"new": 0, "in_work": 1, "rejected": 1}


@pytest.mark.asyncio
async def test_run_lead_harvest_saves_the_places_of_osm(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manual search reads OSM; places already in the database are not saved twice."""
    fake_osm_place = RawPlace(
        external_id="osm_999",
        source="osm",
        name="Ремонт Авто",
        category="auto_repair",
        phone="+79998887766",
        high_margin=True,
    )

    async def mock_osm_search(self: Any, district: str, category: str) -> list[RawPlace]:
        return [fake_osm_place]

    monkeypatch.setattr(OSMOverpassProvider, "search_places", mock_osm_search)

    res = await run_lead_harvest(session=session, district_name="Центральный", category_key="auto_repair")
    assert (res["total_found"], res["qualified"], res["saved_new"], res["duplicates_skipped"]) == (1, 1, 1, 0)

    again = await run_lead_harvest(session=session, district_name="Центральный", category_key="auto_repair")
    assert (again["saved_new"], again["duplicates_skipped"]) == (0, 1)


async def test_run_lead_harvest_survives_a_failing_osm(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(self: Any, district: str, category: str) -> list[RawPlace]:
        raise RuntimeError("overpass")

    monkeypatch.setattr(OSMOverpassProvider, "search_places", broken)
    res = await run_lead_harvest(session, "Центральный", "auto_repair")
    assert (res["total_found"], res["saved_new"]) == (0, 0)


@pytest.mark.asyncio
async def test_import_skips_business_already_saved_from_another_source(
    session: AsyncSession,
) -> None:
    """A CSV row for a business already saved from OSM (same phone) is a duplicate, not a new lead."""
    session.add(
        Lead(
            external_id="osm_900",
            source="osm",
            name="Детейлинг Гараж",
            category="auto_repair",
            phone="+7 999 111-22-33",
            site_status="no_site",
            site_source="2gis",
            score=65,
            crm_status="in_work",
        )
    )
    await session.commit()

    csv_bytes = ("Название;Рубрика;Телефон\nГараж 78 детейлинг-центр;Автосервис;8 (999) 111 22 33\n").encode()

    res = await import_csv_harvest(session, csv_bytes)
    assert res == {"total_found": 1, "qualified": 1, "saved_new": 0, "duplicates_skipped": 1, "over_limit": 0}
    assert await get_leads_stats(session) == {"new": 0, "in_work": 1, "rejected": 0}


@pytest.mark.asyncio
async def test_website_checks_run_with_higher_concurrency(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checks used to run 5 at a time; a district with hundreds of dead sites took many minutes."""
    running = 0
    max_running = 0

    async def slow_check(url: str | None, client: Any = None) -> tuple[str, str | None]:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.01)
        running -= 1
        return ("has_site", url)

    monkeypatch.setattr(pipeline, "analyze_website", slow_check)
    places = [
        RawPlace(
            external_id=f"csv_{i}",
            source="csv",
            name=f"Место {i}",
            category="horeca",
            raw_website=f"https://site{i}.ru",
        )
        for i in range(60)
    ]
    await pipeline._process_and_save_places(session, places)
    assert max_running == pipeline.CHECK_CONCURRENCY == 20


@pytest.mark.asyncio
async def test_progress_callback_reports_checked_sites_and_errors_do_not_break_harvest(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def instant_check(url: str | None, client: Any = None) -> tuple[str, str | None]:
        return ("no_site", None)

    monkeypatch.setattr(pipeline, "analyze_website", instant_check)
    places = [
        RawPlace(
            external_id=f"csv_{i}",
            source="csv",
            name=f"Место {i}",
            category="dentist_clinic",
            phone=f"+7 999 000-00-{i:02d}",
        )
        for i in range(10)
    ]
    reports: list[tuple[int, int]] = []

    async def progress(done: int, total: int) -> None:
        reports.append((done, total))
        if done == 5:
            raise RuntimeError("Telegram is down")

    stats = await pipeline._process_and_save_places(session, places, progress=progress)

    assert reports == [(done, 10) for done in range(1, 11)]
    assert stats["saved_new"] == 10


@pytest.mark.asyncio
async def test_csv_import_is_capped(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A huge CSV must not turn into hours of website checks; the rest is reported, not silently dropped."""
    monkeypatch.setattr(pipeline, "MAX_CSV_PLACES", 2)
    csv_bytes = (
        "Название;Рубрика;Телефон\n"
        "Клиника 1;Стоматология;+7 999 000-00-01\n"
        "Клиника 2;Стоматология;+7 999 000-00-02\n"
        "Клиника 3;Стоматология;+7 999 000-00-03\n"
    ).encode()

    res = await import_csv_harvest(session, csv_bytes)
    assert res == {"total_found": 2, "qualified": 2, "saved_new": 2, "duplicates_skipped": 0, "over_limit": 1}


@pytest.mark.asyncio
async def test_harvest_saves_all_leads_in_one_commit(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A commit per lead took ~1.9 s per 500 leads on SQLite; the whole batch must be one transaction."""
    from sqlalchemy import event

    async def instant_check(url: str | None, client: Any = None) -> tuple[str, str | None]:
        return ("no_site", None)

    monkeypatch.setattr(pipeline, "analyze_website", instant_check)
    places = [
        RawPlace(
            external_id=f"osm_{i}",
            source="osm",
            name=f"Lead {i}",
            category="dentist_clinic",
            phone=f"+7 999 100-{i:02d}-00",
        )
        for i in range(30)
    ]
    commits = 0

    def count_commit(sess: Any) -> None:
        nonlocal commits
        commits += 1

    event.listen(session.sync_session, "after_commit", count_commit)
    try:
        stats = await pipeline._process_and_save_places(session, places)
    finally:
        event.remove(session.sync_session, "after_commit", count_commit)

    assert stats["saved_new"] == 30
    assert commits == 1
