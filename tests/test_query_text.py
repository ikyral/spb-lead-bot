"""leads.query_text: the search text that found a lead is kept for new leads, old rows stay NULL."""

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database.models import Base, Lead
from src.database.repo import NEW_LEAD_COLUMNS, init_db
from src.services import dedup, pipeline
from src.services.providers.base import RawPlace


async def test_an_old_table_gets_the_column_and_old_rows_keep_null(tmp_path: Any) -> None:
    assert "query_text" in NEW_LEAD_COLUMNS
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE leads DROP COLUMN query_text"))
        await conn.execute(
            text(
                "INSERT INTO leads (external_id, source, name, category, site_status, score, crm_status) "
                "VALUES ('2gis_1', '2gis', 'Старый', 'street_food', 'no_site', 50, 'new')"
            )
        )
    try:
        await init_db(engine)
        await init_db(engine)  # a restart must not fail
        async with engine.connect() as conn:
            columns = {row[1] for row in await conn.execute(text("PRAGMA table_info(leads)"))}
            rows = list(await conn.execute(text("SELECT name, query_text FROM leads")))
        assert "query_text" in columns
        assert rows == [("Старый", None)]
    finally:
        await engine.dispose()


def _place(number: int, query_text: str | None) -> RawPlace:
    return RawPlace(
        external_id=f"2gis_{number}",
        source="2gis",
        name=f"Шаурма {number}",
        category="street_food",
        district="Выборгский",
        phone=f"+7 812 555-02-{number:02d}",
        high_margin=True,
        query_text=query_text,
    )


async def test_the_pipeline_stores_the_query_text_of_a_place(
    session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def verdict(url: str | None, client: Any = None) -> tuple[str, None]:
        return "no_site", None

    monkeypatch.setattr(pipeline, "analyze_website", verdict)
    search = "шаурма Выборгский район Санкт-Петербург"
    async with session_maker() as session:
        await pipeline._process_and_save_places(session, [_place(1, search), _place(2, None)])
    async with session_maker() as session:
        leads = {lead.external_id: lead for lead in (await session.scalars(select(Lead))).all()}
    assert leads["2gis_1"].query_text == search
    assert leads["2gis_1"].category == "street_food" and leads["2gis_1"].district == "Выборгский"
    assert leads["2gis_2"].query_text is None


def test_a_duplicate_fills_a_missing_query_text_and_keeps_an_existing_one() -> None:
    kept = _place(1, None)
    duplicate = _place(1, "шаурма Выборгский район Санкт-Петербург")
    dedup._fill_from_duplicate(kept, duplicate)
    assert kept.query_text == "шаурма Выборгский район Санкт-Петербург"
    other = _place(1, "кебаб Выборгский район Санкт-Петербург")
    dedup._fill_from_duplicate(kept, other)
    assert kept.query_text == "шаурма Выборгский район Санкт-Петербург"
