from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, Connection, CursorResult, and_, case, func, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import settings
from src.database.models import (
    CONTACT_CONTACTED,
    CONTACT_NO_ANSWER,
    CONTACT_REJECTED,
    CONTACT_REPLIED,
    STATUS_ISSUED,
    STATUS_NEW,
    Base,
    CollectorState,
    ImportLog,
    Lead,
    ProviderState,
    SearchProgress,
)
from src.services.scorer import calculate_lead_score
from src.services.site_status import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    ISSUE_GROUPS,
    SITE_FOUND,
    SITE_NO_SITE,
    SITE_UNKNOWN,
    SOURCE_MANUAL,
    UNVERIFIED_STATUSES,
    issue_group,
)
from src.services.twogis_links import TWOGIS_SOURCES, readable_card_number

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
NEW_LEAD_COLUMNS: tuple[str, ...] = (
    "email",
    "source_url",
    "issued_at",
    "contact_status",
    "contact_status_by_id",
    "contact_status_by",
    "contact_status_at",
    "exported_at",
    "imported_at",
    "rubric",
    "site_url",
    "site_confidence",
    "site_source",
    "site_checked_at",
    "is_chain",
    "contact_links",
)
# The same for the collector_state table.
NEW_STATE_COLUMNS: tuple[str, ...] = ("last_pool_reminder_at", "key_warning_mark", "scraper_check_runs")
# The same for the provider_state table.
NEW_PROVIDER_COLUMNS: tuple[str, ...] = ("requests_total", "key_fingerprint", "requests_day", "requests_day_used")
# Blocks in a row after which a provider stays off until an admin turns it on.
MAX_CONSECUTIVE_BLOCKS: int = 3


def _add_missing_columns(sync_conn: Connection) -> None:
    """create_all() does not alter existing tables, so older databases get new columns here."""
    models = ((Lead, NEW_LEAD_COLUMNS), (CollectorState, NEW_STATE_COLUMNS), (ProviderState, NEW_PROVIDER_COLUMNS))
    for model, names in models:
        table_name = model.__tablename__
        existing = {column["name"] for column in inspect(sync_conn).get_columns(table_name)}
        for name in names:
            if name not in existing:
                ddl_type = model.__table__.c[name].type.compile(dialect=sync_conn.dialect)
                sync_conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl_type}"))
                if name == "contact_status":
                    _move_contact_results_out_of_crm_status(sync_conn)
                elif name == "requests_total":
                    # NULL would never compare below the limit, and no request could be spent.
                    sync_conn.execute(text("UPDATE provider_state SET requests_total = 0"))
                elif name == "requests_day_used":
                    sync_conn.execute(text("UPDATE provider_state SET requests_day_used = 0"))


def _move_contact_results_out_of_crm_status(sync_conn: Connection) -> None:
    """Before contact_status existed, these results were stored in crm_status of handed-out leads."""
    for status in (CONTACT_CONTACTED, CONTACT_REPLIED, CONTACT_NO_ANSWER, CONTACT_REJECTED):
        sync_conn.execute(
            text(
                "UPDATE leads SET contact_status = :status, crm_status = :issued "
                "WHERE crm_status = :status AND issued_at IS NOT NULL"
            ),
            {"status": status, "issued": STATUS_ISSUED},
        )


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
        chunk = external_ids[start : start + _EXISTENCE_CHECK_CHUNK]
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
    in_list = _in_pool_clause() if crm_status == STATUS_NEW else Lead.crm_status == crm_status
    stmt = (
        select(Lead)
        .where(in_list, Lead.score >= min_score)
        .order_by(Lead.score.desc(), Lead.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _in_pool_clause() -> ColumnElement[bool]:
    """Leads that may still be handed out: new, without a known site, not part of a chain."""
    return and_(
        Lead.crm_status == STATUS_NEW,
        Lead.site_status != SITE_FOUND,
        func.coalesce(Lead.is_chain, False).is_(False),
    )


def _has_contacts_clause() -> ColumnElement[bool]:
    """The same as Lead.has_contacts, in SQL."""
    columns = (Lead.phone, Lead.raw_website, Lead.socials, Lead.email)
    return or_(*(func.trim(func.coalesce(column, "")) != "" for column in columns))


@dataclass(frozen=True)
class IssueRules:
    """What may go into a batch. By default: everything in the pool, whatever its niche."""

    issue_unknown: bool = True
    max_niche_share: float = 1.0

    def niche_cap(self, limit: int) -> int | None:
        """How many leads of one niche fit into a batch, None if there is no limit."""
        return None if self.max_niche_share >= 1 else max(1, int(limit * self.max_niche_share))


# Parallel batches can take leads this one has just picked; then it picks others.
_ISSUE_ATTEMPTS: int = 10


async def _pick_leads(
    session: AsyncSession, wanted: int, rules: IssueRules, cap: int | None, in_batch: Counter[str]
) -> list[int]:
    """The next leads by the order of handing out, within the limit of a niche."""
    group = case(*((Lead.site_status.in_(statuses), rank) for rank, statuses in enumerate(ISSUE_GROUPS)), else_=99)
    stmt = (
        select(Lead.id, Lead.category)
        .where(_in_pool_clause())
        .order_by(group, Lead.score.desc(), case((_has_contacts_clause(), 0), else_=1), Lead.created_at, Lead.id)
    )
    if not rules.issue_unknown:
        stmt = stmt.where(Lead.site_status != SITE_UNKNOWN)
    if cap is None:
        return list((await session.scalars(stmt.with_only_columns(Lead.id).limit(wanted))).all())

    picked: list[int] = []
    counts = Counter(in_batch)
    for lead_id, category in (await session.execute(stmt)).all():
        if counts[category] < cap:
            counts[category] += 1
            picked.append(lead_id)
            if len(picked) == wanted:
                break
    return picked


async def issue_next_leads(session: AsyncSession, limit: int, rules: IssueRules | None = None) -> list[Lead]:
    """
    Hand out up to `limit` new leads: the most certain about needing a site first, by score within that.

    Each lead becomes 'issued' only if it is still new, so concurrent calls never return the same lead.
    """
    rules = rules or IssueRules()
    cap = rules.niche_cap(limit)
    leads: list[Lead] = []
    in_batch: Counter[str] = Counter()
    for _ in range(_ISSUE_ATTEMPTS):
        picked = await _pick_leads(session, limit - len(leads), rules, cap, in_batch)
        if not picked:
            break
        stmt = (
            update(Lead)
            .where(Lead.id.in_(picked), Lead.crm_status == STATUS_NEW)
            .values(crm_status=STATUS_ISSUED, issued_at=datetime.now(UTC))
            .returning(Lead)
            .execution_options(synchronize_session=False)
        )
        taken = list((await session.scalars(stmt)).all())
        await session.commit()
        leads += taken
        in_batch.update(lead.category for lead in taken)
        if len(leads) >= limit:
            break
    # RETURNING has no guaranteed order.
    return sorted(
        leads,
        key=lambda lead: (issue_group(lead.site_status), -lead.score, not lead.has_contacts, lead.created_at, lead.id),
    )


async def return_undelivered_leads(session: AsyncSession, lead_ids: list[int]) -> None:
    """Put issued leads that never reached the admin back to the pool, keeping their position."""
    if not lead_ids:
        return
    await session.execute(
        update(Lead)
        .where(Lead.id.in_(lead_ids), Lead.crm_status == STATUS_ISSUED, Lead.contact_status.is_(None))
        .values(crm_status=STATUS_NEW, issued_at=None)
    )
    await session.commit()


async def count_new_leads(session: AsyncSession) -> int:
    """Number of leads not handed out yet."""
    result = await session.execute(select(func.count(Lead.id)).where(_in_pool_clause()))
    return int(result.scalar_one())


async def count_unknown_site_leads(session: AsyncSession) -> int:
    """How many leads of the pool have no verdict about the site yet."""
    result = await session.execute(
        select(func.count(Lead.id)).where(_in_pool_clause(), Lead.site_status == SITE_UNKNOWN)
    )
    return int(result.scalar_one())


@dataclass(frozen=True)
class PoolStats:
    """What the pool consists of, and how often a source was wrong about a site."""

    by_site: dict[str, int]
    by_niche: dict[str, int]
    # Verdicts 'there is a site' given by hand: the leads a source should not have offered.
    manual_site_found: int
    chains: int

    @property
    def waiting_for_check(self) -> int:
        return sum(count for status, count in self.by_site.items() if status in UNVERIFIED_STATUSES)


async def get_pool_stats(session: AsyncSession) -> PoolStats:
    """Counts for the statistics and the status of collection."""

    async def counts(column: Any) -> dict[str, int]:
        rows = await session.execute(select(column, func.count(Lead.id)).where(_in_pool_clause()).group_by(column))
        return {str(key): int(count) for key, count in rows.all()}

    manual = await session.execute(
        select(func.count(Lead.id)).where(Lead.site_status == SITE_FOUND, Lead.site_source == SOURCE_MANUAL)
    )
    chains = await session.execute(
        select(func.count(Lead.id)).where(Lead.crm_status == STATUS_NEW, Lead.is_chain.is_(True))
    )
    return PoolStats(
        by_site=await counts(Lead.site_status),
        by_niche=await counts(Lead.category),
        manual_site_found=int(manual.scalar_one()),
        chains=int(chains.scalar_one()),
    )


async def get_unknown_site_candidates(session: AsyncSession, niche: str | None = None) -> list[Lead]:
    """All of the pool that nobody has looked at and whose card on 2GIS a collector can open, the best first."""
    stmt = (
        select(Lead)
        .where(_in_pool_clause(), Lead.site_status == SITE_UNKNOWN, Lead.source.in_(TWOGIS_SOURCES))
        .order_by(Lead.score.desc(), Lead.created_at, Lead.id)
    )
    if niche is not None:
        stmt = stmt.where(Lead.category == niche)
    # A link is checked in Python, so a bad one cannot make a batch shorter than the pool allows.
    return [lead for lead in (await session.scalars(stmt)).all() if readable_card_number(lead.source_url) is not None]


async def get_unknown_site_leads(session: AsyncSession, limit: int) -> list[Lead]:
    """The best of the pool that nobody has looked at, whose card on 2GIS a collector can open."""
    return (await get_unknown_site_candidates(session))[:limit]


async def get_niche_site_counts(session: AsyncSession) -> dict[str, dict[str, int]]:
    """
    Leads by niche and status of the site, for judging niches.

    Everything checked counts, whatever became of the lead later. Chains are never handed out, and
    'no site' with low confidence means only an empty tag in OSM, so neither says anything about a niche.
    Unknown are only those that are still in the pool.
    """
    known = await session.execute(
        select(Lead.category, Lead.site_status, func.count(Lead.id))
        .where(
            Lead.site_status != SITE_UNKNOWN,
            func.coalesce(Lead.is_chain, False).is_(False),
            func.coalesce(Lead.site_confidence, "") != CONFIDENCE_LOW,
        )
        .group_by(Lead.category, Lead.site_status)
    )
    unknown = await session.execute(
        select(Lead.category, func.count(Lead.id))
        .where(_in_pool_clause(), Lead.site_status == SITE_UNKNOWN)
        .group_by(Lead.category)
    )
    counts: dict[str, dict[str, int]] = {}
    for category, status, count in known.all():
        counts.setdefault(category, {})[status] = int(count)
    for category, count in unknown.all():
        counts.setdefault(category, {})[SITE_UNKNOWN] = int(count)
    return counts


async def get_known_niches(session: AsyncSession) -> list[str]:
    """The niches that have any leads at all."""
    return [str(category) for category in (await session.scalars(select(Lead.category).distinct())).all()]


async def get_leads_by_external_ids(session: AsyncSession, external_ids: list[str]) -> dict[str, Lead]:
    """The leads known by these IDs."""
    leads: dict[str, Lead] = {}
    for start in range(0, len(external_ids), _EXISTENCE_CHECK_CHUNK):
        chunk = external_ids[start : start + _EXISTENCE_CHECK_CHUNK]
        for lead in (await session.scalars(select(Lead).where(Lead.external_id.in_(chunk)))).all():
            leads[lead.external_id] = lead
    return leads


async def count_new_leads_with_contacts(session: AsyncSession) -> int:
    """How many of the leads not handed out yet can be contacted without opening their card."""
    result = await session.execute(select(func.count(Lead.id)).where(_in_pool_clause(), _has_contacts_clause()))
    return int(result.scalar_one())


async def set_lead_phone(session: AsyncSession, lead_id: int, phone: str) -> Lead | None:
    """Save a phone an admin found in the card. Returns the updated lead, None if there is no such lead."""
    lead = await session.get(Lead, lead_id)
    if lead is None:
        return None
    lead.phone = phone
    await session.commit()
    return lead


async def set_site_verdict(session: AsyncSession, lead_id: int, has_site: bool) -> tuple[Lead | None, bool]:
    """
    Save what a person found out about the site. Returns the lead and whether anything changed.

    Repeating a verdict changes nothing, not even its time. The result of contacting the
    lead (contact_status) is a different matter and stays as it is.
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        return None, False
    status = SITE_FOUND if has_site else SITE_NO_SITE
    if lead.site_status == status and lead.site_source == SOURCE_MANUAL:
        return lead, False
    lead.site_status = status
    lead.site_source = SOURCE_MANUAL
    lead.site_confidence = CONFIDENCE_HIGH
    lead.site_checked_at = datetime.now(UTC)
    lead.site_url = (lead.site_url or lead.raw_website) if has_site else None
    lead.score = calculate_lead_score(lead.category, status, lead.phone, lead.socials)
    await session.commit()
    return lead, True


async def get_status_counts(session: AsyncSession) -> dict[str, int]:
    """Lead counts for every status present in the database."""
    result = await session.execute(select(Lead.crm_status, func.count(Lead.id)).group_by(Lead.crm_status))
    return {status: int(count) for status, count in result.all()}


async def count_issued_leads(session: AsyncSession) -> int:
    """Number of leads ever handed out, whatever their status is now."""
    result = await session.execute(select(func.count(Lead.id)).where(Lead.issued_at.is_not(None)))
    return int(result.scalar_one())


async def set_contact_status(
    session: AsyncSession, lead_id: int, status: str, changed_by_id: int, changed_by: str
) -> None:
    """Save what came of contacting a handed-out lead, who said so and when."""
    await session.execute(
        update(Lead)
        .where(Lead.id == lead_id)
        .values(
            contact_status=status,
            contact_status_by_id=changed_by_id,
            contact_status_by=changed_by[:128],
            contact_status_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def get_contact_status_counts(session: AsyncSession) -> dict[str, int]:
    """Lead counts by the result of contacting them."""
    result = await session.execute(
        select(Lead.contact_status, func.count(Lead.id))
        .where(Lead.contact_status.is_not(None))
        .group_by(Lead.contact_status)
    )
    return {str(status): int(count) for status, count in result.all()}


async def get_unexported_leads(session: AsyncSession, limit: int) -> list[Lead]:
    """The oldest leads that have not gone into an export file yet."""
    stmt = select(Lead).where(Lead.exported_at.is_(None)).order_by(Lead.created_at, Lead.id).limit(limit)
    return list((await session.scalars(stmt)).all())


async def mark_leads_exported(session: AsyncSession, lead_ids: list[int], exported_at: datetime) -> None:
    """Remember that these leads went into a file, so they are not exported again."""
    for start in range(0, len(lead_ids), _EXISTENCE_CHECK_CHUNK):
        chunk = lead_ids[start : start + _EXISTENCE_CHECK_CHUNK]
        await session.execute(update(Lead).where(Lead.id.in_(chunk)).values(exported_at=exported_at))
    await session.commit()


async def add_import_log(
    session: AsyncSession,
    source: str,
    file_name: str,
    ok: bool,
    error: str | None,
    total: int,
    saved_new: int,
    duplicates: int,
    rejected: int,
) -> None:
    """Record the outcome of importing a file."""
    session.add(
        ImportLog(
            source=source,
            file_name=file_name[:255],
            ok=ok,
            error=error[:512] if error else None,
            total=total,
            saved_new=saved_new,
            duplicates=duplicates,
            rejected=rejected,
        )
    )
    await session.commit()


async def get_last_import(session: AsyncSession) -> ImportLog | None:
    """The most recent imported file, if any."""
    return (await session.scalars(select(ImportLog).order_by(ImportLog.id.desc()).limit(1))).first()


async def count_imported_leads(session: AsyncSession) -> int:
    """Number of leads that came from export files."""
    result = await session.execute(select(func.count(Lead.id)).where(Lead.imported_at.is_not(None)))
    return int(result.scalar_one())


async def get_provider_states(session: AsyncSession) -> dict[str, ProviderState]:
    """Known states of providers by name; a provider that has had no problems may have none."""
    return {state.name: state for state in (await session.scalars(select(ProviderState))).all()}


async def _provider_state(session: AsyncSession, name: str) -> ProviderState:
    state = await session.get(ProviderState, name)
    if state is None:
        state = ProviderState(name=name, consecutive_blocks=0)
        session.add(state)
        await session.flush()
    return state


async def record_provider_run(session: AsyncSession, name: str) -> None:
    """Remember when a provider that must not run often was started."""
    state = await _provider_state(session, name)
    state.last_run_at = datetime.now(UTC)
    await session.commit()


async def restore_provider_run(session: AsyncSession, name: str, previous: datetime | None) -> None:
    """The run told nothing about the site: as far as the rest between runs goes, it did not happen."""
    state = await _provider_state(session, name)
    state.last_run_at = previous
    await session.commit()


async def record_provider_success(session: AsyncSession, name: str) -> None:
    """The site answered normally: blocks are no longer 'in a row'."""
    state = await session.get(ProviderState, name)
    if state is not None and (state.consecutive_blocks or state.blocked_until is not None):
        state.consecutive_blocks = 0
        state.blocked_until = None
        await session.commit()


async def record_provider_block(session: AsyncSession, name: str, reason: str, backoff_hours: float) -> ProviderState:
    """
    Register a refusal from a site: the pause doubles with every block in a row,
    and after MAX_CONSECUTIVE_BLOCKS the provider stays off until an admin turns it on.
    """
    state = await _provider_state(session, name)
    now = datetime.now(UTC)
    state.consecutive_blocks += 1
    state.last_block_reason = reason[:512]
    state.last_block_at = now
    state.blocked_until = now + timedelta(hours=backoff_hours * 2 ** (state.consecutive_blocks - 1))
    if state.consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
        state.disabled_at = now
    await session.commit()
    return state


async def disable_provider(session: AsyncSession, name: str, reason: str) -> ProviderState:
    """Switch a provider off until an admin turns it on or its key is replaced."""
    state = await _provider_state(session, name)
    now = datetime.now(UTC)
    state.disabled_at = now
    state.last_block_at = now
    state.last_block_reason = reason[:512]
    await session.commit()
    return state


async def sync_provider_key(session: AsyncSession, name: str, fingerprint: str) -> None:
    """A new key starts with an empty counter, and what happened to the old key does not apply to it."""
    try:
        state = await _provider_state(session, name)
    except IntegrityError:
        # A parallel call has just created the row: a manual search and a collection can meet here.
        await session.rollback()
        state = await _provider_state(session, name)
    if state.key_fingerprint != fingerprint:
        if state.key_fingerprint is not None:
            state.requests_total = 0
            state.disabled_at = None
            state.blocked_until = None
            state.consecutive_blocks = 0
        state.key_fingerprint = fingerprint
    await session.commit()


async def spend_provider_request(session: AsyncSession, name: str, fingerprint: str, ceiling: int | None) -> bool:
    """Count one API request, unless the ceiling is reached. One UPDATE: parallel searches cannot overrun it."""
    await sync_provider_key(session, name, fingerprint)
    stmt = update(ProviderState).where(ProviderState.name == name)
    if ceiling is not None:
        stmt = stmt.where(ProviderState.requests_total < ceiling)
    result = cast(
        "CursorResult[Any]",
        await session.execute(stmt.values(requests_total=ProviderState.requests_total + 1)),
    )
    await session.commit()
    return result.rowcount > 0


async def spend_daily_request(session: AsyncSession, name: str, day: str, ceiling: int) -> int | None:
    """
    Count one request of `day`, unless `ceiling` of them were sent that day already.

    Returns how many are left for the day after this one, None if none could be taken.
    """
    state = await _provider_state(session, name)
    if state.requests_day != day:
        state.requests_day = day
        state.requests_day_used = 0
    if state.requests_day_used >= ceiling:
        await session.commit()
        return None
    state.requests_day_used += 1
    await session.commit()
    return ceiling - state.requests_day_used


async def enable_provider(session: AsyncSession, name: str) -> None:
    """An admin's decision: forget the blocks and the pause."""
    state = await _provider_state(session, name)
    state.consecutive_blocks = 0
    state.blocked_until = None
    state.disabled_at = None
    await session.commit()


async def get_search_progress(session: AsyncSession) -> dict[str, SearchProgress]:
    """How far every search target has been walked, by its key."""
    return {row.key: row for row in (await session.scalars(select(SearchProgress))).all()}


async def save_search_progress(session: AsyncSession, rows: list[SearchProgress]) -> None:
    """Store new positions. Called after the leads of those pages are saved: otherwise a crash would lose them."""
    for row in rows:
        await session.merge(row)
    await session.commit()


async def get_collector_state(session: AsyncSession) -> CollectorState:
    """Return the collector state row, creating it on first use (the caller commits)."""
    state = await session.get(CollectorState, 1)
    if state is None:
        state = CollectorState(id=1)
        session.add(state)
        await session.flush()
    return state


async def claim_scraper_check(
    session: AsyncSession, limit: int, window: timedelta, now: datetime
) -> tuple[datetime | None, int]:
    """
    Take one of the diagnostics runs allowed within the window.

    Returns when the next one becomes possible if none is left (else None), and how many are left.
    """
    state = await get_collector_state(session)
    runs: list[datetime] = []
    for part in (state.scraper_check_runs or "").split(","):
        with suppress(ValueError):
            moment = datetime.fromisoformat(part)
            if moment + window > now:
                runs.append(moment)
    if len(runs) >= limit:
        return min(runs) + window, 0
    runs.append(now)
    state.scraper_check_runs = ",".join(moment.isoformat(timespec="seconds") for moment in runs)
    await session.commit()
    return None, limit - len(runs)


async def release_scraper_check(session: AsyncSession, claimed_at: datetime) -> None:
    """Give back a diagnostics run that never reached the site."""
    state = await get_collector_state(session)
    claimed = claimed_at.isoformat(timespec="seconds")
    runs = [part for part in (state.scraper_check_runs or "").split(",") if part and part != claimed]
    state.scraper_check_runs = ",".join(runs) or None
    await session.commit()


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
    stmt = update(Lead).where(Lead.id == lead_id).values(crm_status=new_status)
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
