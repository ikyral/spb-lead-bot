"""Tests for database schema initialization and query plans."""

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.database.models import Lead
from src.database.repo import get_leads, init_db, save_new_leads, update_crm_status, upsert_lead

LEAD_LIST_INDEX = "ix_leads_crm_status_score"

# Schema as created by earlier versions of the bot (before the index existed).
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
    VALUES ('osm_1', 'osm', 'Старый лид', 'dentist_clinic', 'no_site', 80, 'in_work')
    """,
]


@pytest.fixture
async def engine() -> AsyncEngine:
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    yield eng
    await eng.dispose()


async def _index_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("PRAGMA index_list(leads)"))
        return {row[1] for row in rows}


@pytest.mark.asyncio
async def test_init_db_creates_lead_list_index(engine: AsyncEngine) -> None:
    await init_db(engine)
    assert LEAD_LIST_INDEX in await _index_names(engine)


@pytest.mark.asyncio
async def test_init_db_adds_missing_index_to_existing_database(engine: AsyncEngine) -> None:
    """create_all skips indexes of existing tables, so databases from older versions must be upgraded too."""
    async with engine.begin() as conn:
        for statement in OLD_SCHEMA:
            await conn.execute(text(statement))
    assert LEAD_LIST_INDEX not in await _index_names(engine)

    await init_db(engine)
    await init_db(engine)  # idempotent: a restart must not fail

    assert LEAD_LIST_INDEX in await _index_names(engine)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT name, crm_status FROM leads"))).all()
    assert rows == [("Старый лид", "in_work")]


@pytest.mark.asyncio
async def test_lead_card_query_uses_index_without_sorting(engine: AsyncEngine) -> None:
    """Every card page runs this query: it must not scan the table and sort it in a temp B-tree."""
    await init_db(engine)
    captured: list[tuple[str, tuple]] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "FROM leads" in statement:
            captured.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with async_sessionmaker(engine)() as session:
            await get_leads(session, crm_status="new", limit=1, offset=3)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    statement, parameters = captured[-1]
    async with engine.connect() as conn:
        plan = [row[3] for row in await conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)]

    assert any(f"USING INDEX {LEAD_LIST_INDEX}" in step for step in plan), plan
    assert not any("TEMP B-TREE" in step for step in plan), plan
    assert not any(step.startswith("SCAN leads") for step in plan), plan


def test_index_is_declared_on_model() -> None:
    names = {index.name for index in Lead.__table__.indexes}
    assert LEAD_LIST_INDEX in names


def _lead_data(i: int, **overrides) -> dict:
    data = {
        "external_id": f"osm_{i}", "source": "osm", "name": f"Lead {i}", "category": "horeca",
        "site_status": "no_site", "score": 60, "crm_status": "new",
    }
    data.update(overrides)
    return data


@pytest.fixture
async def session(engine: AsyncEngine):
    await init_db(engine)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


async def _count_leads(session) -> int:
    return (await session.execute(text("SELECT count(*) FROM leads"))).scalar_one()


@pytest.mark.asyncio
async def test_upsert_lead_does_not_hide_non_duplicate_integrity_errors(session) -> None:
    """A NOT NULL violation is a bug, not a duplicate: it must not be counted as 'duplicates_skipped'."""
    with pytest.raises(IntegrityError):
        await upsert_lead(session, _lead_data(1, name=None))
    assert await _count_leads(session) == 0


@pytest.mark.asyncio
async def test_upsert_lead_concurrent_duplicate_is_still_a_duplicate(session, monkeypatch) -> None:
    """If another harvest inserts the same external_id between the check and the commit, it is a duplicate."""
    from src.database import repo

    await upsert_lead(session, _lead_data(1))
    real_exists = repo._external_id_exists
    calls = 0

    async def stale_first_check(sess, external_id):
        nonlocal calls
        calls += 1
        return False if calls == 1 else await real_exists(sess, external_id)

    monkeypatch.setattr(repo, "_external_id_exists", stale_first_check)
    assert await upsert_lead(session, _lead_data(1, name="Другое имя")) is False
    assert await _count_leads(session) == 1


@pytest.mark.asyncio
async def test_save_new_leads_uses_one_commit_and_skips_existing(session) -> None:
    await upsert_lead(session, _lead_data(0))
    commits = 0

    def count_commit(sess):
        nonlocal commits
        commits += 1

    event.listen(session.sync_session, "after_commit", count_commit)
    batch = [_lead_data(i) for i in range(50)] + [_lead_data(7, name="Повтор в пачке")]
    saved, duplicates = await save_new_leads(session, batch)
    event.remove(session.sync_session, "after_commit", count_commit)

    assert (saved, duplicates) == (49, 2)  # osm_0 already saved, osm_7 repeated in the batch
    assert commits == 1
    assert await _count_leads(session) == 50
    names = dict((await session.execute(text("SELECT external_id, name FROM leads"))).all())
    assert names["osm_0"] == "Lead 0" and names["osm_7"] == "Lead 7"  # existing data never overwritten


@pytest.mark.asyncio
async def test_save_new_leads_falls_back_when_a_concurrent_insert_wins(session, monkeypatch) -> None:
    """A stale existence check makes the batch commit fail; leads are then saved one by one."""
    from src.database import repo

    await upsert_lead(session, _lead_data(3))

    async def stale_existing(sess, external_ids):
        return set()

    monkeypatch.setattr(repo, "_existing_external_ids", stale_existing)
    saved, duplicates = await save_new_leads(session, [_lead_data(i) for i in range(5)])

    assert (saved, duplicates) == (4, 1)
    assert await _count_leads(session) == 5


@pytest.mark.asyncio
async def test_save_new_leads_raises_on_invalid_lead(session) -> None:
    with pytest.raises(IntegrityError):
        await save_new_leads(session, [_lead_data(1), _lead_data(2, name=None)])


@pytest.mark.asyncio
async def test_save_new_leads_handles_large_batches(session) -> None:
    """The existence check must not exceed SQLite's bound-parameter limit on big CSV imports."""
    saved, duplicates = await save_new_leads(session, [_lead_data(i) for i in range(2500)])
    assert (saved, duplicates) == (2500, 0)
    assert await save_new_leads(session, [_lead_data(i) for i in range(2500)]) == (0, 2500)


@pytest.mark.asyncio
async def test_update_crm_status_reports_whether_a_lead_was_updated(session) -> None:
    await upsert_lead(session, _lead_data(1))
    lead_id = (await session.execute(text("SELECT id FROM leads"))).scalar_one()

    assert await update_crm_status(session, lead_id, "in_work") is True
    assert await update_crm_status(session, 999_999, "in_work") is False


@pytest.mark.asyncio
async def test_created_at_is_read_back_as_aware_utc(engine: AsyncEngine, session) -> None:
    """SQLite drops tzinfo: values came back naive, and non-UTC input was stored without conversion."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    msk = timezone(timedelta(hours=3))
    session.add(Lead(**_lead_data(1), created_at=datetime(2026, 11, 4, 12, 0, tzinfo=msk)))
    session.add(Lead(**_lead_data(2)))  # Python default: now(UTC)
    await session.commit()
    async with engine.begin() as conn:  # server default: CURRENT_TIMESTAMP (UTC)
        await conn.execute(text(
            "INSERT INTO leads (external_id, source, name, category, site_status, score, crm_status) "
            "VALUES ('osm_3', 'osm', 'Raw', 'horeca', 'no_site', 60, 'new')"
        ))

    async with async_sessionmaker(engine)() as fresh:
        rows = dict((await fresh.execute(select(Lead.external_id, Lead.created_at))).all())

    assert rows["osm_1"] == datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
    for value in rows.values():
        assert value.utcoffset() == timedelta(0)
    now = datetime.now(timezone.utc)
    assert abs(now - rows["osm_2"]) < timedelta(minutes=5)
    assert abs(now - rows["osm_3"]) < timedelta(minutes=5)
