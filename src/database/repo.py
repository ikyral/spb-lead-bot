from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Connection, CursorResult, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import settings
from src.database.models import STATUS_ISSUED, STATUS_NEW, Base, CollectorState, Lead

engine: AsyncEngine = create_async_engine(settings.DB_URL, echo=False)
async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def init_db(target_engine: AsyncEngine | None = None) -> None:
    """Initialize database tables and indexes (idempotent, safe on existing databases)."""
    current_engine = target_engine or engine
    async with current_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_create_missing_indexes)


# Nullable columns added to `leads` after the first release. Old rows need no backfill.
NEW_LEAD_COLUMNS: tuple[str, ...] = ("email", "source_url", "issued_at")


def _add_missing_columns(sync_conn: Connection) -> None:
    """create_all() does not alter existing tables, so older databases get new columns here."""
    existing = {column["name"] for column in inspect(sync_conn).get_columns("leads")}
    for name in NEW_LEAD_COLUMNS:
        if name not in existing:
            ddl_type = Lead.__table__.c[name].type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(text(f"ALTER TABLE leads ADD COLUMN {name} {ddl_type}"))


def _create_missing_indexes(sync_conn: Connection) -> None:
    """
    Create indexes added to models after the table already existed.

    create_all() skips existing tables together with their indexes, so databases
    created by older versions would never get new indexes otherwise.
    """
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(sync_conn, checkfirst=True)


async def upsert_lead(
    session: AsyncSession,
    lead_data: dict[str, Any] | Lead,
) -> bool:
    """
    Insert a lead if it does not already exist by external_id.

    Existing leads are NOT overwritten to preserve user CRM status and edits.
    Returns True if the lead is newly inserted, False if it already exists.
    """
    external_id: str | None
    if isinstance(lead_data, Lead):
        external_id = lead_data.external_id
    elif isinstance(lead_data, dict):
        external_id = lead_data.get("external_id")
    else:
        raise ValueError(f"Unsupported lead_data type: {type(lead_data)}")

    if not external_id:
        raise ValueError("Lead data must contain a non-empty external_id")

    if await _external_id_exists(session, external_id):
        return False

    lead = lead_data if isinstance(lead_data, Lead) else Lead(**lead_data)

    try:
        session.add(lead)
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        # Only a concurrent insert of the same external_id is a duplicate;
        # any other constraint violation (e.g. NOT NULL) is a bug and must surface.
        if await _external_id_exists(session, external_id):
            return False
        raise


async def _external_id_exists(session: AsyncSession, external_id: str) -> bool:
    result = await session.execute(select(Lead.id).where(Lead.external_id == external_id))
    return result.scalar_one_or_none() is not None


# Keeps IN (...) lists well below SQLite's bound-parameter limit (999 on old builds).
_EXISTENCE_CHECK_CHUNK = 500


async def _existing_external_ids(session: AsyncSession, external_ids: list[str]) -> set[str]:
    existing: set[str] = set()
    for start in range(0, len(external_ids), _EXISTENCE_CHECK_CHUNK):
        chunk = external_ids[start:start + _EXISTENCE_CHECK_CHUNK]
        result = await session.execute(select(Lead.external_id).where(Lead.external_id.in_(chunk)))
        existing.update(result.scalars())
    return existing


async def find_existing_external_ids(session: AsyncSession, external_ids: list[str]) -> set[str]:
    """Return the external IDs from the list that are already saved."""
    return await _existing_external_ids(session, external_ids)


async def save_new_leads(
    session: AsyncSession,
    leads_data: list[dict[str, Any]],
) -> tuple[int, int]:
    """
    Insert leads whose external_id is not saved yet, in a single transaction.

    Existing leads are never overwritten. Returns (saved, duplicates). If a concurrent
    harvest inserts some of the same leads in the meantime, the batch is rolled back and
    the leads are saved one by one with upsert_lead. Non-duplicate errors are raised.
    """
    if not leads_data:
        return 0, 0

    existing = await _existing_external_ids(session, [data["external_id"] for data in leads_data])
    seen = set(existing)
    new_leads: list[dict[str, Any]] = []
    for data in leads_data:
        if data["external_id"] not in seen:
            seen.add(data["external_id"])
            new_leads.append(data)

    try:
        session.add_all([Lead(**data) for data in new_leads])
        await session.commit()
        saved = len(new_leads)
    except IntegrityError:
        await session.rollback()
        saved = 0
        for data in new_leads:
            if await upsert_lead(session, data):
                saved += 1

    return saved, len(leads_data) - saved


async def get_lead_fingerprints(
    session: AsyncSession,
) -> list[tuple[str, str | None, str | None, str | None]]:
    """Return (name, phone, map_url, address) of all leads, used to detect cross-source duplicates."""
    result = await session.execute(select(Lead.name, Lead.phone, Lead.map_url, Lead.address))
    return [(name, phone, map_url, address) for name, phone, map_url, address in result.all()]


async def get_leads(
    session: AsyncSession,
    crm_status: str = "new",
    min_score: int = 30,
    limit: int = 1,
    offset: int = 0,
) -> list[Lead]:
    """Retrieve leads filtered by CRM status and minimum score with pagination."""
    stmt = (
        select(Lead)
        .where(Lead.crm_status == crm_status, Lead.score >= min_score)
        .order_by(Lead.score.desc(), Lead.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def issue_next_leads(session: AsyncSession, limit: int) -> list[Lead]:
    """
    Hand out the oldest `limit` new leads: they become 'issued' in a single atomic statement.

    Concurrent calls can never return the same lead. The repeated status check in the outer
    WHERE matters on databases where the subquery can see a stale snapshot.
    """
    oldest_new = (
        select(Lead.id)
        .where(Lead.crm_status == STATUS_NEW)
        .order_by(Lead.created_at, Lead.id)
        .limit(limit)
    )
    stmt = (
        update(Lead)
        .where(Lead.id.in_(oldest_new), Lead.crm_status == STATUS_NEW)
        .values(crm_status=STATUS_ISSUED, issued_at=datetime.now(UTC))
        .returning(Lead)
        .execution_options(synchronize_session=False)
    )
    leads = list((await session.scalars(stmt)).all())
    await session.commit()
    # RETURNING has no guaranteed order.
    return sorted(leads, key=lambda lead: (lead.created_at, lead.id))


async def return_undelivered_leads(session: AsyncSession, lead_ids: list[int]) -> None:
    """Put issued leads that never reached the admin back to the pool, keeping their position."""
    if not lead_ids:
        return
    await session.execute(
        update(Lead)
        .where(Lead.id.in_(lead_ids), Lead.crm_status == STATUS_ISSUED)
        .values(crm_status=STATUS_NEW, issued_at=None)
    )
    await session.commit()


async def count_new_leads(session: AsyncSession) -> int:
    """Number of leads not handed out yet."""
    result = await session.execute(select(func.count(Lead.id)).where(Lead.crm_status == STATUS_NEW))
    return int(result.scalar_one())


async def get_status_counts(session: AsyncSession) -> dict[str, int]:
    """Lead counts for every status present in the database."""
    result = await session.execute(select(Lead.crm_status, func.count(Lead.id)).group_by(Lead.crm_status))
    return {status: int(count) for status, count in result.all()}


async def count_issued_leads(session: AsyncSession) -> int:
    """Number of leads ever handed out, whatever their status is now."""
    result = await session.execute(select(func.count(Lead.id)).where(Lead.issued_at.is_not(None)))
    return int(result.scalar_one())


async def get_collector_state(session: AsyncSession) -> CollectorState:
    """Return the collector state row, creating it on first use (the caller commits)."""
    state = await session.get(CollectorState, 1)
    if state is None:
        state = CollectorState(id=1)
        session.add(state)
        await session.flush()
    return state


async def get_leads_stats(session: AsyncSession) -> dict[str, int]:
    """Return lead counts grouped by CRM status ('new', 'in_work', 'rejected')."""
    stmt = select(Lead.crm_status, func.count(Lead.id)).group_by(Lead.crm_status)
    result = await session.execute(stmt)
    counts = dict(result.all())
    return {
        "new": counts.get("new", 0),
        "in_work": counts.get("in_work", 0),
        "rejected": counts.get("rejected", 0),
    }


async def update_crm_status(
    session: AsyncSession,
    lead_id: int,
    new_status: str,
) -> bool:
    """Update CRM status of a lead by ID ('new', 'in_work', 'rejected')."""
    stmt = (
        update(Lead)
        .where(Lead.id == lead_id)
        .values(crm_status=new_status)
    )
    # UPDATE returns a CursorResult at runtime; Session.execute is typed as the generic Result.
    result = cast("CursorResult[Any]", await session.execute(stmt))
    await session.commit()
    return result.rowcount > 0


async def export_leads_by_status(
    session: AsyncSession,
    crm_status: str,
) -> list[Lead]:
    """Export all leads for a given CRM status (or 'all' for all leads)."""
    stmt = select(Lead).order_by(Lead.score.desc(), Lead.id.asc())
    if crm_status.lower() != "all":
        stmt = stmt.where(Lead.crm_status == crm_status)
    result = await session.execute(stmt)
    return list(result.scalars().all())
