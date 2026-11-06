"""
Updates leads from what a collector read in their cards on 2GIS.

Unlike an ordinary import this changes leads that already exist, so it holds back wherever a
person's work or knowledge may be at stake: handed-out and rejected leads and verdicts about a
site stay as they are, and a card that was not understood says nothing about the organization.
"""

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import settings
from src.database.models import CONTACT_REJECTED, STATUS_NEW, STATUS_REJECTED, Lead
from src.database.repo import get_leads_by_external_ids
from src.services.chains import load_chain_index
from src.services.checker import analyze_website, create_check_client
from src.services.dedup import normalize_phones
from src.services.lead_file import EnrichRecord, EnrichResult
from src.services.normalize import normalize_email, normalize_phone_list, normalize_website
from src.services.scorer import calculate_lead_score
from src.services.site_status import (
    SITE_DEAD,
    SITE_FOUND,
    SITE_HAS_SITE,
    SITE_NO_SITE,
    SITE_SOCIAL_ONLY,
    SOURCE_2GIS_CARD,
    SOURCE_MANUAL,
    confidence_of_check,
)

logger = logging.getLogger(__name__)

REASON_UNKNOWN_LEAD = "такого лида нет в базе"
REASON_REPEATED = "повтор записи в файле"
REASON_ISSUED = "лид уже выдан"
REASON_REJECTED = "лид в отказе"
REASON_MANUAL = "о сайте уже есть ручной вердикт"
REASON_NOT_NEWER = "эти данные не новее уже применённых"
REASON_DUPLICATE_PHONE = "такой телефон уже есть у другого лида"
REASON_CHANGED = "лид изменился, пока проверялись сайты"

# Sites are checked at the same time, like in the pipeline: they mostly wait on the network.
_CHECK_CONCURRENCY: int = 20
_PHONE_COLUMN_MAX: int = 128
_SOCIALS_COLUMN_MAX: int = 512
_URL_COLUMN_MAX: int = 512
_EMAIL_COLUMN_MAX: int = 255
_SEPARATOR: str = ", "


@dataclass(frozen=True)
class UsefulLead:
    """A lead this file showed to need a site: what the owner needs to see it right away."""

    name: str
    category: str
    site_status: str
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    source_url: str | None = None


@dataclass
class EnrichStats:
    """Outcome of a file: counts and fixed reasons, and for the report the leads worth a look. Not for logs."""

    updated: int = 0
    sites_working: int = 0
    sites_dead: int = 0
    sites_social: int = 0
    no_site: int = 0
    not_recognized: int = 0
    ignored: Counter[str] = field(default_factory=Counter)
    useful: list[UsefulLead] = field(default_factory=list)

    @property
    def sites_found(self) -> int:
        return self.sites_working + self.sites_dead + self.sites_social

    @property
    def ignored_total(self) -> int:
        return sum(self.ignored.values())


def _why_not_updated(lead: Lead | None, record: EnrichRecord) -> str | None:
    """Why this record must not touch the lead, or None if it may."""
    if lead is None or lead.source != record.source:
        return REASON_UNKNOWN_LEAD
    if lead.crm_status == STATUS_REJECTED or lead.contact_status == CONTACT_REJECTED:
        return REASON_REJECTED
    if lead.crm_status != STATUS_NEW or lead.issued_at is not None:
        return REASON_ISSUED
    if lead.site_source == SOURCE_MANUAL:
        return REASON_MANUAL
    # Otherwise an old file sent again, or a late one, would bring back what is already out of date.
    if lead.site_source == SOURCE_2GIS_CARD and lead.site_checked_at and record.fetched_at <= lead.site_checked_at:
        return REASON_NOT_NEWER
    return None


def _joined(parts: list[str], limit: int) -> str | None:
    """As many whole values as fit into the column: half a phone number is worse than none."""
    kept: list[str] = []
    for part in parts:
        if len(_SEPARATOR.join([*kept, part])) <= limit:
            kept.append(part)
    return _SEPARATOR.join(kept) or None


def _merged_phones(lead: Lead, record: EnrichRecord) -> str | None:
    """A phone an admin saved stays first; the card only adds to it."""
    known = lead.phone or ""
    normalized = normalize_phone_list(_SEPARATOR.join(filter(None, [known, *record.phones])))
    return _joined((normalized or "").split(_SEPARATOR), _PHONE_COLUMN_MAX)


def _merged_socials(lead: Lead, record: EnrichRecord) -> str | None:
    known = [part.strip() for part in (lead.socials or "").split(",") if part.strip()]
    return _joined(list(dict.fromkeys([*known, *record.socials])), _SOCIALS_COLUMN_MAX)


async def _check_sites(records: list[EnrichRecord]) -> dict[str, tuple[str, str | None]]:
    """The answers of the checker by the external ID of a lead."""
    semaphore = asyncio.Semaphore(_CHECK_CONCURRENCY)

    async def check(client: httpx.AsyncClient, record: EnrichRecord) -> tuple[str, tuple[str, str | None]]:
        async with semaphore:
            try:
                return record.external_id, await analyze_website(normalize_website(record.website), client=client)
            except Exception:
                # The same as in the pipeline. The address is not logged: it comes from a file.
                logger.exception("Website check crashed")
                return record.external_id, (SITE_DEAD, "Website check error")

    async with create_check_client() as client:
        return dict(await asyncio.gather(*(check(client, record) for record in records)))


def _update_values(lead: Lead, record: EnrichRecord, check: tuple[str, str | None], branches: int) -> dict[str, Any]:
    """The new values of a lead, by the rules of the pipeline for a lead with this site."""
    website = normalize_website(record.website)
    site_status, details = check
    if site_status == SITE_HAS_SITE:
        site_status = SITE_FOUND
    phone = _merged_phones(lead, record)
    socials = _merged_socials(lead, record)
    if site_status == SITE_SOCIAL_ONLY and not socials:
        socials = (details or website or "")[:_SOCIALS_COLUMN_MAX] or None
    email = lead.email or normalize_email(record.email)
    return {
        "phone": phone,
        "socials": socials,
        "email": email[:_EMAIL_COLUMN_MAX] if email else None,
        "raw_website": website[:_URL_COLUMN_MAX] if website else None,
        "site_url": website[:_URL_COLUMN_MAX] if website and site_status in (SITE_FOUND, SITE_DEAD) else None,
        "site_status": site_status,
        "site_confidence": confidence_of_check(site_status, SOURCE_2GIS_CARD),
        "site_source": SOURCE_2GIS_CARD,
        "site_checked_at": datetime.now(UTC),
        "score": calculate_lead_score(lead.category, site_status, phone, socials, small_chain=branches == 2),
    }


async def _phone_owners(session: AsyncSession) -> dict[str, int]:
    owners: dict[str, int] = {}
    rows = await session.execute(select(Lead.id, Lead.phone).where(func.coalesce(Lead.phone, "") != ""))
    for lead_id, phone in rows.all():
        for number in normalize_phones(phone):
            owners.setdefault(number, lead_id)
    return owners


def _count_update(stats: EnrichStats, site_status: str) -> None:
    stats.updated += 1
    if site_status == SITE_FOUND:
        stats.sites_working += 1
    elif site_status == SITE_DEAD:
        stats.sites_dead += 1
    elif site_status == SITE_SOCIAL_ONLY:
        stats.sites_social += 1
    else:
        stats.no_site += 1


async def _apply_records(
    session: AsyncSession,
    records: list[EnrichRecord],
    checks: dict[str, tuple[str, str | None]],
    stats: EnrichStats,
) -> None:
    leads = await get_leads_by_external_ids(session, [record.external_id for record in records])
    owners = await _phone_owners(session)
    chains = await load_chain_index(session, settings)

    for record in records:
        lead = leads.get(record.external_id)
        reason = _why_not_updated(lead, record)
        if lead is None or reason is not None:
            # A person may have given a verdict while the sites were being checked.
            stats.ignored[reason or REASON_UNKNOWN_LEAD] += 1
            continue

        new_numbers = normalize_phones(_SEPARATOR.join(record.phones)) - normalize_phones(lead.phone)
        if any(number in owners for number in new_numbers):
            # A duplicate, as in any import: a known business is not saved the second time.
            stats.ignored[REASON_DUPLICATE_PHONE] += 1
            continue

        check = checks.get(record.external_id) if record.website else (SITE_NO_SITE, None)
        if check is None:
            # The lead became usable only now, say it came back to the pool: without a check nothing is known.
            stats.ignored[REASON_CHANGED] += 1
            continue

        values = _update_values(lead, record, check, chains.branches(lead.name))
        # The conditions once more in the statement itself: leads are handed out by a single UPDATE as well.
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(Lead)
                .where(
                    Lead.id == lead.id,
                    Lead.crm_status == STATUS_NEW,
                    Lead.issued_at.is_(None),
                    func.coalesce(Lead.site_source, "") != SOURCE_MANUAL,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount == 0:
            stats.ignored[REASON_CHANGED] += 1
            continue
        for number in new_numbers:
            owners[number] = lead.id
        _count_update(stats, values["site_status"])
        # A chain is not handed out, so there is nothing to tell the owner about it.
        if values["site_status"] != SITE_FOUND and not lead.is_chain:
            stats.useful.append(
                UsefulLead(
                    name=lead.name,
                    category=lead.category,
                    site_status=values["site_status"],
                    address=lead.address,
                    phone=values["phone"],
                    website=values["raw_website"],
                    source_url=lead.source_url,
                )
            )
    await session.commit()


async def apply_enrichment(session_maker: async_sessionmaker[AsyncSession], parsed: EnrichResult) -> EnrichStats:
    """
    Apply a result file to the leads it names.

    Sites are checked outside of any transaction, it takes a while. Everything is decided again
    right before writing, because leads may be handed out or get a verdict in the meantime.
    """
    stats = EnrichStats()
    stats.ignored.update(parsed.rejected)

    usable: dict[str, EnrichRecord] = {}
    for record in parsed.records:
        if not record.parsed:
            stats.not_recognized += 1
        elif record.external_id in usable:
            stats.ignored[REASON_REPEATED] += 1
        else:
            usable[record.external_id] = record
    if not usable:
        return stats

    async with session_maker() as session:
        leads = await get_leads_by_external_ids(session, list(usable))
    # Only sites that will be used: a file must not make the server visit addresses for nothing.
    to_check = [
        record
        for record in usable.values()
        if record.website and _why_not_updated(leads.get(record.external_id), record) is None
    ]
    checks = await _check_sites(to_check) if to_check else {}

    async with session_maker() as session:
        await _apply_records(session, list(usable.values()), checks, stats)
    logger.info("Enrichment: %d leads updated, %d records ignored", stats.updated, stats.ignored_total)
    return stats
