"""Integration tests for lead pipeline and database repository."""

from pathlib import Path
from typing import Any
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.database.models import Base, Lead
from src.database.repo import get_leads, get_leads_stats, update_crm_status
from src.services.pipeline import import_csv_harvest, run_lead_harvest
from src.services.providers.base import RawPlace


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
    async def mock_send(
        self: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
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
    }

    # Verify database contents
    leads = await get_leads(session, limit=10)
    assert len(leads) == 3

    leads_by_name = {lead.name: lead for lead in leads}
    assert "Кофейня с рабочим сайтом" not in leads_by_name

    dentist = leads_by_name["Стоматология Улыбка СПб"]
    assert dentist.site_status == "dead_site"
    assert dentist.score == 100
    assert dentist.crm_status == "new"
    assert dentist.district == "Петроградский"

    detailing = leads_by_name["Детейлинг Гараж 78"]
    assert detailing.site_status == "social_only"
    assert detailing.score == 90
    assert detailing.crm_status == "new"

    furniture = leads_by_name["Студия мебели ЛофтНева"]
    assert furniture.site_status == "no_site"
    assert furniture.score == 80
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
async def test_run_lead_harvest_with_providers(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify run_lead_harvest aggregates OSM and 2GIS places."""
    fake_osm_place = RawPlace(
        external_id="osm_999",
        source="osm",
        name="Ремонт Авто",
        category="auto_repair",
        phone="+79998887766",
        high_margin=True,
    )
    fake_2gis_place = RawPlace(
        external_id="2gis_888",
        source="2gis",
        name="Косметолог",
        category="beauty",
        raw_website="https://vk.com/cosmo",
        high_margin=True,
    )

    async def mock_osm_search(self: Any, district: str, category: str) -> list[RawPlace]:
        return [fake_osm_place]

    async def mock_2gis_search(self: Any, district: str, category: str) -> list[RawPlace]:
        return [fake_2gis_place]

    from src.services.providers.osm import OSMOverpassProvider
    from src.services.providers.twogis import TwoGisProvider

    monkeypatch.setattr(OSMOverpassProvider, "search_places", mock_osm_search)
    monkeypatch.setattr(TwoGisProvider, "search_places", mock_2gis_search)

    res = await run_lead_harvest(
        session=session,
        district_name="Центральный",
        category_key="auto_repair",
        twogis_api_key="valid_token_123",
    )

    assert res["total_found"] == 2
    assert res["qualified"] == 2
    assert res["saved_new"] == 2
    assert res["duplicates_skipped"] == 0
