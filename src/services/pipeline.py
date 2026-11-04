"""Pipeline service to harvest, verify, score, and persist business leads."""

import asyncio
import logging
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


async def _process_and_save_places(
    session: AsyncSession,
    places: list[RawPlace],
    concurrency: int = 5,
) -> dict[str, int]:
    """
    Verify places concurrently, score them, and persist qualified leads.

    The same business found by several sources is merged before checking websites;
    a qualified place matching an already saved lead counts as a duplicate.
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

    async def _check_place(
        client: httpx.AsyncClient,
        place: RawPlace,
    ) -> tuple[RawPlace, str, str | None]:
        async with semaphore:
            try:
                site_status, details = await analyze_website(
                    place.raw_website, client=client
                )
            except Exception:
                logger.exception("Website check crashed for %r", place.raw_website)
                site_status, details = "dead_site", "Website check error"
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
) -> dict[str, int]:
    """Harvest leads from OSM and optionally 2GIS, then verify and save them."""
    places: list[RawPlace] = []

    try:
        osm_provider = OSMOverpassProvider()
        osm_places = await osm_provider.search_places(district_name, category_key)
        places.extend(osm_places)
    except Exception as exc:
        logger.warning("OSM lead harvest failed for district %s: %s", district_name, exc)

    if twogis_api_key and twogis_api_key.strip():
        try:
            twogis_provider = TwoGisProvider(twogis_api_key.strip())
            twogis_places = await twogis_provider.search_places(district_name, category_key)
            places.extend(twogis_places)
        except Exception as exc:
            logger.warning("2GIS lead harvest failed for district %s: %s", district_name, exc)

    return await _process_and_save_places(session, places)


async def import_csv_harvest(
    session: AsyncSession,
    file_bytes: bytes,
) -> dict[str, int]:
    """Parse leads from CSV file bytes, verify websites, score, and save them."""
    places = parse_csv_leads(file_bytes)
    return await _process_and_save_places(session, places)
