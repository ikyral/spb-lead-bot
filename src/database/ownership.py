"""
Several people in one bot: whose a lead is, when it goes back to the pool, who reserved it for a request file.

A batch goes to the person who asked (issued_to_user_id). 'Написал' makes the lead theirs for LEAD_OWNERSHIP_DAYS
(owner_user_id, owned_until); 'Ответили' keeps it theirs with no end; 'Нет ответа' frees it and gives it back to
the pool a day later; without progress a lead goes back when its time ends. The person of a lead is its owner,
or whom it was issued to. Leads issued before this was kept belong to the owner of the bot (assign_existing_leads).
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.models import (
    CONTACT_CONTACTED,
    CONTACT_NO_ANSWER,
    CONTACT_REJECTED,
    CONTACT_REPLIED,
    STATUS_ISSUED,
    STATUS_NEW,
    Lead,
)

# Without progress these go back to the pool when owned_until passes.
_RELEASABLE: tuple[str, ...] = (CONTACT_CONTACTED, CONTACT_NO_ANSWER)
# The button '↩️ Вернуть в пул': anything handed out but an answer or a refusal.
_FINISHED: tuple[str, ...] = (CONTACT_REPLIED, CONTACT_REJECTED)
_TEAM_LEADS_SHOWN: int = 10
logger = logging.getLogger(__name__)

# Back in the pool, as if never handed out: the next person gets it clean.
_BACK_TO_POOL: dict[str, Any] = {
    "crm_status": STATUS_NEW,
    "issued_at": None,
    "issued_to_user_id": None,
    "owner_user_id": None,
    "owner_name": None,
    "owned_until": None,
    "remind_at": None,
    "contact_status": None,
    "contact_status_by_id": None,
    "contact_status_by": None,
    "contact_status_at": None,
}


def person_column() -> ColumnElement[Any]:
    return func.coalesce(Lead.owner_user_id, Lead.issued_to_user_id)


def person_of(lead: Lead) -> int | None:
    return lead.owner_user_id if lead.owner_user_id is not None else lead.issued_to_user_id


def can_act(lead: Lead, user_id: int | None, bot_owner_id: int) -> bool:
    """May this person press the buttons of the lead? Their own, nobody's yet, or anything for the owner of the bot."""
    person = person_of(lead)
    return person is None or (user_id is not None and user_id in (bot_owner_id, person))


def is_held_by_other(lead: Lead, user_id: int, now: datetime) -> bool:
    """Is it someone else's lead now: theirs with no end or until a date that has not passed?"""
    owner = lead.owner_user_id
    return owner is not None and owner != user_id and (lead.owned_until is None or lead.owned_until > now)


async def release_expired_leads(session: AsyncSession, now: datetime) -> int:
    """Leads whose time ended without progress, back to the pool. Running it again changes nothing."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Lead)
            .where(
                Lead.crm_status == STATUS_ISSUED,
                Lead.owned_until.is_not(None),
                Lead.owned_until <= now,
                Lead.contact_status.in_(_RELEASABLE),
            )
            .values(**_BACK_TO_POOL)
            .execution_options(synchronize_session=False)
        ),
    )
    await session.commit()
    return result.rowcount


# How often leads whose time ended are looked for.
RELEASE_INTERVAL_SECONDS: int = 15 * 60


async def release_expired_lead_job(session_maker: async_sessionmaker[AsyncSession]) -> None:
    """The scheduled run: never raises, so the schedule goes on whatever happens."""
    try:
        async with session_maker() as session:
            released = await release_expired_leads(session, datetime.now(UTC))
        if released:
            logger.info("Leads back in the pool, their time ended without progress: %d", released)
    except Exception:
        logger.exception("Giving expired leads back to the pool failed")


async def release_lead(session: AsyncSession, lead_id: int) -> bool:
    """'↩️ Вернуть в пул': a handed-out lead with no answer or refusal goes back now. False if it cannot."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Lead)
            .where(
                Lead.id == lead_id,
                Lead.crm_status == STATUS_ISSUED,
                func.coalesce(Lead.contact_status, "").not_in(_FINISHED),
            )
            .values(**_BACK_TO_POOL)
            .execution_options(synchronize_session=False)
        ),
    )
    await session.commit()
    return result.rowcount > 0


def can_release(lead: Lead) -> bool:
    return lead.crm_status == STATUS_ISSUED and lead.contact_status not in _FINISHED


async def reserve_for_export(
    session: AsyncSession, lead_ids: list[int], user_id: int, until: datetime, now: datetime
) -> set[int]:
    """
    Keep the leads of a request file for its author until `until`. Returns those really kept.

    The condition is in the statement itself: of two people exporting at once each lead goes to one.
    """
    if not lead_ids:
        return set()
    rows = await session.execute(
        update(Lead)
        .where(
            Lead.id.in_(lead_ids),
            Lead.crm_status == STATUS_NEW,
            or_(Lead.reserved_until.is_(None), Lead.reserved_until <= now, Lead.reserved_by == user_id),
        )
        .values(reserved_by=user_id, reserved_until=until)
        .returning(Lead.id)
        .execution_options(synchronize_session=False)
    )
    reserved = {int(lead_id) for lead_id in rows.scalars().all()}
    await session.commit()
    return reserved


async def assign_existing_leads(session: AsyncSession, bot_owner_id: int, days: int, not_before: datetime) -> int:
    """
    At start: leads handed out before people were told apart become the owner's of the bot.

    'Написал' is kept until its time plus `days`, but not before `not_before`, so that nothing goes back
    to the pool the moment the bot starts; 'Ответили' with no end. Only leads with nobody set are touched,
    so running it again changes nothing.
    """
    issued = await session.execute(
        update(Lead)
        .where(Lead.issued_at.is_not(None), Lead.issued_to_user_id.is_(None))
        .values(issued_to_user_id=bot_owner_id)
        .execution_options(synchronize_session=False)
    )
    written = (
        await session.scalars(
            select(Lead).where(
                Lead.contact_status.in_((CONTACT_CONTACTED, CONTACT_REPLIED)),
                Lead.owner_user_id.is_(None),
                Lead.issued_at.is_not(None),
            )
        )
    ).all()
    for lead in written:
        lead.owner_user_id = bot_owner_id
        lead.owner_name = lead.contact_status_by
        if lead.contact_status == CONTACT_CONTACTED:
            changed = lead.contact_status_at or not_before
            lead.owned_until = max(changed + timedelta(days=days), not_before)
        else:
            lead.owned_until = None
    await session.commit()
    return cast("CursorResult[Any]", issued).rowcount + len(written)


@dataclass(frozen=True)
class PersonStats:
    user_id: int
    name: str | None
    in_work: int
    replied: int
    rejected: int


async def get_people_stats(session: AsyncSession) -> list[PersonStats]:
    """For the owner of the bot: each person's leads in work, with an answer and with a refusal."""
    person = person_column()
    status = func.coalesce(Lead.contact_status, "")
    rows = await session.execute(
        select(
            person,
            func.max(Lead.owner_name),
            func.count(Lead.id).filter(status.not_in(_FINISHED)),
            func.count(Lead.id).filter(status == CONTACT_REPLIED),
            func.count(Lead.id).filter(status == CONTACT_REJECTED),
        )
        .where(Lead.issued_at.is_not(None), person.is_not(None))
        .group_by(person)
        .order_by(person)
    )
    names = await _names(session)
    return [
        PersonStats(int(user_id), owner_name or names.get(int(user_id)), int(in_work), int(replied), int(rejected))
        for user_id, owner_name, in_work, replied, rejected in rows.all()
    ]


async def _names(session: AsyncSession) -> dict[int, str]:
    """How each person was last seen to call themselves: by the statuses they set."""
    rows = await session.execute(
        select(Lead.contact_status_by_id, Lead.contact_status_by)
        .where(Lead.contact_status_by_id.is_not(None), Lead.contact_status_by.is_not(None))
        .order_by(Lead.contact_status_at)
    )
    return {int(user_id): str(name) for user_id, name in rows.all() if user_id is not None}


async def get_team_leads(session: AsyncSession) -> dict[int, list[Lead]]:
    """Each person's leads in work, the oldest first, for the owner of the bot to look at or give back."""
    person = person_column()
    rows = (
        await session.scalars(
            select(Lead)
            .where(
                Lead.crm_status == STATUS_ISSUED,
                person.is_not(None),
                func.coalesce(Lead.contact_status, "").not_in(_FINISHED),
            )
            .order_by(person, case((Lead.owned_until.is_(None), 1), else_=0), Lead.owned_until, Lead.id)
        )
    ).all()
    team: dict[int, list[Lead]] = defaultdict(list)
    for lead in rows:
        user_id = person_of(lead)
        if user_id is not None and len(team[user_id]) < _TEAM_LEADS_SHOWN:
            team[user_id].append(lead)
    return dict(team)


def held_clause(user_id: int) -> ColumnElement[bool]:
    """The leads of this person: theirs, or handed out to them and nobody's since."""
    return and_(Lead.issued_at.is_not(None), person_column() == user_id)
