from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import settings
from src.database.models import Base, Lead

engine: AsyncEngine = create_async_engine(settings.DB_URL, echo=False)
async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def init_db(target_engine: AsyncEngine | None = None) -> None:
    """Initialize database tables."""
    current_engine = target_engine or engine
    async with current_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def upsert_lead(
    session: AsyncSession,
    lead_data: dict[str, Any] | Lead,
) -> bool:
    """
    Insert a lead if it does not already exist by external_id.

    Existing leads are NOT overwritten to preserve user CRM status and edits.
    Returns True if the lead is newly inserted, False if it already exists.
    """
    if isinstance(lead_data, Lead):
        external_id = lead_data.external_id
    elif isinstance(lead_data, dict):
        external_id = lead_data.get("external_id")
    else:
        raise ValueError(f"Unsupported lead_data type: {type(lead_data)}")

    if not external_id:
        raise ValueError("Lead data must contain a non-empty external_id")

    stmt = select(Lead.id).where(Lead.external_id == external_id)
    result = await session.execute(stmt)
    if result.scalar_one_or_none() is not None:
        return False

    if isinstance(lead_data, Lead):
        lead = lead_data
    else:
        lead = Lead(**lead_data)

    try:
        session.add(lead)
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def get_lead_fingerprints(
    session: AsyncSession,
) -> list[tuple[str, str | None, str | None]]:
    """Return (name, phone, map_url) of all leads, used to detect cross-source duplicates."""
    result = await session.execute(select(Lead.name, Lead.phone, Lead.map_url))
    return [(name, phone, map_url) for name, phone, map_url in result.all()]


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
    result = await session.execute(stmt)
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
