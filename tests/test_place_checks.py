"""place_checks: every organization whose site 2gis_cdp checked, for shares per niche and for not clicking it again."""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scripts import niche_stats
from src.config import Settings
from src.database.models import Base, Lead, PlaceCheck
from src.database.repo import PlaceCheckData, get_recently_checked_firm_ids, init_db, save_place_checks
from src.services import pipeline
from src.services.collector import collect_leads, load_collector_config
from src.services.providers.base import BaseMapProvider, RawPlace, SearchArea, SearchPage, SearchQuery
from src.services.providers.twogis_cdp import CdpOptions, TwoGisCdpProvider
from src.services.site_status import SITE_DEAD, SITE_HAS_SITE, SITE_NO_SITE, USEFUL_STATUSES
from src.services.twogis_links import firm_id_of

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
SEARCH = "шиномонтаж Выборгский район"


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, BOT_TOKEN="t", ADMIN_TG_ID=1, **overrides)


def _check(firm_id: str, status: str = SITE_NO_SITE, category: str = "tire_service") -> PlaceCheckData:
    return PlaceCheckData(
        firm_id=firm_id, category=category, district="Выборгский", query_text=SEARCH, site_status=status
    )


def _place(number: int, website: str | None = None, source: str = "2gis", phone: str | None = None) -> RawPlace:
    return RawPlace(
        external_id=f"{source}_{number}_hash",
        source=source,
        name=f"Организация {number}",
        category="tire_service",
        district="Выборгский",
        address=f"ул. Пример, {number}",
        phone=phone or f"+7 812 555-01-{number:02d}",
        raw_website=website,
        email=f"org{number}@example.com",
        socials=f"https://vk.com/org{number}",
        high_margin=True,
        query_text=SEARCH,
    )


@pytest.fixture
def site_works_for_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """A listed https site works, an http one is dead, no address means no site: no network."""

    async def verdict(url: str | None, client: Any = None) -> tuple[str, None]:
        if not url:
            return SITE_NO_SITE, None
        return (SITE_HAS_SITE if url.startswith("https://") else SITE_DEAD), None

    monkeypatch.setattr(pipeline, "analyze_website", verdict)


async def _checks(session_maker: async_sessionmaker[AsyncSession]) -> dict[str, PlaceCheck]:
    async with session_maker() as session:
        return {row.firm_id: row for row in (await session.scalars(select(PlaceCheck))).all()}


# ---------- the table ----------


def test_firm_id_is_the_number_without_the_branch_suffix() -> None:
    assert firm_id_of("2gis_70000001_abc") == "70000001"
    assert firm_id_of("2gis_70000001") == "70000001"
    assert firm_id_of("osm_node_1") == ""
    assert firm_id_of("2gis_") == ""


async def test_an_old_database_gets_the_table_and_keeps_its_leads(tmp_path: Any) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    old_tables = [table for table in Base.metadata.sorted_tables if table.name != "place_checks"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=old_tables))
        await conn.exec_driver_sql(
            "INSERT INTO leads (external_id, source, name, category, site_status, score, crm_status) "
            "VALUES ('2gis_1', '2gis', 'Шины', 'tire_service', 'no_site', 50, 'new')"
        )
    try:
        await init_db(engine)
        await init_db(engine)  # idempotent
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
            columns = await conn.run_sync(lambda sync: {c["name"] for c in inspect(sync).get_columns("place_checks")})
            leads = (await conn.exec_driver_sql("SELECT external_id FROM leads")).all()
        assert "place_checks" in tables
        assert columns == {"firm_id", "category", "district", "query_text", "site_status", "checked_at"}
        assert leads == [("2gis_1",)]
    finally:
        await engine.dispose()


async def test_saving_again_updates_the_row_instead_of_adding_one(session_maker: Any) -> None:
    async with session_maker() as session:
        assert await save_place_checks(session, [_check("1"), _check("2", SITE_HAS_SITE)], now=NOW) == (2, 0)
    later = NOW + timedelta(days=40)
    async with session_maker() as session:
        # A firm twice in one batch: the last one wins, there is still one row.
        result = await save_place_checks(session, [_check("1", SITE_HAS_SITE), _check("1", SITE_DEAD)], now=later)
        assert result == (0, 1)
    rows = await _checks(session_maker)
    assert set(rows) == {"1", "2"}
    assert rows["1"].site_status == SITE_DEAD
    assert rows["1"].checked_at == later
    assert rows["2"].checked_at == NOW
    async with session_maker() as session:
        assert await save_place_checks(session, []) == (0, 0)


async def test_firms_checked_within_the_days_are_known_older_ones_are_checked_again(session_maker: Any) -> None:
    async with session_maker() as session:
        await save_place_checks(session, [_check("recent")], now=NOW - timedelta(days=29))
        await save_place_checks(session, [_check("old")], now=NOW - timedelta(days=31))
    async with session_maker() as session:
        assert await get_recently_checked_firm_ids(session, 30, now=NOW) == {"recent"}
        assert await get_recently_checked_firm_ids(session, 60, now=NOW) == {"recent", "old"}
        assert await get_recently_checked_firm_ids(session, 1, now=NOW) == set()


# ---------- the setting ----------


@pytest.mark.parametrize(("value", "expected"), [(0, 1), (-5, 1), (1, 1), (45, 45), (365, 365), (1000, 365)])
def test_site_recheck_days_is_clamped(value: int, expected: int, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="src.config"):
        days = _settings(SITE_RECHECK_DAYS=value).SITE_RECHECK_DAYS
    assert days == expected
    assert ("SITE_RECHECK_DAYS" in caplog.text) is (value != expected)


def test_site_recheck_days_defaults_to_30_and_reaches_the_collector() -> None:
    assert _settings().SITE_RECHECK_DAYS == 30
    assert load_collector_config(_settings(SITE_RECHECK_DAYS=7)).site_recheck_days == 7


# ---------- the pipeline ----------


async def test_every_checked_place_is_remembered_with_its_status_and_no_contacts(
    session_maker: Any, site_works_for_https: None
) -> None:
    places = [_place(1, "https://works.example"), _place(2, None), _place(3, "http://dead.example")]
    async with session_maker() as session:
        stats = await pipeline.save_collected_places(
            session, places, record_checks_for={place.external_id for place in places}
        )
    assert stats["saved_new"] == 2  # the one with a working site is not a lead
    rows = await _checks(session_maker)
    assert {firm: row.site_status for firm, row in rows.items()} == {
        "1": SITE_HAS_SITE,
        "2": SITE_NO_SITE,
        "3": SITE_DEAD,
    }
    leads = {lead.external_id: lead.site_status for lead in await _leads(session_maker)}
    assert leads == {"2gis_2_hash": SITE_NO_SITE, "2gis_3_hash": SITE_DEAD}  # the same status as the lead
    row = rows["2"]
    assert (row.category, row.district, row.query_text) == ("tire_service", "Выборгский", SEARCH)
    # Only the numbers the statistics need: no name, address, phone, site, socials or email.
    assert set(PlaceCheck.__table__.columns.keys()) == {
        "firm_id",
        "category",
        "district",
        "query_text",
        "site_status",
        "checked_at",
    }


async def test_only_places_of_the_recording_provider_and_not_the_skipped_known_ones(
    session_maker: Any, site_works_for_https: None
) -> None:
    async with session_maker() as session:
        session.add(
            Lead(
                external_id="2gis_5_hash",
                source="2gis",
                name="Организация 5",
                category="tire_service",
                site_status=SITE_NO_SITE,
                score=50,
            )
        )
        await session.commit()
    cdp = [_place(4), _place(5)]  # 5 is already a lead: skipped before the check
    other = [_place(6), _place(7, source="osm")]
    async with session_maker() as session:
        await pipeline.save_collected_places(
            session, [*cdp, *other], record_checks_for={place.external_id for place in cdp}
        )
    assert set(await _checks(session_maker)) == {"4"}


async def test_a_known_lead_found_again_by_its_contacts_is_remembered_too(
    session_maker: Any, site_works_for_https: None
) -> None:
    async with session_maker() as session:
        await pipeline.save_collected_places(session, [_place(8)])
    # Another branch id, the same phone: a duplicate of the saved lead, still checked.
    again = _place(9, phone="+7 812 555-01-08")
    async with session_maker() as session:
        stats = await pipeline._process_and_save_places(session, [again], record_checks_for={again.external_id})
    assert stats["duplicates_skipped"] == 1
    assert (await _checks(session_maker))["9"].site_status == SITE_NO_SITE


async def test_a_failure_to_remember_checks_does_not_lose_the_leads(
    session_maker: Any, site_works_for_https: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(session: AsyncSession, checks: list[PlaceCheckData], now: datetime | None = None) -> None:
        raise OperationalError("INSERT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(pipeline, "save_place_checks", broken)
    place = _place(10)
    with caplog.at_level(logging.ERROR, logger="src.services.pipeline"):
        async with session_maker() as session:
            stats = await pipeline.save_collected_places(session, [place], record_checks_for={place.external_id})
    assert stats["saved_new"] == 1
    assert [lead.external_id for lead in await _leads(session_maker)] == ["2gis_10_hash"]
    assert "place_checks" in caplog.text


async def _leads(session_maker: Any) -> list[Lead]:
    async with session_maker() as session:
        return list((await session.scalars(select(Lead).order_by(Lead.id))).all())


# ---------- the collector ----------


class RecordingProvider(BaseMapProvider):
    """Like 2gis_cdp for the collector: remembers its checks, receives known and recently checked firms."""

    name = "2gis_cdp"
    external_id_prefix = "2gis_"
    records_place_checks = True

    def __init__(self, places: list[RawPlace]) -> None:
        self.places = places
        self.known: set[str] = set()
        self.calls: list[str] = []

    def set_known_external_ids(self, external_ids: set[str]) -> None:
        self.calls.append("known")
        self.known = {firm_id_of(eid) for eid in external_ids}

    def add_checked_firm_ids(self, firm_ids: set[str]) -> None:
        self.calls.append("checked")
        self.known |= firm_ids

    async def search_places(self, district_name: str, category_key: str) -> list[RawPlace]:
        return []

    async def search(self, query: Any, area: Any, page: int = 1, page_size: int = 50) -> SearchPage:
        return SearchPage(places=[place for place in self.places if firm_id_of(place.external_id) not in self.known])


class PlainProvider(RecordingProvider):
    name = "osm"
    external_id_prefix = None
    records_place_checks = False


def _one_target(**overrides: Any) -> Any:
    return load_collector_config(_settings(COLLECT_QUERIES="tire_service", COLLECT_DISTRICTS="Выборгский", **overrides))


async def test_the_collector_skips_saved_and_recently_checked_firms_and_records_new_checks(
    session_maker: Any, site_works_for_https: None
) -> None:
    now = datetime.now(UTC)
    async with session_maker() as session:
        session.add(
            Lead(
                external_id="2gis_20_hash",
                source="2gis",
                name="Организация 20",
                category="tire_service",
                site_status=SITE_NO_SITE,
                score=50,
            )
        )
        await session.commit()
        await save_place_checks(session, [_check("21", SITE_HAS_SITE)], now=now - timedelta(days=5))
        await save_place_checks(session, [_check("22", SITE_HAS_SITE)], now=now - timedelta(days=40))
    provider = RecordingProvider(
        [_place(20), _place(21, "https://a.example"), _place(22, "https://b.example"), _place(23)]
    )
    osm = PlainProvider([_place(30, source="osm")])

    await collect_leads(session_maker, [provider, osm], _one_target())

    assert provider.calls == ["known", "checked"]  # the checked ones are added after the saved leads, not instead
    assert provider.known >= {"20", "21"} and "22" not in provider.known
    assert osm.calls == []  # neither hint for a source that does not record its checks
    rows = await _checks(session_maker)
    assert set(rows) == {"21", "22", "23"}  # 22 was checked again, 23 is new; nothing of OSM
    assert rows["22"].checked_at > now - timedelta(minutes=5)
    assert rows["21"].checked_at < now - timedelta(days=4)  # not clicked, so not touched


# ---------- scripts/niche_stats.py --db ----------


def _make_db(path: Any, checks: list[tuple[str, str | None, str | None, str]] | None) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE leads (id INTEGER PRIMARY KEY, category TEXT, district TEXT, site_status TEXT, "
        "is_chain BOOLEAN, query_text TEXT)"
    )
    connection.execute(
        "INSERT INTO leads (category, district, site_status, is_chain, query_text) VALUES (?, ?, ?, 0, ?)",
        ("tire_service", "Выборгский", "no_site", SEARCH),
    )
    if checks is not None:
        connection.execute(
            "CREATE TABLE place_checks (firm_id TEXT PRIMARY KEY, category TEXT, district TEXT, query_text TEXT, "
            "site_status TEXT, checked_at DATETIME)"
        )
        for number, (category, district, query, status) in enumerate(checks):
            connection.execute(
                "INSERT INTO place_checks VALUES (?, ?, ?, ?, ?, '2026-11-20 10:00:00')",
                (str(number), category, district, query, status),
            )
    connection.commit()
    connection.close()


def test_useful_statuses_are_no_site_and_dead_site() -> None:
    assert {SITE_NO_SITE, SITE_DEAD} <= USEFUL_STATUSES
    assert SITE_HAS_SITE not in USEFUL_STATUSES
    assert niche_stats.useful_statuses() == USEFUL_STATUSES


def test_db_mode_prints_shares_per_niche_and_per_district_and_query(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "leads.db"
    _make_db(
        db,
        [
            ("tire_service", "Выборгский", SEARCH, "no_site"),
            ("tire_service", "Выборгский", SEARCH, "dead_site"),
            ("tire_service", "Выборгский", SEARCH, "has_site"),
            ("tire_service", "Выборгский", SEARCH, "has_site"),
            ("tire_service", "Центральный", "шиномонтаж Центральный район", "has_site"),
            ("nails", None, None, "no_site"),
        ],
    )
    before = db.read_bytes()
    stats = niche_stats.count_checks(niche_stats.read_check_rows(str(db)) or [], USEFUL_STATUSES)
    viborg = stats[("tire_service", "Выборгский", SEARCH)]
    assert (viborg.checked, viborg.useful, viborg.share) == (4, 2, 0.5)
    assert stats[("tire_service", "Центральный", "шиномонтаж Центральный район")].useful == 0

    assert niche_stats.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("Shares of useful organizations")
    nails = next(line for line in lines if line.startswith("nails"))
    tires = next(line for line in lines if line.startswith("tire_service ") and "%" in line)
    assert nails.split()[1:] == ["1", "1", "100%"]
    assert tires.split()[1:] == ["5", "2", "40%"]
    assert f"  Выборгский / {SEARCH}" in out and "50%" in out
    assert "  - / unknown query" in out
    assert "Saved leads from the database" in out  # the leads section follows
    assert db.read_bytes() == before  # read-only


@pytest.mark.parametrize("checks", [None, []], ids=["missing", "empty"])
def test_db_mode_without_checks_says_so_and_shows_the_leads(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], checks: list[Any] | None
) -> None:
    db = tmp_path / "old.db"
    _make_db(db, checks)
    assert niche_stats.read_check_rows(str(db)) == (None if checks is None else [])
    assert niche_stats.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(niche_stats.NO_CHECKS_NOTE)
    assert "data collected after this change" in out
    assert "Saved leads from the database" in out and "tire_service" in out


# ---------- the card city guard and the checks ----------


async def test_a_card_dropped_as_not_from_spb_is_neither_a_lead_nor_a_check(
    session_maker: Any, site_works_for_https: None
) -> None:
    """
    The guard drops the card before the pipeline: no lead and no place_checks row.

    A row would put a firm outside the city into the denominator of the shares of its niche. The cost is that such a
    firm is opened again on the next pass; the guard counts and logs every one of them.
    """

    def item(firm_id: str, lat: float, lon: float) -> dict[str, Any]:
        return {
            "id": firm_id,
            "name": f"Фирма {firm_id}",
            "type": "branch",
            "point": {"lat": lat, "lon": lon},
            "contact_groups": [{"contacts": [{"type": "phone", "value": f"+7 812 555-00-0{firm_id}"}]}],
        }

    provider = TwoGisCdpProvider(CdpOptions())
    payload = {"result": {"items": [item("1", 59.93, 30.33), item("2", 55.60, 37.36), item("3", 60.20, 29.69)]}}
    places = provider._places(
        [payload],
        SearchQuery(text="шиномонтаж", category="tire_service"),
        SearchArea(bbox=(59.9, 30.3, 60.0, 30.4), district="Выборгский район"),
    )
    assert provider.foreign_city_dropped == 1
    async with session_maker() as session:
        await pipeline.save_collected_places(session, places, record_checks_for={place.external_id for place in places})
    assert {lead.external_id for lead in await _leads(session_maker)} == {"2gis_1", "2gis_3"}  # Zelenogorsk is SPb
    assert set(await _checks(session_maker)) == {"1", "3"}  # the Moscow-region firm "2" is not remembered


# ---------- a site that did not answer is not remembered ----------


def _mock_check_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """The real checker over a stub transport: no network."""
    monkeypatch.setattr(
        pipeline, "create_check_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _answers(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "timeout.example":
        raise httpx.ConnectTimeout("timed out", request=request)
    if host == "refused.example":
        raise httpx.ConnectError("Connection refused", request=request)
    if host == "dns.example":
        raise httpx.ConnectError("Failed to resolve host", request=request)
    if host == "ssl.example":
        raise httpx.ConnectError("SSL: CERTIFICATE_VERIFY_FAILED", request=request)
    if host == "gone.example":
        return httpx.Response(404, request=request)
    if host == "broken.example":
        return httpx.Response(500, request=request)
    return httpx.Response(200, request=request)


@pytest.mark.parametrize("host", ["timeout", "refused", "dns", "ssl"])
async def test_a_site_that_did_not_answer_is_a_lead_but_not_remembered(
    session_maker: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, host: str
) -> None:
    _mock_check_client(monkeypatch, _answers)
    place = _place(1, f"https://{host}.example")
    with caplog.at_level(logging.INFO, logger="src.services.pipeline"):
        async with session_maker() as session:
            stats = await pipeline.save_collected_places(session, [place], record_checks_for={place.external_id})
    assert stats["saved_new"] == 1
    assert {lead.external_id: lead.site_status for lead in await _leads(session_maker)} == {
        place.external_id: SITE_DEAD
    }
    assert await _checks(session_maker) == {}
    assert "the site did not answer: 1" in caplog.text
    assert f"{host}.example" not in caplog.text


async def test_a_crashed_check_is_a_lead_but_not_remembered(
    session_maker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(url: str | None, client: Any = None) -> tuple[str, str | None]:
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "analyze_website", crash)
    place = _place(1, "https://crash.example")
    async with session_maker() as session:
        stats = await pipeline.save_collected_places(session, [place], record_checks_for={place.external_id})
    assert stats["saved_new"] == 1
    assert [lead.site_status for lead in await _leads(session_maker)] == [SITE_DEAD]
    assert await _checks(session_maker) == {}


async def test_real_http_errors_and_invalid_urls_stay_remembered_the_rest_is_unchanged(
    session_maker: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_check_client(monkeypatch, _answers)
    places = [
        _place(1, "https://gone.example"),
        _place(2, "https://broken.example"),
        _place(3, "https://::invalid::url"),
        _place(4, "https://works.example"),
        _place(5, None),
        _place(6, "https://timeout.example"),
    ]
    with caplog.at_level(logging.INFO, logger="src.services.pipeline"):
        async with session_maker() as session:
            await pipeline.save_collected_places(
                session, places, record_checks_for={place.external_id for place in places}
            )
    rows = await _checks(session_maker)
    assert {firm: row.site_status for firm, row in rows.items()} == {
        "1": SITE_DEAD,
        "2": SITE_DEAD,
        "3": SITE_DEAD,
        "4": SITE_HAS_SITE,
        "5": SITE_NO_SITE,
    }
    leads = {lead.external_id: lead.site_status for lead in await _leads(session_maker)}
    assert leads == {
        "2gis_1_hash": SITE_DEAD,
        "2gis_2_hash": SITE_DEAD,
        "2gis_3_hash": SITE_DEAD,
        "2gis_5_hash": SITE_NO_SITE,
        "2gis_6_hash": SITE_DEAD,
    }
    assert "the site did not answer: 1" in caplog.text


def test_legacy_collect_recheck_days_in_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLLECT_RECHECK_DAYS", "30")
    settings = _settings()
    assert not hasattr(settings, "COLLECT_RECHECK_DAYS")
    assert settings.SITE_RECHECK_DAYS == 30
    assert not hasattr(load_collector_config(_settings(COLLECT_RECHECK_DAYS=30)), "recheck_days")
