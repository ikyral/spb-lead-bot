"""Pipeline service to harvest, verify, score, and persist business leads."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.repo import get_lead_fingerprints, upsert_lead
from src.services.checker import analyze_website, create_check_client
from src.services.dedup import DuplicateIndex, merge_duplicate_places
from src.services.providers.base import RawPlace
from src.services.providers.csv_loader import parse_csv_leads
from src.services.providers.osm import OSMOverpassProvider
from src.services.providers.twogis import TwoGisProvider
from src.services.scorer import calculate_lead_score

logger = logging.getLogger(__name__)

# Simultaneous website checks. They target different hosts and mostly wait on the
# network (up to TOTAL_CHECK_TIMEOUT_SECONDS for a dead site), so 20 is safe.
CHECK_CONCURRENCY: int = 20
# Upper bound for one CSV import (each row may need a website check).
MAX_CSV_PLACES: int = 2000

# Called as progress(done, total) after each website check.
ProgressCallback = Callable[[int, int], Awaitable[None]]


async def _process_and_save_places(
    session: AsyncSession,
    places: list[RawPlace],
    concurrency: int = CHECK_CONCURRENCY,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """
    Verify places concurrently, score them, and persist qualified leads.

    The same business found by several sources is merged before checking websites;
    a qualified place matching an already saved lead counts as a duplicate.
    `progress` is notified after every check; its errors never break the harvest.
    """
    total_found = len(places)
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
                site_status, details = await analyze_website(
                    place.raw_website, client=client
                )
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
        results = await asyncio.gather(
            *(_check_place(client, place) for place in places)
        )

    qualified = 0
    saved_new = 0
    duplicates_skipped = 0

    known_leads = DuplicateIndex()
    for key, (name, phone, map_url) in enumerate(await get_lead_fingerprints(session)):
        known_leads.add(key, name, phone, map_url)

    for place, site_status, details in results:
        socials = place.socials
        if site_status == "social_only" and (not socials or not socials.strip()):
            socials = details or place.raw_website

        score = calculate_lead_score(
            category_key=place.category,
            site_status=site_status,
            phone=place.phone,
            socials=socials,
            high_margin=place.high_margin,
        )

        if site_status == "has_site" or score < 30:
            continue

        qualified += 1

        if known_leads.find(place.name, place.phone, place.map_url) is not None:
            duplicates_skipped += 1
            continue

        lead_data: dict[str, Any] = {
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
        }

        is_new = await upsert_lead(session, lead_data)
        if is_new:
            saved_new += 1
        else:
            duplicates_skipped += 1

    return {
        "total_found": total_found,
        "qualified": qualified,
        "saved_new": saved_new,
        "duplicates_skipped": duplicates_skipped,
    }


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
        except Exception as exc:
            logger.warning("OSM lead harvest failed for district %s: %s", district_name, exc)
            return []

    async def _search_2gis() -> list[RawPlace]:
        if not twogis_api_key or not twogis_api_key.strip():
            return []
        try:
            return await TwoGisProvider(twogis_api_key.strip()).search_places(district_name, category_key)
        except Exception as exc:
            logger.warning("2GIS lead harvest failed for district %s: %s", district_name, exc)
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
    stats: dict[str, int] = await _process_and_save_places(
        session, places[:MAX_CSV_PLACES], progress=progress
    )
    stats["over_limit"] = over_limit
    return stats
