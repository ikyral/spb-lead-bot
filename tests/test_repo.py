"""Tests for database schema initialization and query plans."""

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.database.models import Lead
from src.database.repo import get_leads, init_db

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
