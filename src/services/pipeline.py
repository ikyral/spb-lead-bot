"""Pipeline service to harvest, verify, score, and persist business leads."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import settings
from src.database.repo import find_existing_external_ids, get_lead_fingerprints, save_new_leads
from src.services.chains import load_chain_index, refresh_chain_flags
from src.services.checker import analyze_website, create_check_client
from src.services.dedup import DuplicateIndex, merge_duplicate_places
from src.services.dgis_key import DgisKeyPolicy, DgisQuota
from src.services.providers.base import RawPlace
from src.services.providers.csv_loader import parse_csv_leads
from src.services.providers.osm import OSMOverpassProvider
from src.services.providers.twogis import TwoGisOptions, TwoGisProvider
from src.services.scorer import calculate_lead_score, is_worth_saving
from src.services.site_status import SITE_DEAD, SITE_UNKNOWN, confidence_of_check, source_of_status

logger = logging.getLogger(__name__)

# Simultaneous website checks. They target different hosts and mostly wait on the
# network (up to TOTAL_CHECK_TIMEOUT_SECONDS for a dead site), so 20 is safe.
CHECK_CONCURRENCY: int = 20
# Upper bound for one CSV import (each row may need a website check).
MAX_CSV_PLACES: int = 2000

# Called as progress(done, total) after each website check.
ProgressCallback = Callable[[int, int], Awaitable[None]]


def _contacts_unknown(place: RawPlace) -> bool:
    """The source gave no contacts, and none came from a duplicate in another source."""
    return not place.contacts_known and not (place.phone or place.raw_website or place.socials)


async def _load_known_leads(session: AsyncSession) -> DuplicateIndex:
    known_leads = DuplicateIndex()
    for key, (name, phone, map_url, address) in enumerate(await get_lead_fingerprints(session)):
        known_leads.add(key, name, phone, map_url, address)
    return known_leads


async def _process_and_save_places(
    session: AsyncSession,
    places: list[RawPlace],
    concurrency: int = CHECK_CONCURRENCY,
    progress: ProgressCallback | None = None,
    extra_fields: dict[str, Any] | None = None,
    seen: list[RawPlace] | None = None,
) -> dict[str, int]:
    """
    Verify places concurrently, score them, and persist qualified leads.

    The same business found by several sources is merged before checking websites;
    a qualified place matching an already saved lead counts as a duplicate.
    `progress` is notified after every check; its errors never break the harvest.
    `seen` is everything this run has seen, also what is not saved, for counting branches of chains.
    """
    total_found = len(places)
    # Before merging: branches with one phone for the whole chain would become one place.
    observed = places if seen is None else seen
    places = merge_duplicate_places(places)
    if not places:
        return {
            "total_found": 0,
            "qualified": 0,
            "saved_new": 0,
            "duplicates_skipped": 0,
        }

    semaphore = asyncio.Semaphore(concurrency)
    checks_total = len(places)
    checks_done = 0

    async def _check_place(
        client: httpx.AsyncClient,
        place: RawPlace,
    ) -> tuple[RawPlace, str, str | None]:
        nonlocal checks_done
        async with semaphore:
            try:
                site_status, details = await analyze_website(place.raw_website, client=client)
            except Exception:
                logger.exception("Website check crashed for %r", place.raw_website)
                site_status, details = "dead_site", "Website check error"
        checks_done += 1
        if progress is not None:
            try:
                await progress(checks_done, checks_total)
            except Exception:
                logger.warning("Progress callback failed", exc_info=True)
        return place, site_status, details

    async with create_check_client() as client:
        results = await asyncio.gather(*(_check_place(client, place) for place in places))

    qualified = 0
    duplicates_skipped = 0
    leads_to_save: list[dict[str, Any]] = []
    chains_to_save: list[dict[str, Any]] = []

    known_leads = await _load_known_leads(session)
    chains = await load_chain_index(session, settings)
    for place in observed:
        chains.add(place.name, place.address)

    for place, site_status, details in results:
        # There is no site to check, but the organization may well have one: its card will tell.
        if _contacts_unknown(place) and not settings.LEADS_REQUIRE_CONTACT:
            site_status = SITE_UNKNOWN

        socials = place.socials
        if site_status == "social_only" and (not socials or not socials.strip()):
            socials = details or place.raw_website

        score = calculate_lead_score(
            category_key=place.category,
            site_status=site_status,
            phone=place.phone,
            socials=socials,
            high_margin=place.high_margin,
            small_chain=chains.branches(place.name) == 2,
        )

        if not is_worth_saving(site_status, score):
            continue

        qualified += 1

        if known_leads.find(place.name, place.phone, place.map_url, place.address) is not None:
            duplicates_skipped += 1
            continue

        is_chain = chains.is_chain(place.name)
        (chains_to_save if is_chain else leads_to_save).append(
            {
                "external_id": place.external_id,
                "source": place.source,
                "name": place.name,
                "category": place.category,
                "district": place.district,
                "address": place.address,
                "phone": place.phone,
                "socials": socials,
                "raw_website": place.raw_website,
                "site_status": site_status,
                "score": score,
                "crm_status": "new",
                "map_url": place.map_url,
                "email": place.email,
                "source_url": place.source_url,
                "rubric": place.rubric,
                "site_url": place.raw_website if site_status == SITE_DEAD else None,
                "site_confidence": confidence_of_check(site_status, place.source),
                "site_source": source_of_status(place.source) if site_status != SITE_UNKNOWN else None,
                "site_checked_at": None if site_status == SITE_UNKNOWN else datetime.now(UTC),
                "is_chain": is_chain,
                **(extra_fields or {}),
            }
        )

    # One transaction for the whole harvest instead of a commit per lead (~30x faster on SQLite).
    saved_new, already_saved = await save_new_leads(session, leads_to_save)
    duplicates_skipped += already_saved
    # Kept to count branches next time, but they are not new leads for anyone.
    saved_chains, _ = await save_new_leads(session, chains_to_save)
    marked = await refresh_chain_flags(session, chains)
    if saved_chains or marked:
        logger.info("Chains: %d saved aside, %d earlier leads marked", saved_chains, marked)

    return {
        "total_found": total_found,
        "qualified": qualified,
        "saved_new": saved_new,
        "duplicates_skipped": duplicates_skipped,
    }


async def save_collected_places(
    session: AsyncSession, places: list[RawPlace], extra_fields: dict[str, Any] | None = None
) -> dict[str, int]:
    """
    Verify, score and save places from automatic collection.

    Runs repeat, so most places are already known: they are skipped before the website
    check and never touched, whatever happened to the lead (issued, contacted, rejected).
    """
    merged = merge_duplicate_places(places)
    known_ids = await find_existing_external_ids(session, [place.external_id for place in merged])
    known_leads = await _load_known_leads(session)
    fresh = [
        place
        for place in merged
        if place.external_id not in known_ids
        and known_leads.find(place.name, place.phone, place.map_url, place.address) is None
    ]
    stats = await _process_and_save_places(session, fresh, extra_fields=extra_fields, seen=places)
    stats["total_found"] = len(places)
    stats["duplicates_skipped"] += len(places) - len(fresh)
    return stats


def _manual_search_quota(session: AsyncSession, api_key: str) -> DgisQuota:
    """Manual searches spend the same requests of the key as collection."""
    session_maker = async_sessionmaker(session.bind, expire_on_commit=False, class_=AsyncSession)
    return DgisQuota(session_maker, api_key, DgisKeyPolicy.from_settings(settings))


async def run_lead_harvest(
    session: AsyncSession,
    district_name: str,
    category_key: str,
    twogis_api_key: str | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Harvest leads from OSM and optionally 2GIS (queried concurrently), then verify and save them."""

    async def _search_osm() -> list[RawPlace]:
        try:
            return await OSMOverpassProvider().search_places(district_name, category_key)
        except Exception:
            # Providers handle network/API errors themselves; this only catches bugs, with a traceback.
            logger.exception("OSM lead harvest failed for district %s", district_name)
            return []

    async def _search_2gis() -> list[RawPlace]:
        if not twogis_api_key or not twogis_api_key.strip():
            return []
        try:
            provider = TwoGisProvider(
                twogis_api_key.strip(),
                options=TwoGisOptions.from_settings(settings),
                spend_request=_manual_search_quota(session, twogis_api_key).spend,
            )
            return await provider.search_places(district_name, category_key)
        except Exception:
            logger.exception("2GIS lead harvest failed for district %s", district_name)
            return []

    # gather keeps the order (OSM first), so duplicate merging keeps the OSM identity as before.
    osm_places, twogis_places = await asyncio.gather(_search_osm(), _search_2gis())
    return await _process_and_save_places(session, osm_places + twogis_places, progress=progress)


async def import_csv_harvest(
    session: AsyncSession,
    file_bytes: bytes,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """
    Parse leads from CSV file bytes, verify websites, score, and save them.

    Only the first MAX_CSV_PLACES places are processed; the rest is reported as `over_limit`.
    """
    places = parse_csv_leads(file_bytes)
    over_limit = max(0, len(places) - MAX_CSV_PLACES)
    stats: dict[str, int] = await _process_and_save_places(session, places[:MAX_CSV_PLACES], progress=progress)
    stats["over_limit"] = over_limit
    return stats
